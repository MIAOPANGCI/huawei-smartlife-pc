"""华为智慧生活客户端（高层封装）。

整合登录认证、设备查询、设备控制与 Token 刷新，并支持会话持久化。
本模块为调用方（如 GUI）提供简洁接口。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .api import HuaweiApi
from .auth import HuaweiAuth
from .exceptions import AuthenticationError
from .models import (
    AuthSession,
    ChallengeChannel,
    Device,
    DeviceState,
    DiscoverySnapshot,
    LoginChallenge,
    LoginStart,
    MessageChannelSession,
    Scene,
    SmartHomeCloudRoute,
)
from .mqtt_control import HuaweiMqttController

_LOGGER = logging.getLogger(__name__)

MqttMessageHandler = Callable[[str, dict[str, Any]], None]
CommandResponseHandler = Callable[[str, str, Any], None]


def _gen_64hex() -> str:
    """生成 64 字符（32 字节）十六进制，用作 device_id / pushtmid。"""
    return secrets.token_hex(32)


class HuaweiSmartLifeClient:
    """华为智慧生活 PC 客户端。"""

    def __init__(
        self,
        *,
        device_name: str = "huawei-smarthome-pc",
        timeout: float = 20.0,
        on_message: MqttMessageHandler | None = None,
        on_command_response: CommandResponseHandler | None = None,
    ) -> None:
        self.device_id = _gen_64hex()
        self.pushtmid = _gen_64hex()
        self.device_name = device_name
        self.timeout = timeout
        self._on_message = on_message

        self._auth = HuaweiAuth(
            device_id=self.device_id,
            device_name=device_name,
            pushtmid=self.pushtmid,
            timeout=timeout,
        )
        self._api = HuaweiApi(timeout=timeout)
        self._mqtt = HuaweiMqttController(
            connect_timeout=timeout,
            on_message=self._on_mqtt_message,
            on_command_response=on_command_response,
        )
        self._session: AuthSession | None = None
        self._route: SmartHomeCloudRoute | None = None
        self._message_session: MessageChannelSession | None = None
        self._lock = threading.Lock()

    # ============ 登录 ============
    def begin_login(self, account: str, password: str) -> LoginStart:
        """开始登录。若无需挑战，LoginStart.session 直接返回；否则需走挑战流程。"""
        result = self._auth.begin_login(account, password)
        if result.session is not None:
            self._session = result.session
        return result

    def switch_challenge_channel(self, channel: ChallengeChannel) -> LoginChallenge:
        """只切渠道（本地状态），不发任何请求。"""
        return self._auth.switch_challenge_channel(channel)

    def select_challenge_channel(self, channel: ChallengeChannel) -> LoginChallenge:
        return self._auth.select_challenge_channel(channel)

    def complete_challenge(self, code: str) -> AuthSession:
        self._session = self._auth.complete_challenge(code)
        return self._session

    # ============ 会话管理 ============
    def _require_session(self) -> AuthSession:
        if self._session is None:
            raise AuthenticationError("尚未登录，请先调用 begin_login")
        return self._session

    def ensure_valid(self) -> AuthSession:
        """确保 Token 有效，必要时刷新。"""
        session = self._require_session()
        with self._lock:
            if not session.is_oauth_valid(300):
                session = self._auth.refresh_oauth(session)
                self._session = session
            if not session.is_hms_valid(300):
                session = self._auth.refresh_hms_lite(session)
                self._session = session
        return session

    def save_session(self, path: str) -> None:
        """把当前会话（Token 等）保存到 JSON 文件，便于下次免密恢复。"""
        session = self._require_session()
        data = {
            "account": session.account,
            "user_id": session.user_id,
            "service_token": session.service_token,
            "hms_access_token": session.hms_access_token,
            "hms_refresh_token": session.hms_refresh_token,
            "hms_expires_at": _dt_iso(session.hms_expires_at),
            "oauth_access_token": session.oauth_access_token,
            "oauth_expires_at": _dt_iso(session.oauth_expires_at),
            "device_id": session.device_id,
            "device_name": session.device_name,
            "pushtmid": session.pushtmid,
            "app_id": session.app_id,
            "base_url": session.base_url,
            "home_zone": session.home_zone,
            "oauth_domain": session.oauth_domain,
            "site_domain": session.site_domain,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def load_session(self, path: str) -> bool:
        """从 JSON 文件恢复会话。返回是否成功恢复且 token 仍可能有效。"""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False
        session = AuthSession(
            account=data.get("account", ""),
            user_id=data["user_id"],
            service_token=data["service_token"],
            hms_access_token=data.get("hms_access_token"),
            hms_refresh_token=data.get("hms_refresh_token"),
            hms_expires_at=_dt_parse(data.get("hms_expires_at")),
            oauth_access_token=data.get("oauth_access_token"),
            oauth_expires_at=_dt_parse(data.get("oauth_expires_at")),
            device_id=data.get("device_id", self.device_id),
            device_name=data.get("device_name", self.device_name),
            pushtmid=data.get("pushtmid", self.pushtmid),
            app_id=data.get("app_id", "com.huawei.smarthome-ios"),
            base_url=data.get("base_url", "https://smarthome.hicloud.com"),
            home_zone=data.get("home_zone"),
            oauth_domain=data.get("oauth_domain"),
            site_domain=data.get("site_domain"),
        )
        self._session = session
        return True

    # ============ 设备查询 ============
    def get_snapshot(self) -> DiscoverySnapshot:
        session = self.ensure_valid()
        return self._api.get_snapshot(session)

    def get_dynamic_states(
        self, device_ids: list[str], home_id: str | None = None
    ) -> list[DeviceState]:
        session = self.ensure_valid()
        return self._api.get_dynamic_states(session, device_ids, home_id)

    def get_device_detail(self, dev_id: str) -> Device | None:
        session = self.ensure_valid()
        return self._api.get_device_detail(session, dev_id)

    # ============ 智能场景 ============
    def get_scenes(self, home_id: str) -> list[Scene]:
        session = self.ensure_valid()
        return self._api.get_scenes(session, home_id)

    def execute_scene(self, scene_id: str, home_id: str) -> bool:
        session = self.ensure_valid()
        return self._api.execute_scene(session, scene_id, home_id)

    # ============ MQTT 控制 ============
    def connect_mqtt(self) -> None:
        """登录消息中心 + 解析云端路由 + 建立 MQTT 连接。"""
        session = self.ensure_valid()
        self._message_session = self._api.login_message_center(session)
        self._route = self._api.select_cloud_route(session)
        self._mqtt.connect(session, self._route)

    @property
    def is_mqtt_connected(self) -> bool:
        return self._mqtt.is_connected

    def send_command(
        self,
        dev_id: str,
        service_id: str,
        data: Mapping[str, Any],
        *,
        topic: str | None = None,
        target: str | None = None,
        method: str | None = None,
        raw_body: Mapping[str, Any] | None = None,
    ) -> str:
        """下发设备控制命令，返回 requestId。

        topic/target/method/raw_body 为高级覆盖项，用于协议调试；
        默认（均为 None）即使用校准后的标准信封格式。
        """
        return self._mqtt.publish_command(
            dev_id,
            service_id,
            data,
            topic=topic,
            target=target,
            method=method,
            raw_body=raw_body,
        )

    def disconnect_mqtt(self) -> None:
        self._mqtt.disconnect()

    # ============ 内部 ============
    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        if self._on_message is None:
            return
        try:
            parsed = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"raw": payload.decode("utf-8", errors="replace")}
        try:
            self._on_message(topic, parsed)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("处理 MQTT 消息回调时出错")


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
