"""华为智慧生活设备控制（非官方 MQTT 实现）。

设备控制通过 MQTT over TLS（端口 8883）下发命令信封：
  - broker：由云端路由选择接口返回（smarthomehost）
  - 认证：username = user_id，password = oauth_access_token
  - 订阅：/smartHome/signaltrans/v2/categories/command（遥测与命令响应同一通道）

命令信封格式经社区实现（ha-huawei-smarthome）与真机旁听双重校准：
  - topic  = /smartHome/signaltrans/v2/categories/command（裸主题，不带设备 ID）
  - header.to    = /devices/{dev_id}/services/{service_id}（URI 寻址，非裸 ID）
  - header.from  = /users/{user_id}
  - header.method = POST，header.mode = ACK
  - body = 纯数据字典（如 {"on": 1}）；服务由 to 的 URI 指定，无需 serviceId 包装
设备响应以 commandRsp 通知回到同一通道：header.from 等于命令的 to，
body.errcode 为 0 表示设备接受，非 0 表示被拒绝。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import paho.mqtt.client as mqtt

from .constants import (
    MQTT_CAPTURE_FILTERS,
    MQTT_FILTER,
    MQTT_SUBSCRIPTION_QOS,
)
from .exceptions import MqttConfigurationError, MqttConnectionError
from .models import AuthSession, MqttConnectionSettings, SmartHomeCloudRoute

_LOGGER = logging.getLogger(__name__)

MqttMessageHandler = Callable[[str, bytes], None]
CommandResponseHandler = Callable[[str, str, object], None]


def _reason_code_value(reason_code: object) -> int:
    """把 on_connect/on_disconnect 的 reason_code 归一成 int。

    paho-mqtt 1.x 直接给 int；2.x 给 ReasonCode 对象（带 .value），
    直接 int(ReasonCode) 会 TypeError，故这里兼容两种。
    """
    if reason_code is None:
        return -1
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class HuaweiMqttController:
    """华为智慧生活 MQTT 控制器（同步，调用方在后台线程运行）。"""

    def __init__(
        self,
        *,
        connect_timeout: float = 20.0,
        on_message: MqttMessageHandler | None = None,
        on_command_response: CommandResponseHandler | None = None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._on_message = on_message
        self._on_command_response = on_command_response
        self._client: mqtt.Client | None = None
        self._settings: MqttConnectionSettings | None = None
        self._connected = False
        self._connect_event = threading.Event()
        self._connect_error: BaseException | None = None
        self._loop_thread: threading.Thread | None = None
        # 待确认命令：to(URI) -> requestId。commandRsp 的 header.from 会等于这里的 to。
        self._pending_commands: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def connect(self, session: AuthSession, route: SmartHomeCloudRoute) -> None:
        """使用云端路由与鉴权信息连接 MQTT Broker。"""
        if not route.smart_home_host or route.mqtt_port <= 0:
            raise MqttConfigurationError("MQTT Broker 地址不完整")
        settings = MqttConnectionSettings(
            broker_host=route.smart_home_host,
            broker_port=route.mqtt_port,
            client_id=str(uuid.uuid4()).upper(),
            subscription_filters=(MQTT_FILTER,),
            subscription_qos=MQTT_SUBSCRIPTION_QOS,
            username=session.user_id,
            password=session.oauth_access_token,
            control_source=session.device_name,
            use_tls=True,
            keepalive=60,
        )
        self._settings = settings
        self._connect_event.clear()
        self._connect_error = None
        try:
            client = mqtt.Client(
                client_id=settings.client_id,
                protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )
            client.username_pw_set(settings.username, settings.password)
            if settings.use_tls:
                client.tls_set()
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message_wrapper
            client.connect_async(
                settings.broker_host, settings.broker_port, settings.keepalive
            )
            client.loop_start()
            self._client = client
        except Exception as error:
            raise MqttConnectionError(f"MQTT 客户端创建失败: {error}") from error

        if not self._connect_event.wait(timeout=self._connect_timeout):
            self.disconnect()
            raise MqttConnectionError("MQTT 连接超时")
        if self._connect_error is not None:
            self.disconnect()
            raise MqttConnectionError(f"MQTT 连接被拒绝: {self._connect_error}")

        # 订阅命令/状态通道
        try:
            for flt in settings.subscription_filters:
                result, _mid = client.subscribe(flt, settings.subscription_qos)
                if result != mqtt.MQTT_ERR_SUCCESS:
                    raise MqttConnectionError(f"MQTT 订阅失败: {flt}")
        except MqttConnectionError:
            self.disconnect()
            raise

        # 旁听订阅（校准命令格式用）：通配捕获 signaltrans 下的真实命令。
        # subscribe() 只是把 SUBSCRIBE 包入队，broker 若拒绝会异步体现在 on_subscribe，
        # 不会影响已建立的主连接与遥测推送，故此处失败仅告警。
        for flt in MQTT_CAPTURE_FILTERS:
            try:
                client.subscribe(flt, settings.subscription_qos)
                _LOGGER.info("已加入旁听订阅: %s", flt)
            except Exception as error:  # noqa: BLE001
                _LOGGER.warning("旁听订阅入队失败(忽略): %s: %s", flt, error)

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        rc = _reason_code_value(reason_code)
        if rc == 0:
            self._connected = True
            self._connect_error = None
        else:
            self._connected = False
            self._connect_error = RuntimeError(f"CONNACK reason={rc}")
        self._connect_event.set()

    def _on_disconnect(self, _client, _userdata, *_args) -> None:
        self._connected = False

    def _on_message_wrapper(self, _client, _userdata, message) -> None:
        topic = getattr(message, "topic", "")
        payload = getattr(message, "payload", b"")
        if isinstance(topic, str) and isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload)
            # 先做命令响应关联（commandRsp），再透传给上层处理器
            self._maybe_correlate_command(raw)
            if self._on_message is not None:
                try:
                    self._on_message(topic, raw)
                except Exception:  # noqa: BLE001 - 防止回调异常影响网络循环
                    _LOGGER.exception("处理 MQTT 消息时出错")

    def _maybe_correlate_command(self, payload: bytes) -> None:
        """命中待确认命令的 commandRsp 时触发 on_command_response 回调。

        响应判定（与社区实现一致）：header.notifyType == "commandRsp" 且
        header.from 等于某条待确认命令的 to；body.errcode == 0 表示设备接受。
        """
        if not self._pending_commands or self._on_command_response is None:
            return
        try:
            value = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        header = value.get("header")
        body = value.get("body")
        if not isinstance(header, dict) or not isinstance(body, dict):
            return
        if header.get("notifyType") != "commandRsp":
            return
        target = header.get("from")
        if not isinstance(target, str):
            return
        request_id = self._pending_commands.pop(target, None)
        if request_id is None:
            return
        try:
            self._on_command_response(request_id, target, body.get("errcode"))
        except Exception:  # noqa: BLE001 - 防止回调异常影响网络循环
            _LOGGER.exception("处理命令响应回调时出错")

    def publish_command(
        self,
        dev_id: str,
        service_id: str,
        data: Mapping[str, Any],
        *,
        topic: str | None = None,
        target: str | None = None,
        gateway_id: str | None = None,
        method: str | None = None,
        raw_body: Mapping[str, Any] | None = None,
        qos: int = 1,
    ) -> str:
        """下发一条设备控制命令，返回 requestId。

        信封格式（社区实现 + 真机旁听双重校准，详见模块文档）：
          topic  = /smartHome/signaltrans/v2/categories/command（裸主题）
          to     = /devices/{dev_id}/services/{service_id}
          method = POST，mode = ACK
          body   = 纯数据字典，如 {"on": 1}
        命令响应以 commandRsp 回到同一通道：header.from 等于本命令的 to，
        body.errcode 为 0 表示设备已接受。

        dev_id:       设备 ID
        service_id:   服务 ID（如 "switch"、"brightness" 等）
        data:         该服务下要写入的属性，例如 {"on": 1}
        topic:        命令发布主题（默认裸 command 主题，可被抓包结果覆盖）
        target:       header.to 目标（默认 /devices/{dev_id}/services/{service_id}）
        gateway_id:   网关 ID（预留，部分子设备路由需要）
        method:       header.method（默认 POST）
        raw_body:     直接覆盖整个 body（高级调试用），提供时忽略 data
        qos:          MQTT 发布 QoS（默认 1）
        """
        if not self.is_connected or self._client is None or self._settings is None:
            raise MqttConnectionError("MQTT 未连接")
        if not dev_id or not service_id:
            raise MqttConfigurationError("设备 ID 与服务 ID 不能为空")

        request_id = str(uuid.uuid4()).upper()
        cmd_topic = topic or MQTT_FILTER
        to = target or f"/devices/{dev_id}/services/{service_id}"
        if raw_body is not None:
            command_body = dict(raw_body)
        else:
            # body 是纯数据字典；协议层可能残留的 requestId 由服务端忽略，此处剔除
            command_body = {
                key: value for key, value in dict(data).items() if key != "requestId"
            }
        payload = {
            "body": command_body,
            "header": {
                "accessToken": self._settings.password,
                "ctrlSrc": self._settings.control_source,
                "ctrlSrcType": 2,
                "ctrlType": 1,
                "from": f"/users/{self._settings.username}",
                "method": method or "POST",
                "mode": "ACK",
                "requestId": request_id,
                "seq": -1,
                "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "to": to,
            },
            "seq": -1,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        # 登记待确认命令：响应的 header.from 会等于这里的 to
        self._pending_commands[to] = request_id
        info = self._client.publish(cmd_topic, payload=body, qos=qos)
        try:
            info.wait_for_publish(timeout=self._connect_timeout)
        except (TypeError, ValueError):
            info.wait_for_publish()
        if not info.is_published():
            self._pending_commands.pop(to, None)
            raise MqttConnectionError("MQTT 命令发布超时")
        _LOGGER.info(
            "已下发命令 topic=%s to=%s method=%s body=%s requestId=%s",
            cmd_topic,
            to,
            method or "POST",
            command_body,
            request_id,
        )
        return request_id

    def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        self._pending_commands.clear()
        if client is not None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()
