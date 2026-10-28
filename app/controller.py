"""应用控制器：把协议客户端、数据层与 UI 粘合起来。

职责
----
* 登录 / 二次验证 / 会话恢复与持久化
* 快照与实时状态的拉取、合并（在后台线程执行，结果回主线程）
* 设备命令下发、MQTT 连接维护
* 把异常翻译成用户能看懂的中文提示
* 通过 Qt 信号把「发生了什么」广播给 UI，UI 只管显示

UI 不直接调用网络接口，控制器也不碰任何控件——两边通过信号解耦。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

from PySide6.QtCore import QObject, Signal

from huawei_smartlife import (
    HuaweiSmartLifeClient,
    HuaweiSmartLifeError,
)
from huawei_smartlife.exceptions import (
    AuthenticationError,
    AuthExpiredError,
    InvalidResponseError,
    MqttConnectionError,
    TransientNetworkError,
)
from huawei_smartlife.models import (
    CHALLENGE_KIND_SMS,
    DeviceState,
    LoginChallenge,
    LoginStart,
    Scene,
)

from .dispatch import post
from .state import DeviceStore
from .telemetry_text import describe_query, describe_telemetry
from .workers import run_async

_LOGGER = logging.getLogger(__name__)

APP_DIR = os.path.join(os.path.expanduser("~"), ".huawei_smartlife")
SESSION_PATH = os.path.join(APP_DIR, "session.json")
UI_SETTINGS_PATH = os.path.join(APP_DIR, "ui_settings.json")


# 路由器信息里会带 WiFi 密码明文，原始协议模式必须打码。
# 排查协议不需要看密码，但日志一旦被截图/贴出去就是真泄露。
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "wpapresharedkey",
        "wpapresharedkeytemp",
        "wepkey",
        "password",
        "passwd",
        "pwd",
        "token",
        "accesstoken",
        "refreshtoken",
    }
)


def _mask_sensitive(value: Any) -> Any:
    """递归把敏感字段的值替换成 ***。"""
    if isinstance(value, Mapping):
        return {
            key: (
                "***"
                if str(key).strip().lower() in _SENSITIVE_KEYS
                else _mask_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_sensitive(item) for item in value]
    return value


def _pretty_json(payload: Any) -> str:
    """原始协议显示用：缩进两格的 JSON（敏感字段已打码）。"""
    try:
        return json.dumps(_mask_sensitive(payload), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(payload)


def friendly_error(error: BaseException) -> str:
    """把异常翻译成用户能看懂的中文。"""
    if isinstance(error, TransientNetworkError):
        return "网络连接失败，请检查网络后重试。"
    if isinstance(error, AuthExpiredError):
        return "登录状态已过期，请重新登录。"
    if isinstance(error, MqttConnectionError):
        return f"实时通道异常：{error}"
    if isinstance(error, InvalidResponseError):
        return f"云端返回的数据无法解析：{error}"
    if isinstance(error, AuthenticationError):
        text = str(error)
        return text or "认证失败，请检查账号或验证码。"
    if isinstance(error, HuaweiSmartLifeError):
        return str(error) or "操作失败。"
    if isinstance(error, (TimeoutError, OSError)):
        return "网络请求超时，请稍后重试。"
    return str(error) or error.__class__.__name__


class AppController(QObject):
    """全局唯一的业务控制器。"""

    # ---- 登录 ----
    login_started = Signal()
    login_succeeded = Signal(str)            # account
    login_failed = Signal(str)               # message
    challenge_required = Signal(object)      # LoginChallenge
    logged_out = Signal(str)                 # reason
    # ---- 数据 ----
    snapshot_loaded = Signal(int, int)       # device_count, home_count
    device_changed = Signal(str)             # dev_id
    devices_reloaded = Signal()              # 卡片需要整体重建
    scenes_loaded = Signal(list)             # list[Scene]
    # ---- 连接 ----
    mqtt_state_changed = Signal(bool)
    busy_changed = Signal(bool, str)         # busy, message
    # ---- 消息 ----
    log = Signal(str, str)                   # message, kind
    notify = Signal(str, str)                # message, kind

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = DeviceStore()
        self._client: HuaweiSmartLifeClient | None = None
        self._pending_challenge: LoginChallenge | None = None
        # 登录页「记住登录状态」开关。False 时登录成功不落盘，并清掉旧会话，
        # 否则下次启动 _bootstrap 还能免密进来，开关等于摆设。
        self._remember_session = True
        self._account: str = ""
        self._mqtt_connected = False
        # 协议调试：订阅 signaltrans/# 通配，能收到账号下所有客户端的消息。
        # 默认关闭（由主窗口按 UI 设置注入），开启后日志会明显变吵。
        self._capture_enabled = False
        # 运行日志输出原始 MQTT 报文（而非中文摘要），排查协议时开
        self._raw_protocol = False
        self._scenes: list[Scene] = []
        self._busy_count = 0
        self._build_client()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _build_client(self) -> None:
        self._client = HuaweiSmartLifeClient(
            on_message=self._on_mqtt_message,
            on_command_response=self._on_command_response,
        )

    @property
    def client(self) -> HuaweiSmartLifeClient:
        if self._client is None:
            self._build_client()
        assert self._client is not None
        return self._client

    @property
    def account(self) -> str:
        return self._account

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_connected

    @property
    def scenes(self) -> list[Scene]:
        return list(self._scenes)

    @property
    def pending_challenge(self) -> LoginChallenge | None:
        return self._pending_challenge

    # ------------------------------------------------------------------
    # 信号发射：统一投递到主线程
    # ------------------------------------------------------------------
    def _emit(self, signal, *args) -> None:
        """线程安全的信号发射。

        controller 的很多信号来自工作线程（登录、刷新）或 paho 网络线程
        （MQTT 遥测），而接收者是 Qt 控件。这里统一投递到主线程再 emit，
        避免跨线程直接操作 UI。
        """
        post(lambda: self._emit_now(signal, args))

    @staticmethod
    def _emit_now(signal, args: tuple) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            # 窗口已销毁，无人接收，丢弃
            pass

    # ------------------------------------------------------------------
    # 忙碌状态（用于按钮禁用 / 进度提示）
    # ------------------------------------------------------------------
    def _begin_busy(self, message: str) -> None:
        self._busy_count += 1
        if self._busy_count == 1:
            self._emit(self.busy_changed, True, message)

    def _end_busy(self) -> None:
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            self._emit(self.busy_changed, False, "")

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def restore_session(self) -> bool:
        """尝试用本地会话免密登录（会自动刷新 Token）。"""
        if not os.path.exists(SESSION_PATH):
            return False
        try:
            if not self.client.load_session(SESSION_PATH):
                return False
        except Exception as error:  # noqa: BLE001 - 会话文件损坏时按未登录处理
            self._emit(self.log, f"读取本地会话失败：{error}", "warning")
            return False

        try:
            session = self.client.ensure_valid()
        except Exception as error:  # noqa: BLE001 - Token 刷新失败 → 需重新登录
            self._emit(self.log, f"本地会话已失效：{friendly_error(error)}", "warning")
            self.clear_session()
            return False

        self._account = session.account or ""
        self._emit(self.log, "已恢复上次登录会话。", "info")
        self._emit(self.login_succeeded, self._account)
        return True

    def login(self, account: str, password: str, remember: bool = True) -> None:
        """账号密码登录。成功→login_succeeded；需要验证→challenge_required。"""
        self._remember_session = bool(remember)
        if not account or not password:
            self._emit(self.login_failed, "请输入账号和密码。")
            return

        self._begin_busy("正在登录…")
        self._emit(self.login_started, )
        self._emit(self.log, "正在登录华为账号…", "info")

        def work() -> LoginStart:
            return self.client.begin_login(account, password)

        def done(result: LoginStart) -> None:
            self._end_busy()
            self._account = account
            if result.session is not None:
                self._persist_session_if_needed()
                self._emit(self.login_succeeded, account)
                self._emit(self.log, "登录成功。", "success")
            elif result.challenge is not None:
                self._pending_challenge = result.challenge
                self._emit(self.challenge_required, result.challenge)
                self._emit(self.log, f"需要二次验证：{result.challenge.prompt}", "warning")
            else:
                self._emit(self.login_failed, "登录失败：云端未返回会话或验证要求。")

        def failed(error: BaseException) -> None:
            self._end_busy()
            message = friendly_error(error)
            self._emit(self.login_failed, message)
            self._emit(self.log, f"登录失败：{message}", "danger")

        run_async(work, done, failed)

    def switch_challenge_channel(self, channel) -> None:
        """界面换接收方式时同步后端状态。

        这是纯本地操作（不发网络请求），所以同步做完即可；也不能 emit
        ``challenge_required``，否则界面会重新渲染、把用户的输入打断。
        """
        if self._pending_challenge is None:
            return
        try:
            self._pending_challenge = self.client.switch_challenge_channel(channel)
        except Exception as error:  # noqa: BLE001 - 切换失败不该打断登录
            self._emit(self.log, f"切换接收方式失败：{friendly_error(error)}", "warning")

    def select_challenge_channel(self, channel) -> None:
        """请求在指定渠道下发验证码（短信 / 邮件 / 设备）。"""
        if self._pending_challenge is None:
            return

        def work() -> LoginChallenge:
            return self.client.select_challenge_channel(channel)

        def done(challenge: LoginChallenge) -> None:
            self._pending_challenge = challenge
            self._emit(self.challenge_required, challenge)
            self._emit(self.log, challenge.prompt, "info")
            notice = (
                "验证码已发送，请查收"
                if challenge.kind == CHALLENGE_KIND_SMS
                else challenge.prompt
            )
            self._emit(self.notify, notice, "info")

        def failed(error: BaseException) -> None:
            message = friendly_error(error)
            self._emit(self.log, f"发送验证码失败：{message}", "danger")
            self._emit(self.notify, message, "danger")

        run_async(work, done, failed)

    def submit_challenge(self, code: str) -> None:
        """提交短信 / 设备验证码完成登录。"""
        if not code.strip():
            self._emit(self.login_failed, "请输入验证码。")
            return

        self._begin_busy("正在验证…")

        def work():
            return self.client.complete_challenge(code.strip())

        def done(session) -> None:
            self._end_busy()
            self._pending_challenge = None
            self._account = session.account or self._account
            self._persist_session_if_needed()
            self._emit(self.login_succeeded, self._account)
            self._emit(self.log, "验证通过，登录成功。", "success")

        def failed(error: BaseException) -> None:
            self._end_busy()
            message = friendly_error(error)
            self._emit(self.login_failed, f"验证失败：{message}")
            self._emit(self.log, f"验证失败：{message}", "danger")

        run_async(work, done, failed)

    def logout(self) -> None:
        """断开 MQTT、清除本地会话。"""
        try:
            self.client.disconnect_mqtt()
        except Exception:  # noqa: BLE001 - 断开失败不影响登出
            pass
        self._set_mqtt(False)
        self.clear_session()
        self.store.clear()
        self._account = ""
        self._scenes = []
        self._build_client()
        self._emit(self.log, "已退出登录。", "info")
        self._emit(self.logged_out, "已退出登录")

    def _persist_session_if_needed(self) -> None:
        """按「记住登录状态」决定是否落盘。

        不记住时必须顺手删掉旧文件：用户可能上次是记住的，这次取消了，
        旧的 session.json 留着就会让下次启动照样免密进入。
        """
        if self._remember_session:
            self._persist_session()
            return
        self.clear_session()
        self._emit(self.log, "未勾选「记住登录状态」，本次不保存会话。", "info")

    def _persist_session(self) -> None:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            self.client.save_session(SESSION_PATH)
            self._emit(self.log, f"会话已保存：{SESSION_PATH}", "info")
        except Exception as error:  # noqa: BLE001 - 保存失败仅告警
            self._emit(self.log, f"保存会话失败：{error}", "warning")

    def clear_session(self) -> None:
        try:
            if os.path.exists(SESSION_PATH):
                os.remove(SESSION_PATH)
        except OSError as error:
            self._emit(self.log, f"删除会话文件失败：{error}", "warning")

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """拉取设备快照 + 批量实时状态。"""
        self._begin_busy("正在刷新设备…")

        def work():
            snapshot = self.client.get_snapshot()
            states: list[DeviceState] = []
            try:
                states = self.client.get_dynamic_states(
                    [device.dev_id for device in snapshot.devices]
                )
            except Exception as error:  # noqa: BLE001 - 实时状态失败不阻塞刷新
                self._emit(self.log, f"实时状态获取失败（已忽略）：{friendly_error(error)}", "warning")
            return snapshot, states

        def done(result) -> None:
            self._end_busy()
            snapshot, states = result
            self.store.replace_snapshot(snapshot, states)
            self._emit(self.snapshot_loaded, len(snapshot.devices), len(snapshot.homes))
            self._emit(self.devices_reloaded, )
            self._emit(self.log, 
                f"已加载 {len(snapshot.devices)} 个设备、{len(snapshot.homes)} 个家庭。",
                "success",
            )

        def failed(error: BaseException) -> None:
            self._end_busy()
            message = friendly_error(error)
            self._emit(self.log, f"刷新失败：{message}", "danger")
            self._emit(self.notify, message, "danger")

        run_async(work, done, failed)

    def refresh_device_state(self, dev_id: str) -> None:
        """补拉单个设备的实时状态（详情弹窗打开时用）。"""
        device = self.store.get(dev_id)
        if device is None:
            return

        def work():
            return self.client.get_dynamic_states([dev_id])

        def done(states: list[DeviceState]) -> None:
            state = next((s for s in states if s.dev_id == dev_id), None)
            if state is None:
                return
            if self.store.merge_dynamic_state(device, state):
                self._emit(self.device_changed, dev_id)

        def failed(error: BaseException) -> None:
            self._emit(self.log, f"获取 {device.name} 实时状态失败：{friendly_error(error)}", "warning")

        run_async(work, done, failed)

    # ------------------------------------------------------------------
    # 设备控制
    # ------------------------------------------------------------------
    def toggle_switch(self, dev_id: str, sid: str, key: str, current_on: bool) -> None:
        """切换主开关：当前为开则关，反之则开。"""
        self.send_command(dev_id, sid, {key: 0 if current_on else 1})

    def send_command(
        self,
        dev_id: str,
        sid: str,
        data: dict[str, Any],
        *,
        topic: str | None = None,
        target: str | None = None,
        method: str | None = None,
        raw_body: dict[str, Any] | None = None,
    ) -> None:
        """下发命令。成功后乐观更新本地状态，等待设备回传校正。"""
        device = self.store.get(dev_id)
        name = device.name if device else dev_id

        if not self._mqtt_connected:
            self._emit(self.notify, "实时通道未连接，请先点击「连接」", "warning")

        override = ""
        if topic or target or method or raw_body is not None:
            override = f"（覆盖 topic={topic} to={target} method={method}）"
        self._emit(self.log, f"下发命令 → {name} / {sid} {json.dumps(data, ensure_ascii=False)}{override}", "info")

        def work() -> str:
            return self.client.send_command(
                dev_id,
                sid,
                data,
                topic=topic,
                target=target,
                method=method,
                raw_body=raw_body,
            )

        def done(request_id: str) -> None:
            self._emit(self.log, f"命令已发出（requestId={request_id}），等待设备响应…", "info")
            # 乐观更新：让界面立刻反映意图（真实状态稍后由 commandRsp 校正）
            for key, value in data.items():
                self.store.mark_switch(dev_id, sid, key, value)
            self._emit(self.device_changed, dev_id)

        def failed(error: BaseException) -> None:
            message = friendly_error(error)
            self._emit(self.log, f"命令下发失败：{message}", "danger")
            self._emit(self.notify, message, "danger")

        run_async(work, done, failed)

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------
    def connect_mqtt(self, force: bool = False) -> None:
        if self._mqtt_connected and not force:
            return
        self._begin_busy("正在连接实时通道…")
        self._emit(self.log, "正在连接实时通道（MQTT）…", "info")

        def work():
            if force:
                # 改订阅范围只能重连；先断开旧连接，避免两个 loop 同时收消息
                try:
                    self.client.disconnect_mqtt()
                except Exception:  # noqa: BLE001 - 旧连接已经断了也没关系
                    pass
            self.client.connect_mqtt(capture=self._capture_enabled)
            return True

        def done(_result) -> None:
            self._end_busy()
            self._set_mqtt(True)
            self._emit(self.log, "实时通道已连接，开始接收设备状态推送。", "success")
            self._emit(self.notify, "实时通道已连接", "success")

        def failed(error: BaseException) -> None:
            self._end_busy()
            self._set_mqtt(False)
            message = friendly_error(error)
            self._emit(self.log, f"实时通道连接失败：{message}", "danger")
            self._emit(self.notify, "实时通道连接失败", "danger")

        run_async(work, done, failed)

    def set_raw_protocol(self, enabled: bool) -> None:
        """开关「日志显示原始协议报文」。只影响显示，不影响收发。"""
        enabled = bool(enabled)
        if enabled == self._raw_protocol:
            return
        self._raw_protocol = enabled
        self._emit(
            self.log,
            "已开启原始协议显示：日志将输出完整 MQTT 报文，不再翻译。"
            if enabled
            else "已关闭原始协议显示，日志恢复中文摘要。",
            "warning" if enabled else "info",
        )

    def set_capture_enabled(self, enabled: bool, reconnect: bool = True) -> None:
        """开关「协议调试订阅」。

        开启后额外订阅 ``signaltrans/#`` 通配主题，能收到**整个账号下所有客户端**
        的消息（包括手机端官方 App 发出的查询），排查协议时有用，但日常只会让
        运行日志变吵。订阅范围在连接时确定，所以要重连才生效。
        """
        enabled = bool(enabled)
        if enabled == self._capture_enabled:
            return
        self._capture_enabled = enabled
        if enabled:
            self._emit(
                self.log,
                "已开启协议调试订阅：将接收账号下所有 MQTT 消息（含官方 App 的查询）。",
                "warning",
            )
        else:
            self._emit(self.log, "已关闭协议调试订阅，只接收本机的命令响应与设备状态。", "info")
        if reconnect and self._mqtt_connected:
            self._emit(self.log, "实时通道将重新连接以应用新的订阅范围。", "info")
            self.connect_mqtt(force=True)

    def disconnect_mqtt(self) -> None:
        try:
            self.client.disconnect_mqtt()
        finally:
            self._set_mqtt(False)
            self._emit(self.log, "实时通道已断开。", "info")

    def _set_mqtt(self, connected: bool) -> None:
        if connected == self._mqtt_connected:
            return
        self._mqtt_connected = connected
        self._emit(self.mqtt_state_changed, connected)

    # ------------------------------------------------------------------
    # 场景
    # ------------------------------------------------------------------
    def load_scenes(self) -> None:
        homes = [home.home_id for home in self.store.homes]
        if not homes:
            self._emit(self.scenes_loaded, [])
            return

        def work():
            collected: list[Scene] = []
            for home_id in homes:
                try:
                    collected.extend(self.client.get_scenes(home_id))
                except Exception as error:  # noqa: BLE001 - 某个家庭失败不影响其它
                    self._emit(self.log, 
                        f"读取家庭场景失败（{home_id}）：{friendly_error(error)}", "warning"
                    )
            return collected

        def done(scenes: list[Scene]) -> None:
            self._scenes = scenes
            self._emit(self.scenes_loaded, scenes)
            self._emit(self.log, f"已加载 {len(scenes)} 个智能场景。", "info")

        def failed(error: BaseException) -> None:
            self._emit(self.log, f"加载场景失败：{friendly_error(error)}", "warning")
            self._emit(self.scenes_loaded, [])

        run_async(work, done, failed)

    def execute_scene(self, scene: Scene) -> None:
        home_id = scene.home_id or ""
        self._emit(self.log, f"执行场景：{scene.name}", "info")

        def work() -> bool:
            return self.client.execute_scene(scene.scene_id, home_id)

        def done(ok: bool) -> None:
            if ok:
                self._emit(self.notify, f"已执行场景「{scene.name}」", "success")
                self._emit(self.log, f"场景「{scene.name}」执行请求已发送。", "success")
            else:
                self._emit(self.notify, "场景执行失败", "danger")

        def failed(error: BaseException) -> None:
            message = friendly_error(error)
            self._emit(self.log, f"执行场景失败：{message}", "danger")
            self._emit(self.notify, message, "danger")

        run_async(work, done, failed)

    # ------------------------------------------------------------------
    # MQTT 回调（来自 paho 网络线程 → 只发信号，不碰 UI）
    # ------------------------------------------------------------------
    def _device_display_name(self, dev_id: str) -> str:
        """dev_id → 设备名；设备不在快照里时返回空串，让摘要退回短 ID。"""
        device = self.store.get(dev_id)
        return device.name if device is not None else ""

    def _on_mqtt_message(self, topic: str, payload: dict) -> None:
        header = payload.get("header") if isinstance(payload, dict) else None
        notify_type = header.get("notifyType") if isinstance(header, dict) else None

        # 信封原文只进 app.log（DEBUG 级），界面上用下面翻译好的中文摘要。
        # 一个功率计量设备 3 秒一条，全量 dump 到界面几小时就能刷到几十 MB，
        # 真正要看的错误反而被埋掉。想看原文时把日志级别调到 DEBUG 即可。
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                _LOGGER.debug("MQTT %s %s", topic, json.dumps(payload, ensure_ascii=False))
            except (TypeError, ValueError):
                pass

        if notify_type == "commandRsp":
            method = header.get("method") if isinstance(header, dict) else None
            from_uri = header.get("from") if isinstance(header, dict) else ""
            body = payload.get("body")
            size = len(body) if isinstance(body, (list, dict)) else 0
            if method == "GET":
                # 官方 App / 网关的查询响应（旁听到的），只留可读路径
                if self._raw_protocol:
                    self._emit(self.log, f"【查询】{_pretty_json(payload)}", "info")
                else:
                    self._emit(self.log, f"【查询】{describe_query(from_uri, size)}", "info")
            else:
                self._emit(self.log, f"【命令响应】{describe_query(from_uri, size)}", "info")
            return

        if notify_type == "deviceDataChanged":
            # 遥测记为 detail：界面默认折叠，勾选「显示遥测」才出现
            if self._raw_protocol:
                text = f"[遥测] {_pretty_json(payload)}"
            else:
                text = f"[遥测] {describe_telemetry(payload, self._device_display_name)}"
            self._emit(self.log, text, "detail")
            result = self.store.apply_telemetry(payload)
            if result is not None:
                self._emit(self.device_changed, result[0])
            return

        if notify_type == "deviceStatus":
            if self._raw_protocol:
                self._emit(self.log, f"[状态] {_pretty_json(payload)}", "tag")
            else:
                self._emit(self.log, f"[状态] {describe_query(topic, 0).replace('（0 项）', '')}", "tag")
            result = self.store.apply_device_status(payload)
            if result is not None and result[1]:
                self._emit(self.device_changed, result[0])
            return

        if self._raw_protocol:
            self._emit(self.log, f"[消息] {topic} {_pretty_json(payload)}", "tag")
        else:
            self._emit(self.log, f"[消息] {topic}", "tag")

    def _on_command_response(
        self,
        request_id: str,
        target: str,
        errcode,
        body: dict | None = None,
    ) -> None:
        if errcode == 0:
            self._emit(self.notify, "设备已接受命令", "success")
            self._emit(self.log, f"命令响应：设备接受（{target}，errcode=0）", "success")
            return

        # 被拒时把响应体原样带上：错误码本身说明不了是哪个字段不对，
        # 常见的 -12 是「参数不合法」，得看 body 里回了什么才知道缺什么。
        detail = ""
        if isinstance(body, dict):
            extra = {
                key: value
                for key, value in body.items()
                if key not in {"errcode", "errmessage", "errmsg"}
            }
            message = body.get("errmessage") or body.get("errmsg")
            if message:
                detail = f"：{message}"
            elif extra:
                detail = f"：{json.dumps(extra, ensure_ascii=False)}"
        self._emit(self.notify, f"设备拒绝了命令（errcode={errcode}）", "danger")
        self._emit(
            self.log,
            f"命令响应：设备拒绝（{target}，errcode={errcode}{detail}）",
            "danger",
        )
