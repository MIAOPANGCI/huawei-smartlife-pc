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
import time
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
from .device_spec import describe_timer_tasks
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


# 主动查询的登记在这么久之后自动失效（设备不回包时不至于一直挂着）
_QUERY_TTL = 30.0


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


def _split_service_uri(uri: object) -> tuple[str, str]:
    """``/devices/{dev_id}/services/{sid}`` → (dev_id, sid)。

    解析不出来时给两个空串，调用方据此退回「只记日志」的行为。
    """
    parts = [part for part in str(uri or "").strip("/").split("/") if part]
    if len(parts) >= 4 and parts[0] == "devices" and parts[2] == "services":
        return parts[1], parts[3]
    return "", ""


# 设备/网关错误码。只收确认过含义的，猜的一律不写进来——
# 错误提示写错比不写更误导。
_ERRCODE_HINTS: dict[int, str] = {
    408: "网关等设备回复超时",
    -1: "设备内部错误",
    -2: "设备不支持该操作",
    -3: "参数不合法",
    -12: "设备拒绝执行（多半是参数不符合该服务要求）",
}


def _error_code_of(body: object) -> int | None:
    """从回包里取 errcode，取不到给 None。"""
    if not isinstance(body, dict):
        return None
    code = body.get("errcode")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    try:
        return int(str(code)) if str(code).strip() else None
    except (TypeError, ValueError):
        return None


def _is_error_body(body: dict) -> bool:
    """回包是不是「读失败」的错误体（errcode 存在且非 0）。"""
    if "errcode" not in body:
        return False
    code = _error_code_of(body)
    return code is None or code != 0


def _summarize_service_data(sid: str, data: object) -> str:
    """把查询回来的服务数据压成一行中文，省得用户自己啃 JSON。"""
    if not isinstance(data, dict):
        return str(data)
    tasks = data.get("timer")
    if sid in {"timer", "delay"} and isinstance(tasks, list):
        return describe_timer_tasks(tasks)
    text = json.dumps(_mask_sensitive(data), ensure_ascii=False)
    return text if len(text) <= 220 else text[:220] + "…"


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
        # 主动查询登记：to(URI) -> 登记时刻。查询响应同样是 commandRsp，
        # 但语义是"这是你要的当前值"而不是"命令被接受"，得分开处理。
        self._pending_queries: dict[str, float] = {}
        # 旁听日志节流：官方 App / 云端会每 10 秒轮询一次网关信息，
        # 同一件事（同一 URI、同样结果）反复刷屏会把真正的操作记录埋掉。
        # key -> (上次输出时刻, 期间被合并的条数)
        self._log_throttle: dict[str, tuple[float, int]] = {}
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

    def _emit_throttled(
        self, key: str, text: str, level: str, window: float = 60.0
    ) -> None:
        """同类日志在窗口期内只输出一次，被合并的条数补在下一次输出里。

        专门用于「别人的流量」：官方 App / 云端每 10 秒轮询一次网关，
        成功失败各来一条，逐条记录的话几分钟就能把真正的操作记录顶出屏幕。
        自己的操作（主动查询、命令响应）不走这里，保证每次都有反馈。
        """
        now = time.monotonic()
        # 长期运行下 key 会按 URI 慢慢累积，超过阈值时清掉早已过期的项
        if len(self._log_throttle) > 256:
            cutoff = now - window * 5
            for key, (stamp, _count) in list(self._log_throttle.items()):
                if stamp < cutoff:
                    self._log_throttle.pop(key, None)
        last, merged = self._log_throttle.get(key, (0.0, 0))
        if last and now - last < window:
            self._log_throttle[key] = (last, merged + 1)
            return
        suffix = f"（期间 {merged} 次同类已合并）" if merged else ""
        self._log_throttle[key] = (now, 0)
        self._emit(self.log, f"{text}{suffix}", level)

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
        # 同一服务上接着下发普通命令时，取消之前的查询登记，
        # 否则这条命令的回包会被当成「查询返回」处理。
        self._pending_queries.pop(
            target or f"/devices/{dev_id}/services/{sid}", None
        )

        if not self._mqtt_connected:
            self._emit(self.notify, "实时通道未连接，请先点击「连接」", "warning")

        parts = []
        if topic:
            parts.append(f"topic={topic}")
        if target:
            parts.append(f"to={target}")
        if method:
            parts.append(f"method={method}")
        if raw_body is not None:
            parts.append("raw_body 覆盖")
        override = f"（{'，'.join(parts)}）" if parts else ""
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

    def query_service(self, dev_id: str, sid: str) -> None:
        """主动向设备读取某个服务的当前值（``method=GET``）。

        用处是「对账」：像 timer / delay 这种写进去回包含糊的服务，
        光看 commandRsp 的 errcode 判断不了到底写成功没有，
        必须再读一次设备真实保存的内容来比。
        """
        device = self.store.get(dev_id)
        name = device.name if device else dev_id
        target = f"/devices/{dev_id}/services/{sid}"
        if not self._mqtt_connected:
            self._emit(self.notify, "实时通道未连接，请先点击「连接」", "warning")
            self._emit(self.log, "查询未发出：实时通道未连接。", "warning")
            return
        self._prune_pending_queries()
        self._pending_queries[target] = time.monotonic()
        self._emit(self.log, f"查询服务 → {name} / {sid}（读取设备当前值）", "info")

        def work() -> str:
            return self.client.send_command(dev_id, sid, {}, method="GET")

        def done(_request_id: str) -> None:
            self._emit(self.log, f"查询已发出，等待设备回报 {sid} 的当前内容…", "info")

        def failed(error: BaseException) -> None:
            self._pending_queries.pop(target, None)
            message = friendly_error(error)
            self._emit(self.log, f"查询下发失败：{message}", "danger")

        run_async(work, done, failed)

    def _prune_pending_queries(self) -> None:
        """丢掉过期的查询登记。

        设备一直不回包时登记会永远留着，之后这个服务的普通命令响应就会被
        误判成查询响应、连「已接受」都不再提示。
        """
        deadline = time.monotonic() - _QUERY_TTL
        for key, stamp in list(self._pending_queries.items()):
            if stamp < deadline:
                self._pending_queries.pop(key, None)

    def _is_pending_query(self, target: str) -> bool:
        stamp = self._pending_queries.get(target)
        if stamp is None:
            return False
        if stamp < time.monotonic() - _QUERY_TTL:
            self._pending_queries.pop(target, None)
            return False
        return True

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
                self._handle_query_result(from_uri, body, payload, size)
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

    def _handle_query_result(
        self, from_uri: str, body: object, payload: dict, size: int
    ) -> None:
        """处理 method=GET 的回包（自己的查询 + 旁听到的别人查询）。

        两种回包要分开：``errcode`` 非 0 的是「读失败」（常见 408 = 网关等设备
        超时），body 里只有错误码，绝不能当成服务数据写进设备——
        否则 timer 的 data 里会多出一个莫名其妙的 errcode 字段
        （界面还会显示"已刷新"，看着像成功，其实是脏数据）。
        """
        dev_id, sid = _split_service_uri(from_uri)
        mine = self._pending_queries.pop(from_uri, None) is not None
        ok = isinstance(body, dict) and not _is_error_body(body)

        if self._raw_protocol:
            self._emit(self.log, f"【查询结果】{_pretty_json(payload)}", "info")
            if mine and ok and dev_id:
                if self.store.apply_service_data(dev_id, sid, body):
                    self._emit(self.device_changed, dev_id)
            return

        if not ok:
            code = _error_code_of(body)
            hint = _ERRCODE_HINTS.get(code, "")
            detail = ""
            if code is not None:
                detail = f"（errcode={code}" + (f"，{hint}" if hint else "") + "）"
            name = describe_query(from_uri, 0).replace("（0 项）", "")
            if name in {"查询结果", "查询"}:
                name = "设备"
            if mine:
                self._emit(self.log, f"【查询超时/失败】{name}{detail}", "warning")
            else:
                # 旁听：官方 App / 云端的定时轮询失败，与本地操作无关。
                # 一是别写成「查询超时/失败」让人以为是自己发的；
                # 二是降为 detail 级并按 URI 节流，否则每 10 秒刷两行。
                self._emit_throttled(
                    f"旁听失败|{from_uri}",
                    f"【旁听】{name} 查询失败{detail}",
                    "detail",
                )
            return

        refreshed = False
        if mine and dev_id:
            refreshed = self.store.apply_service_data(dev_id, sid, body)
            if refreshed:
                self._emit(self.device_changed, dev_id)
            name = describe_query(from_uri, size)
            if size:
                # 后面已经跟了内容摘要，这里的「N 项」只会让人以为是任务条数
                name = name.replace(f"（{size} 项）", "")
            self._emit(
                self.log,
                f"【查询返回】{name} ← {_summarize_service_data(sid, body)}",
                "success",
            )
        else:
            # 旁听到的（官方 App / 网关自己发的读），只记一行，不动本地状态；
            # 同样是别人的定时轮询，降为 detail 并节流
            self._emit_throttled(
                f"旁听|{from_uri}",
                f"【旁听】查询结果 {describe_query(from_uri, size)}",
                "detail",
            )

    def _on_command_response(
        self,
        request_id: str,
        target: str,
        errcode,
        body: dict | None = None,
    ) -> None:
        # 主动查询的回包：同一份报文随后还会进 _on_mqtt_message，
        # 那里知道完整上下文（method / from），所以这里只放行、不重复处理。
        if self._is_pending_query(target):
            return

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
        code = errcode if isinstance(errcode, int) else _error_code_of({"errcode": errcode})
        hint = _ERRCODE_HINTS.get(code, "") if isinstance(code, int) else ""
        self._emit(self.notify, f"设备拒绝了命令（errcode={errcode}）", "danger")
        self._emit(
            self.log,
            f"命令响应：设备拒绝（{target}，errcode={errcode}"
            + (f"，{hint}" if hint else "")
            + f"{detail}）",
            "danger",
        )
