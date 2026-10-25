"""华为账号登录认证（非官方实现）。

完整复现华为智慧生活 App 的登录链路：
  1. 获取 RSA 公钥并加密密码（RSA-OAEP / SHA256）
  2. loginV3 账号密码登录（可能要求二次验证）
  3. 挑战渠道：短信验证码（经 CAS 网页层下发）或设备挑战码（在鸿蒙设备查收）
  4. 完成挑战后拿到 service token（TGC）
  5. service token -> OAuth access token -> HMS-lite token（实际操作设备鉴权用）

协议参考社区逆向成果 ha-huawei-smarthome，按协议重新实现，去除 Home
Assistant 依赖。华为未向个人开放官方 API，接口/参数可能随版本失效。
本模块仅供个人学习与研究，请遵守华为服务条款。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .constants import (
    ACCOUNT_BASE_URL,
    OAUTH_BASE_URL,
    SMART_HOME_BASE_URL,
    SMART_HOME_APP_ID,
    SMART_HOME_IOS_APP_ID,
    SMART_HOME_OAUTH_CLIENT_ID,
    SMART_HOME_ACCOUNT_VERSION,
    SMART_HOME_ACCOUNT_CLIENT_VERSION,
    SMART_HOME_ACCOUNT_USER_AGENT,
    HMS_LITE_TOKEN_PATH,
    CAS_BASE_URL,
    CAS_LOGIN_PAGE,
    CAS_AJAX_PATH,
    CAS_CVERSION_BASE,
    CAS_CVERSION_FALLBACK,
    CAS_BASE_SWITCH_ACTION,
    CAS_SERVICE,
    CAS_REQUEST_CLIENT_TYPE,
    CAS_LOGIN_CHANNEL,
    CAS_LANGUAGE,
    CAS_SITE_ID,
    CAS_PAGE_INFO_ACTION,
    CAS_PAGE_NAME,
    CAS_ACCOUNT_TYPE_PHONE,
    CAS_SMS_OPER_TYPE,
    CAS_SMS_REQUEST_TYPE,
    SCOPE_FOR_CODE,
    SCOPE_FOR_SESSION,
)
from .exceptions import (
    AuthenticationError,
    InvalidCredentialsError,
    InvalidResponseError,
    TransientAuthenticationError,
)
from .models import (
    CHALLENGE_KIND_DEVICE,
    AuthSession,
    challenge_error_hint,
    channel_prompt,
    ChallengeChannel,
    LoginChallenge,
    LoginStart,
    channel_kind,
)

_LOGGER = logging.getLogger(__name__)
SET_COOKIE_SEPARATOR = "\n"


class _Resp:
    """最小 HTTP 响应对象。"""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


@dataclass
class _PendingLogin:
    account: str
    encrypted_password: str
    challenge_name: str
    challenge_type: str
    channels: tuple[ChallengeChannel, ...] = ()


class HuaweiAuth:
    """华为账号认证器（同步，调用方应在后台线程运行）。"""

    def __init__(
        self,
        *,
        device_id: str,
        device_name: str = "huawei-smarthome-pc",
        pushtmid: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not device_id:
            raise ValueError("device_id 不能为空")
        self.device_id = device_id
        self.device_name = device_name or "huawei-smarthome-pc"
        self.pushtmid = pushtmid
        self.timeout = timeout
        self._cookies: dict[str, str] = {}
        self._cas_cookies: dict[str, str] = {}
        self._cas_tokens: dict[str, str] = {}
        self._cas_version = CAS_CVERSION_FALLBACK
        self._cas_session_ready = False
        self._pending: _PendingLogin | None = None

    # ============ 公共 API ============
    def begin_login(self, account: str, password: str) -> LoginStart:
        """开始登录，返回会话（无需挑战）或挑战信息。"""
        account = account.strip()
        if not account or not password:
            raise InvalidCredentialsError("账号和密码不能为空")
        return self._begin_login(account, password)

    def switch_challenge_channel(self, channel: ChallengeChannel) -> LoginChallenge:
        """只切换校验渠道，不触发任何发送。

        提交验证码时要求 ``vAcT`` 与验证码的来源渠道一致：设备上收到的码
        必须用设备渠道提交，短信收到的码必须用短信渠道。界面换下拉时如果
        只改显示不改这里，用户切到设备渠道提交照样会被判"验证码不正确"。
        """
        pending = self._pending
        assert pending is not None
        selected = self._resolve_channel(channel, pending)
        self._pending = replace(
            pending,
            challenge_name=selected.name,
            challenge_type=selected.account_type,
        )
        return LoginChallenge(
            prompt=channel_prompt(selected),
            challenge_name=selected.name,
            challenge_type=selected.account_type,
            channels=pending.channels,
            kind=selected.kind,
        )

    def _resolve_channel(
        self, channel: ChallengeChannel, pending: "_PendingLogin"
    ) -> ChallengeChannel:
        matched = [c for c in pending.channels if c.key == channel.key]
        return matched[0] if matched else channel

    def select_challenge_channel(self, channel: ChallengeChannel) -> LoginChallenge:
        """选择验证渠道（若是短信则触发下发）。"""
        if self._pending is None:
            raise InvalidCredentialsError("当前没有待处理的登录挑战")
        return self._select_challenge_channel(channel)

    def complete_challenge(self, code: str) -> AuthSession:
        """提交验证码/挑战码，完成登录。"""
        if not code.strip():
            raise InvalidCredentialsError("验证码/挑战码不能为空")
        session = self._complete_challenge(code.strip())
        self._pending = None
        return session

    def refresh_oauth(self, session: AuthSession) -> AuthSession:
        if not session.service_token:
            raise AuthenticationError("缺少 service token")
        return self._refresh_oauth(session)

    def refresh_hms_lite(self, session: AuthSession) -> AuthSession:
        if not session.service_token:
            raise AuthenticationError("缺少 service token")
        return self._refresh_hms_lite(session)

    # ============ 内部实现 ============
    def _refresh_oauth(self, session: AuthSession) -> AuthSession:
        """用长期有效的 service token(TGC) 重放 OAuth 静默交换，换新 access token。

        与首次登录的 _oauth_request(need_code=False) 完全相同：service_token
        不过期，因此可反复换取新的 OAuth access token。
        """
        response = self._oauth_request(
            need_code=False, scope=SCOPE_FOR_SESSION, service_token=session.service_token
        )
        if response.status == 429 or response.status >= 500:
            raise TransientAuthenticationError("OAuth 刷新暂不可用")
        payload = _json_object(response.body)
        access_token = _find_string(payload, "access_token")
        expires_at = _expires_at(payload.get("expire_in"))
        if response.status >= 400 or not access_token or expires_at is None:
            raise AuthenticationError("OAuth 刷新失败")
        return replace(
            session,
            oauth_access_token=access_token,
            oauth_expires_at=expires_at,
        )

    def _refresh_hms_lite(self, session: AuthSession) -> AuthSession:
        """重新走 静默 code -> HMS-lite token 交换，换新 HMS-lite 凭据。"""
        code = self._obtain_silent_code(session.service_token)
        access_token, refresh_token, expires_at = self._exchange_hms_lite_code(
            session.user_id, code
        )
        return replace(
            session,
            hms_access_token=access_token,
            hms_refresh_token=refresh_token or session.hms_refresh_token,
            hms_expires_at=expires_at,
        )
    def _begin_login(self, account: str, password: str) -> LoginStart:
        self._cookies.clear()
        self._cas_cookies.clear()
        self._cas_tokens.clear()
        self._cas_version = CAS_CVERSION_FALLBACK
        self._cas_session_ready = False
        self._pending = None
        public_key = self._fetch_rsa_public_key()
        encrypted_password = _encrypt_password(password, public_key)
        response = self._login_request(
            account, encrypted_password, challenge=None, retry=False
        )
        fields = _parse_form(response.body)
        result_code = _first(fields, "resultCode")
        if response.status < 400 and result_code == "0":
            return LoginStart(session=self._finish_login(account, fields))
        channels = _extract_channels(fields)
        if not channels:
            raise InvalidCredentialsError("华为账号登录被拒绝（无可用验证渠道）")
        selected = _default_channel(channels)
        self._pending = _PendingLogin(
            account=account,
            encrypted_password=encrypted_password,
            challenge_name=selected.name,
            challenge_type=selected.account_type,
            channels=channels,
        )
        return LoginStart(
            challenge=LoginChallenge(
                prompt=channel_prompt(selected),
                challenge_name=selected.name,
                challenge_type=selected.account_type,
                channels=channels,
                kind=selected.kind,
            )
        )

    def _select_challenge_channel(self, channel: ChallengeChannel) -> LoginChallenge:
        pending = self._pending
        assert pending is not None
        selected = self._resolve_channel(channel, pending)
        if selected.is_sms and not selected.sent:
            self._dispatch_web_sms_code(selected, pending.account)
            selected = replace(selected, sent=True)
        self._pending = replace(
            pending,
            challenge_name=selected.name,
            challenge_type=selected.account_type,
        )
        return LoginChallenge(
            prompt=channel_prompt(selected),
            challenge_name=selected.name,
            challenge_type=selected.account_type,
            channels=pending.channels,
            kind=selected.kind,
        )

    def _dispatch_web_sms_code(self, channel: ChallengeChannel, account: str) -> None:
        """通过 CAS 网页层请求华为下发短信验证码。"""
        self._cas_open_session()
        self._refresh_cas_tokens()
        self._cas_ajax(
            "chkRisk",
            {
                "userAccount": account,
                "service": CAS_SERVICE,
                "operType": "11",
                "registerCountry": "cn",
                "lowLogin": "",
                "ext_clientInfo": "",
            },
        )
        candidates = _sms_phone_candidates(channel.name, account)
        last_error = "未知错误"
        for phone in candidates:
            response = self._cas_ajax(
                "getSMSCodeV3",
                {
                    "userAccount": account,
                    "accountType": CAS_ACCOUNT_TYPE_PHONE,
                    "mobilePhone": phone,
                    "operType": CAS_SMS_OPER_TYPE,
                    "smsReqType": CAS_SMS_REQUEST_TYPE,
                    "siteID": CAS_SITE_ID,
                },
            )
            payload = _json_object_or_none(response.body)
            if not _cas_call_failed(response.status, payload):
                return
            last_error = _cas_error_text(payload, response.status)
            if not _cas_failure_is_explicit(payload):
                raise AuthenticationError(last_error)
        raise AuthenticationError(last_error)

    def _cas_open_session(self) -> None:
        if self._cas_session_ready:
            return
        self._request(
            "GET",
            CAS_LOGIN_PAGE,
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": SMART_HOME_ACCOUNT_USER_AGENT,
            },
            None,
            cookies=self._cas_cookies,
        )
        self._cas_session_ready = True
        self._cas_sync_version()

    def _cas_sync_version(self) -> None:
        response = self._cas_ajax(
            CAS_BASE_SWITCH_ACTION, {}, with_tokens=False, version=CAS_CVERSION_BASE
        )
        payload = _json_object_or_none(response.body)
        version = payload.get("cookieVersion") if payload else None
        if isinstance(version, str) and version:
            self._cas_version = version

    def _refresh_cas_tokens(self) -> None:
        response = self._cas_ajax(
            CAS_PAGE_INFO_ACTION, {"pageName": CAS_PAGE_NAME}, with_tokens=False
        )
        payload = _json_object_or_none(response.body)
        token = payload.get("pageToken") if payload else None
        token_key = payload.get("pageTokenKey") if payload else None
        if not isinstance(token, str) or not isinstance(token_key, str):
            raise AuthenticationError("华为账号网页层 pageToken 不可用")
        self._cas_tokens = {"pageToken": token, "pageTokenKey": token_key}

    def _cas_ajax(
        self,
        action: str,
        values: Mapping[str, str],
        *,
        with_tokens: bool = True,
        version: str | None = None,
    ) -> _Resp:
        query = urlencode(
            {
                "reflushCode": f"{time.time()}",
                "cVersion": version or self._cas_version,
            }
        )
        params: list[tuple[str, str]] = [
            ("reqClientType", CAS_REQUEST_CLIENT_TYPE),
            ("loginChannel", CAS_LOGIN_CHANNEL),
            ("lang", CAS_LANGUAGE),
            ("languageCode", CAS_LANGUAGE),
        ]
        if with_tokens:
            params.extend(self._cas_tokens.items())
        params.extend(values.items())
        return self._request(
            "POST",
            f"{CAS_BASE_URL}{CAS_AJAX_PATH}/{action}?{query}",
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": CAS_BASE_URL,
                "Referer": CAS_LOGIN_PAGE,
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": SMART_HOME_ACCOUNT_USER_AGENT,
            },
            urlencode(params).encode("utf-8"),
            cookies=self._cas_cookies,
        )

    def _complete_challenge(self, code: str) -> AuthSession:
        pending = self._pending
        assert pending is not None
        response = self._login_request(
            pending.account,
            pending.encrypted_password,
            challenge=(code, pending.challenge_name, pending.challenge_type),
            retry=True,
        )
        fields = _parse_form(response.body)
        result_code = _first(fields, "resultCode")
        if response.status >= 400 or result_code != "0":
            _LOGGER.warning(
                "华为账号挑战被拒绝: status=%s resultCode=%s desc=%s",
                response.status,
                result_code,
                (_first(fields, "errorDesc") or "")[:200],
            )
            raise InvalidCredentialsError(
                challenge_error_hint(
                    result_code,
                    pending.channels,
                    channel_kind(pending.challenge_type),
                )
            )
        return self._finish_login(pending.account, fields)

    def _finish_login(self, account: str, fields: Mapping[str, list[str]]) -> AuthSession:
        service_token = _first(fields, "TGC")
        user_id = _first(fields, "userID")
        if not service_token or not user_id:
            raise AuthenticationError("登录响应缺少会话信息")

        st_response = self._post_account_form(
            "/AccountServer/IUserInfoMng/stAuth",
            {
                "ver": SMART_HOME_ACCOUNT_VERSION,
                "st": service_token,
                "app": SMART_HOME_APP_ID,
                "agr": "1",
                "chg": "0",
                "clT": "54",
                "cn": "54000000",
                "dS": "0",
                "dvID": self.device_id,
                "dvT": "6",
                "gAc": "0",
                "tmT": "iPhone",
            },
        )
        st_fields = _parse_form(st_response.body)
        if st_response.status >= 400 or _first(st_fields, "resultCode") not in (
            None,
            "",
            "0",
        ):
            raise AuthenticationError("stAuth 会话校验失败")

        self._post_user_info(user_id)
        code = self._obtain_silent_code(service_token)
        session_response = self._oauth_request(
            need_code=False, scope=SCOPE_FOR_SESSION, service_token=service_token
        )
        session_payload = _json_object(session_response.body)
        oauth_access_token = _find_string(session_payload, "access_token")
        if session_response.status >= 400 or not oauth_access_token:
            raise AuthenticationError("OAuth 会话交换失败")

        hms_access_token, hms_refresh_token, hms_expires_at = (
            self._exchange_hms_lite_code(user_id, code)
        )
        return AuthSession(
            account=account,
            user_id=user_id,
            service_token=service_token,
            hms_access_token=hms_access_token,
            hms_refresh_token=hms_refresh_token,
            hms_expires_at=hms_expires_at,
            oauth_access_token=oauth_access_token,
            oauth_expires_at=_expires_at(session_payload.get("expire_in")),
            device_id=self.device_id,
            device_name=self.device_name,
            pushtmid=self.pushtmid,
            app_id=SMART_HOME_IOS_APP_ID,
            base_url=SMART_HOME_BASE_URL,
            home_zone=_first(st_fields, "homeZone"),
            oauth_domain=_first(st_fields, "oauthDomain"),
            site_domain=_first(st_fields, "siteDomain"),
        )

    def _obtain_silent_code(self, service_token: str) -> str:
        response = self._oauth_request(
            need_code=True, scope=SCOPE_FOR_CODE, service_token=service_token
        )
        if response.status == 429 or response.status >= 500:
            raise TransientAuthenticationError("OAuth code 请求暂不可用")
        payload = _json_object(response.body)
        code = _find_string(payload, "code")
        if response.status >= 400 or not code:
            raise AuthenticationError("OAuth code 交换失败")
        return code

    def _exchange_hms_lite_code(
        self, user_id: str, code: str
    ) -> tuple[str, str | None, datetime | None]:
        response = self._request(
            "POST",
            f"{SMART_HOME_BASE_URL}{HMS_LITE_TOKEN_PATH}",
            {
                "Accept": "*/*",
                "Accept-Language": "zh-Hans-CN;q=1",
                "Authorization": "Bearer (null)",
                "Content-Type": "application/json;charset=UTF-8",
                "X-AppId": SMART_HOME_IOS_APP_ID,
                "X-Huid": user_id,
                "X-RequestId": str(uuid.uuid4()).upper(),
                "User-Agent": SMART_HOME_ACCOUNT_USER_AGENT,
            },
            json.dumps(
                {"appId": SMART_HOME_OAUTH_CLIENT_ID, "code": quote(code, safe="")},
                separators=(",", ":"),
            ).encode("utf-8"),
            send_cookies=False,
        )
        if response.status == 429 or response.status >= 500:
            raise TransientAuthenticationError("HMS-lite token 交换暂不可用")
        payload = _json_object(response.body)
        access_token = payload.get("access_token")
        if response.status >= 400 or not isinstance(access_token, str):
            raise AuthenticationError("HMS-lite token 交换失败")
        refresh_token = payload.get("refresh_token")
        return (
            access_token,
            refresh_token if isinstance(refresh_token, str) else None,
            _expires_at(payload.get("expires_in")),
        )

    def _fetch_rsa_public_key(self) -> str:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<GetResourceReq>"
            f"<version>{SMART_HOME_ACCOUNT_VERSION}</version>"
            "<resourceID>upLogin</resourceID>"
            "<reqClientType>54</reqClientType>"
            "<languageCode>zh-Hans-CN</languageCode>"
            "</GetResourceReq>"
        ).encode("utf-8")
        response = self._post_account(
            "/AccountServer/IUserInfoMng/getResource", body, "text/xml; charset=utf-8"
        )
        key = _public_key_from_response(response.body)
        if response.status >= 400 or not key:
            raise AuthenticationError("RSA 公钥获取失败")
        return key

    def _login_request(
        self,
        account: str,
        encrypted_password: str,
        *,
        challenge: tuple[str, str, str] | None,
        retry: bool,
    ) -> _Resp:
        params: list[tuple[str, str]] = [
            ("ver", SMART_HOME_ACCOUNT_VERSION),
            ("acT", _account_type_code(account)),
            ("ac", account),
            ("pw", encrypted_password),
            ("dvT", "6"),
            ("dvID", self.device_id),
            ("tmT", "iPhone"),
            ("clT", "54"),
            ("cn", "54000000"),
            ("os", "iOS15.8.8"),
            ("app", SMART_HOME_APP_ID),
            ("dvN", self.device_name),
            ("uuid", self.device_id),
        ]
        if challenge:
            code, name, account_type = challenge
            if code is not None:
                params.append(("vCode", code))
            params.extend([("vAcT", account_type), ("vAc", name)])
        params.extend(
            [
                ("lang", "zh-Hans-CN"),
                ("dS", "0"),
                ("mA", "1" if retry else "0"),
                ("deviceInfo", json.dumps(self._device_info(), separators=(",", ":"))),
            ]
        )
        return self._post_account(
            "/AccountServer/IDM/loginV3",
            urlencode(params).encode("utf-8"),
            "application/x-www-form-urlencoded; charset=UTF-8",
        )

    def _post_user_info(self, user_id: str) -> None:
        root = ElementTree.Element("GetUserInfoReq")
        for name, value in (
            ("version", SMART_HOME_ACCOUNT_VERSION),
            ("userID", user_id),
            ("reqClientType", "54"),
            ("queryRangeFlag", "00100000"),
            ("cliDataVersion", f"{int(time.time() * 1000)}_rp"),
            ("cliDataRangeFlag", "11110010"),
        ):
            ElementTree.SubElement(root, name).text = value
        info = ElementTree.SubElement(root, "deviceInfo")
        for name, value in self._device_info().items():
            ElementTree.SubElement(info, name).text = str(value)
        response = self._post_account(
            "/AccountServer/IUserInfoMng/getUserInfo",
            ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
            "text/xml; charset=utf-8",
        )
        if response.status >= 400:
            raise AuthenticationError("用户信息查询失败")

    def _oauth_request(
        self, *, need_code: bool, scope: str, service_token: str
    ) -> _Resp:
        body = urlencode(
            [
                ("grant_type", "service_token"),
                ("scope", scope),
                ("service_token", service_token),
                ("device_type", "6"),
                ("package_name", SMART_HOME_APP_ID),
                ("siteId", "1"),
                ("device_id", self.device_id),
                ("need_code", "true" if need_code else "false"),
                ("uuid", self.device_id),
            ]
        ).encode("utf-8")
        query = urlencode(
            {
                "client_id": SMART_HOME_OAUTH_CLIENT_ID,
                "Version": SMART_HOME_ACCOUNT_VERSION,
                "cVersion": SMART_HOME_ACCOUNT_CLIENT_VERSION,
                "srcAppName": SMART_HOME_APP_ID,
            }
        )
        return self._request(
            "POST",
            f"{OAUTH_BASE_URL}/oauth2/v3/silent_token?{query}",
            {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh-Hans;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "terminal-type": "iPhone",
                "User-Agent": SMART_HOME_ACCOUNT_USER_AGENT,
            },
            body,
            send_cookies=False,
        )

    def _post_account_form(
        self, path: str, values: Mapping[str, str]
    ) -> _Resp:
        return self._post_account(
            path,
            urlencode(values).encode("utf-8"),
            "application/x-www-form-urlencoded; charset=UTF-8",
        )

    def _post_account(self, path: str, body: bytes, content_type: str) -> _Resp:
        query = urlencode(
            {
                "Version": SMART_HOME_ACCOUNT_VERSION,
                "cVersion": SMART_HOME_ACCOUNT_CLIENT_VERSION,
                "ctrID": uuid.uuid4().hex,
                "srcAppName": SMART_HOME_APP_ID,
            }
        )
        url = f"{ACCOUNT_BASE_URL}{path}?{query}"
        return self._request(
            "POST",
            url,
            {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh-Hans;q=0.9",
                "Authorization": str(int(time.time() * 1000)),
                "Content-Type": content_type,
                "SOAPAction": url,
                "User-Agent": SMART_HOME_ACCOUNT_USER_AGENT,
            },
            body,
        )

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        send_cookies: bool = True,
        cookies: dict[str, str] | None = None,
    ) -> _Resp:
        jar = self._cookies if cookies is None else cookies
        request_headers = dict(headers)
        if send_cookies and jar:
            request_headers["Cookie"] = "; ".join(
                f"{n}={v}" for n, v in jar.items()
            )
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return _Resp(
                    status=response.status,
                    headers=_collect_headers(response.headers),
                    body=response.read(),
                )
        except HTTPError as error:
            return _Resp(
                status=error.code,
                headers=_collect_headers(error.headers),
                body=error.read(),
            )
        except (URLError, TimeoutError, OSError) as error:
            raise TransientAuthenticationError(
                f"认证传输失败: {error}"
            ) from error

    def _device_info(self) -> dict[str, str | int]:
        return {
            "terminalCategory": 9,
            "deviceType": 6,
            "deviceID": self.device_id,
            "terminalType": "iPhone",
            "deviceAliasName": self.device_name,
            "uuid": self.device_id,
        }


# ============ 模块级辅助函数 ============
def _encrypt_password(password: str, public_key_pem: str) -> str:
    public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise AuthenticationError("RSA 公钥无效")
    encrypted = public_key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return encrypted.hex().upper()


def _public_key_from_response(body: bytes) -> str | None:
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if value is not None:
        found = _find_public_key(value)
        if found:
            return found
    try:
        root = ElementTree.fromstring(body.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError):
        return None
    for element in root.iter():
        text = (element.text or "").strip()
        if not text:
            continue
        found = _normalize_public_key(text)
        if found:
            return found
        try:
            nested = json.loads(text)
        except json.JSONDecodeError:
            continue
        found = _find_public_key(nested)
        if found:
            return found
    return None


def _find_public_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower().replace("_", "-")
            if isinstance(child, str) and any(
                marker in lowered
                for marker in ("public-key", "publickey", "rsapublic", "rsa-key")
            ):
                found = _normalize_public_key(child)
                if found:
                    return found
            found = _find_public_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_public_key(child)
            if found:
                return found
    return None


def _normalize_public_key(value: str) -> str | None:
    text = value.strip()
    if "BEGIN PUBLIC KEY" in text or "BEGIN RSA PUBLIC KEY" in text:
        return text
    compact = re.sub(r"\s+", "", text)
    try:
        der = base64.b64decode(compact, validate=True)
        public_key = serialization.load_der_public_key(der)
    except (ValueError, TypeError, UnicodeEncodeError):
        return None
    if not isinstance(public_key, rsa.RSAPublicKey):
        return None
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _parse_form(body: bytes) -> dict[str, list[str]]:
    return parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)


def _first(fields: Mapping[str, list[str]], key: str) -> str | None:
    values = fields.get(key)
    return values[0] if values else None


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidResponseError("响应不是合法 JSON") from error
    if not isinstance(value, dict):
        raise InvalidResponseError("响应不是 JSON 对象")
    return value


def _json_object_or_none(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _find_string(value: Any, wanted: str) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) == wanted and isinstance(child, str):
                return child
            found = _find_string(child, wanted)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string(child, wanted)
            if found:
                return found
    return None


def _expires_at(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _extract_channels(fields: Mapping[str, list[str]]) -> tuple[ChallengeChannel, ...]:
    raw = _first(fields, "errorDesc")
    if not raw:
        return ()
    try:
        details = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    items = details.get("authCodeSentList") if isinstance(details, dict) else None
    if not isinstance(items, list):
        return ()
    channels: list[ChallengeChannel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        account_type = item.get("accountType")
        if not isinstance(name, str) or account_type is None:
            continue
        channels.append(
            ChallengeChannel(
                name=name,
                account_type=str(account_type),
                kind=channel_kind(account_type),
                sent=str(item.get("sent")) == "1",
            )
        )
    return tuple(channels)


def _default_channel(channels: tuple[ChallengeChannel, ...]) -> ChallengeChannel:
    """选默认渠道：优先「已发送」的设备渠道。

    华为在登录请求被拒时就把验证码推到了账号下的设备上——用户不用点任何
    按钮，抬头看设备就能拿到，这是最省事的一条路。短信还要额外点一次发送、
    等短信到达，所以只在没有设备渠道时才退回去。
    """
    for channel in channels:
        if channel.kind == CHALLENGE_KIND_DEVICE and channel.sent:
            return channel
    for channel in channels:
        if channel.kind == CHALLENGE_KIND_DEVICE:
            return channel
    for channel in channels:
        if channel.is_sms:
            return channel
    for channel in channels:
        if channel.sent:
            return channel
    return channels[0]


def _sms_phone_candidates(masked: str, account: str) -> list[str]:
    candidates = [masked]
    digits = re.sub(r"\D", "", account)
    if digits:
        candidates.append(f"0086{digits}")
    ordered: list[str] = []
    for value in candidates:
        if value and value not in ordered:
            ordered.append(value)
    return ordered


def _account_type_code(account: str) -> str:
    text = re.sub(r"[\s-]", "", account.strip())
    if "@" in text:
        return "1"
    if re.fullmatch(r"\+?\d{5,20}", text):
        return "2"
    return "0"


def _collect_headers(headers: Any) -> dict[str, str]:
    collected: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name)
        if key.lower() != "set-cookie":
            collected[key] = str(value)
            continue
        existing = collected.get(key)
        collected[key] = (
            f"{existing}{SET_COOKIE_SEPARATOR}{value}" if existing else str(value)
        )
    return collected


def _set_cookie_values(headers: Mapping[str, str]) -> list[str]:
    values: list[str] = []
    for name, value in headers.items():
        if name.lower() != "set-cookie":
            continue
        values.extend(part for part in value.split(SET_COOKIE_SEPARATOR) if part)
    return values


def _cas_call_failed(status: int, payload: Mapping[str, Any] | None) -> bool:
    if not 200 <= status < 300:
        return True
    if not isinstance(payload, Mapping):
        return True
    error_code = payload.get("errorCode")
    if error_code is not None and not _cas_success_code(error_code):
        return True
    success = payload.get("isSuccess")
    if success is not None:
        return not _cas_success_value(success)
    result_code = payload.get("resultCode")
    if result_code is not None:
        return not _cas_success_code(result_code)
    return error_code is None


def _cas_success_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "ok", "success"}


def _cas_success_code(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value in {0, 200}
    return str(value).strip().lower() in {"0", "200", "ok", "success"}


def _cas_failure_is_explicit(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(
        payload.get(key) is not None for key in ("isSuccess", "resultCode", "errorCode")
    )


def _cas_error_text(payload: Mapping[str, Any] | None, status: int) -> str:
    if payload is None:
        return f"HTTP {status}"
    code = payload.get("errorCode")
    desc = payload.get("errorDesc")
    if code or desc:
        return f"{code} {desc}".strip()
    return f"HTTP {status}"
