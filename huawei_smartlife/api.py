"""智慧生活 REST API 客户端（非官方实现）。

所有接口使用 HMS-lite token 鉴权（Authorization: Bearer <token>），并携带
华为智慧生活 App 的私有请求头。响应结构参考社区逆向成果。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .constants import (
    CLOUD_ROUTE_SELECT_PATH,
    DEVICE_DETAIL_PATH,
    DEVICE_DYNAMIC_DATA_PATH,
    DEVICE_SNAPSHOT_PATH,
    HOME_SNAPSHOT_PATH,
    MESSAGE_CENTER_LOGIN_PATH,
    SCENE_EXECUTE_PATH,
    SCENE_LIST_PATH,
    SMART_HOME_APP_VERSION,
    SMART_HOME_BASE_URL,
    SMART_HOME_IOS_APP_ID,
    SMART_HOME_USER_AGENT,
)
from .exceptions import (
    AuthExpiredError,
    InvalidResponseError,
    RemoteOperationError,
    TransientNetworkError,
)
from .models import (
    AuthSession,
    Device,
    DeviceState,
    DiscoverySnapshot,
    Home,
    MessageChannelSession,
    Scene,
    ServiceState,
    SmartHomeCloudRoute,
)

_LOGGER = logging.getLogger(__name__)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _normalize_online(value: Mapping[str, Any]) -> bool | None:
    online = value.get("online")
    if isinstance(online, bool):
        return online
    status = _text(value.get("status"))
    if status == "online":
        return True
    if status == "offline":
        return False
    return None


def _normalize_service(value: Any) -> ServiceState | None:
    if not isinstance(value, Mapping):
        return None
    sid = _text(value.get("sid"))
    if not sid:
        return None
    data_raw = value.get("data")
    data: dict[str, Any] = {}
    if isinstance(data_raw, Mapping):
        for k, v in data_raw.items():
            if isinstance(k, (str, int)):
                data[str(k)] = v
    return ServiceState(
        sid=sid,
        service_type=_text(value.get("st")),
        data=data,
        reported_timestamp=_text(value.get("ts")),
    )


def _normalize_device(item: Mapping[str, Any]) -> Device | None:
    dev_id = _text(item.get("devId"))
    if not dev_id:
        return None
    info = item.get("devInfo")
    info = info if isinstance(info, Mapping) else {}
    name = (
        _text(item.get("devName"))
        or _text(info.get("deviceName"))
        or _text(info.get("model"))
        or "华为设备"
    )
    services: dict[str, ServiceState] = {}
    services_raw = item.get("services")
    if isinstance(services_raw, list):
        for svc in services_raw:
            ns = _normalize_service(svc)
            if ns is not None:
                services[ns.sid] = ns
    return Device(
        home_id=str(item.get("homeId") or "__unassigned__"),
        dev_id=dev_id,
        name=name,
        room_id=_text(item.get("roomId")),
        room_name=_text(item.get("roomName")),
        gateway_id=_text(item.get("gatewayId")),
        node_type=item.get("nodeType"),
        prod_id=_text(info.get("prodId")),
        model=_text(info.get("model")),
        manufacturer=_text(info.get("manufacturerName")),
        firmware_version=_text(info.get("fwv")),
        online=_normalize_online(item),
        services=services,
    )


def _normalize_homes(payload: Mapping[str, Any]) -> list[Home]:
    house_infos = payload.get("houseInfos")
    if not isinstance(house_infos, list):
        raise InvalidResponseError("家庭响应缺少 houseInfos 列表")
    homes: list[Home] = []
    for item in house_infos:
        if not isinstance(item, Mapping):
            continue
        home_id = _text(item.get("homeId"))
        if not home_id:
            continue
        homes.append(
            Home(
                home_id=home_id,
                name=_text(item.get("name")) or "华为家庭",
                role=_text(item.get("role")),
            )
        )
    return homes


def normalize_snapshot(
    devices_payload: Any, homes_payload: Any
) -> DiscoverySnapshot:
    if not isinstance(devices_payload, list):
        raise InvalidResponseError("设备响应不是 JSON 列表")
    if not isinstance(homes_payload, Mapping):
        raise InvalidResponseError("家庭响应不是 JSON 对象")
    homes = _normalize_homes(homes_payload)
    devices: list[Device] = []
    for item in devices_payload:
        if not isinstance(item, Mapping):
            continue
        dev = _normalize_device(item)
        if dev is not None:
            devices.append(dev)
    device_home_index: dict[str, list[str]] = {}
    for dev in devices:
        device_home_index.setdefault(dev.dev_id, []).append(dev.home_id)
    return DiscoverySnapshot(
        homes=tuple(homes),
        devices=tuple(devices),
        device_home_index=device_home_index,
    )


def normalize_dynamic_states(payload: Any) -> list[DeviceState]:
    if not isinstance(payload, list):
        raise InvalidResponseError("动态状态响应不是列表")
    states: list[DeviceState] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        dev_id = _text(item.get("devId"))
        if not dev_id:
            continue
        services: dict[str, ServiceState] = {}
        for svc in item.get("services") or []:
            ns = _normalize_service(svc)
            if ns is not None:
                services[ns.sid] = ns
        states.append(
            DeviceState(
                dev_id=dev_id,
                online=_normalize_online(item),
                services=services,
            )
        )
    return states


def _find_device_object(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        if value.get("devId"):
            return value
        for key in ("device", "deviceInfo", "data", "result"):
            found = _find_device_object(value.get(key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_device_object(child)
            if found is not None:
                return found
    return None


def normalize_device_detail(payload: Any) -> Device | None:
    candidate = _find_device_object(payload)
    if candidate is None:
        raise InvalidResponseError("设备详情中找不到设备对象")
    return _normalize_device(candidate)


class HuaweiApi:
    """智慧生活 REST API 客户端（同步，调用方在后台线程运行）。"""

    def __init__(self, *, base_url: str | None = None, timeout: float = 20.0) -> None:
        self._base_url = base_url or SMART_HOME_BASE_URL
        self._timeout = timeout

    def _request(
        self,
        session: AuthSession,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if not session.hms_access_token:
            raise AuthExpiredError("HMS-lite token 不可用")
        headers: dict[str, str] = {
            "Accept": "*/*",
            "Accept-Language": "zh-Hans-CN;q=1",
            "Authorization": f"Bearer {session.hms_access_token}",
            "X-AppId": SMART_HOME_IOS_APP_ID,
            "X-AppVersion": SMART_HOME_APP_VERSION,
            "X-HomeId": "(null)",
            "X-Huid": session.user_id,
            "X-Language": "zh_CN",
            "X-Model": "iPhone",
            "X-Ori-App-Name": SMART_HOME_IOS_APP_ID,
            "X-PhoneOs": "iOS",
            "X-RequestId": str(uuid.uuid4()).upper(),
            "User-Agent": SMART_HOME_USER_AGENT,
        }
        if extra_headers:
            headers.update(extra_headers)
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        url = f"{self._base_url.rstrip('/')}{path}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()
        except (URLError, TimeoutError, OSError) as error:
            raise TransientNetworkError("智慧生活 API 传输失败") from error

    @staticmethod
    def _json(body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidResponseError("智慧生活响应不是合法 JSON") from error

    def get_snapshot(self, session: AuthSession) -> DiscoverySnapshot:
        _, devices_body = self._request(session, DEVICE_SNAPSHOT_PATH)
        _, homes_body = self._request(session, HOME_SNAPSHOT_PATH)
        return normalize_snapshot(self._json(devices_body), self._json(homes_body))

    def get_dynamic_states(
        self,
        session: AuthSession,
        device_ids: list[str],
        home_id: str | None = None,
    ) -> list[DeviceState]:
        ids = list(dict.fromkeys(d for d in device_ids if d))
        if not ids:
            return []
        body = json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        extra: dict[str, str] = {"Content-Type": "application/json"}
        if home_id:
            extra["X-HomeId"] = home_id
        _, resp = self._request(
            session, DEVICE_DYNAMIC_DATA_PATH, method="POST", body=body, extra_headers=extra
        )
        return normalize_dynamic_states(self._json(resp))

    def get_device_detail(self, session: AuthSession, dev_id: str) -> Device | None:
        _, resp = self._request(
            session, DEVICE_DETAIL_PATH.format(dev_id=quote(dev_id, safe=""))
        )
        return normalize_device_detail(self._json(resp))

    def select_cloud_route(self, session: AuthSession) -> SmartHomeCloudRoute:
        body = json.dumps(
            {"version": SMART_HOME_APP_VERSION, "huid": session.user_id},
            separators=(",", ":"),
        ).encode("utf-8")
        _, resp = self._request(
            session,
            CLOUD_ROUTE_SELECT_PATH,
            method="POST",
            body=body,
            extra_headers={
                "huid": session.user_id,
                "version": SMART_HOME_APP_VERSION,
            },
        )
        value = self._json(resp)
        host = value.get("smarthomehost")
        mqtt_port = value.get("mqtts")
        https_port = value.get("https")
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(mqtt_port, int)
            or mqtt_port <= 0
            or not isinstance(https_port, int)
            or https_port <= 0
        ):
            raise InvalidResponseError("云端路由响应不完整")
        return SmartHomeCloudRoute(
            smart_home_host=host,
            mqtt_port=mqtt_port,
            https_port=https_port,
            expires_seconds=value.get("expires") if isinstance(value.get("expires"), int) else None,
            role=value.get("role") if isinstance(value.get("role"), int) else None,
        )

    def login_message_center(self, session: AuthSession) -> MessageChannelSession:
        if not session.device_id or not session.pushtmid:
            raise InvalidResponseError("缺少消息中心身份标识")
        if len(session.pushtmid) != 64:
            raise InvalidResponseError("pushtmid 必须是 64 位十六进制")
        try:
            int(session.pushtmid, 16)
        except ValueError as error:
            raise InvalidResponseError("pushtmid 不是有效十六进制") from error
        body = json.dumps(
            {
                "phoneos": "iOS",
                "language": "zh_CN",
                "deviceInfo": {
                    "deviceID": session.device_id,
                    "terminalType": "iphone",
                    "deviceAliasName": session.device_name,
                    "deviceType": "0",
                },
                "pushtmid": session.pushtmid,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _, resp = self._request(
            session, MESSAGE_CENTER_LOGIN_PATH, method="POST", body=body
        )
        value = self._json(resp)
        client_id = value.get("mqttClientId")
        mqtt_topic = value.get("mqttTopic")
        if not isinstance(client_id, str) or not client_id:
            raise InvalidResponseError("消息中心响应缺少 client id")
        if not isinstance(mqtt_topic, str) or not mqtt_topic:
            raise InvalidResponseError("消息中心响应缺少 topic")
        return MessageChannelSession(
            mqtt_client_id=client_id,
            mqtt_topic=mqtt_topic,
            first_login=value.get("firstLogin")
            if isinstance(value.get("firstLogin"), bool)
            else None,
            log_collection=value.get("logcollection")
            if isinstance(value.get("logcollection"), str)
            else None,
        )

    # ============ 智能场景 ============
    def get_scenes(
        self, session: AuthSession, home_id: str
    ) -> list[Scene]:
        """列出某家庭下的智能场景。

        端点基于社区逆向推断（见 constants.SCENE_LIST_PATH），实际结构可能变化。
        """
        if not home_id:
            raise InvalidResponseError("列出场景需要 home_id")
        extra = {"X-HomeId": home_id}
        _, resp = self._request(
            session, SCENE_LIST_PATH, method="GET", extra_headers=extra
        )
        value = self._json(resp)
        scenes_payload: Any = value
        if isinstance(value, Mapping):
            # 兼容 {"scenes": [...]} / {"sceneInfos": [...]} / {"data": [...]} 等
            for key in ("scenes", "sceneInfos", "sceneList", "data", "result"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    scenes_payload = candidate
                    break
        return normalize_scenes(scenes_payload, home_id)

    def execute_scene(self, session: AuthSession, scene_id: str, home_id: str) -> bool:
        """执行一个智能场景。返回是否成功下发。"""
        if not scene_id:
            raise InvalidResponseError("执行场景需要 scene_id")
        body = json.dumps(
            {"sceneId": scene_id}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        extra = {"Content-Type": "application/json", "X-HomeId": home_id}
        status, resp = self._request(
            session, SCENE_EXECUTE_PATH, method="POST", body=body, extra_headers=extra
        )
        if status >= 400:
            raise RemoteOperationError(f"执行场景失败，HTTP {status}: {resp.decode('utf-8', errors='replace')}")
        try:
            value = self._json(resp)
        except InvalidResponseError:
            return True
        if isinstance(value, Mapping):
            code = value.get("code")
            if code is not None and int(code) != 0:
                raise RemoteOperationError(
                    f"执行场景返回业务错误 code={code} msg={value.get('msg')}"
                )
        return True


def normalize_scenes(payload: Any, home_id: str) -> list[Scene]:
    if not isinstance(payload, list):
        return []
    scenes: list[Scene] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        scene_id = _text(item.get("sceneId")) or _text(item.get("id"))
        if not scene_id:
            continue
        name = (
            _text(item.get("sceneName"))
            or _text(item.get("name"))
            or _text(item.get("sceneNameZh"))
            or "未命名场景"
        )
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            enabled = item.get("isOn")
            if not isinstance(enabled, bool):
                enabled = None
        scenes.append(
            Scene(
                scene_id=scene_id,
                name=name,
                home_id=home_id,
                icon=_text(item.get("icon")),
                enabled=enabled,
                description=_text(item.get("description")),
            )
        )
    return scenes
