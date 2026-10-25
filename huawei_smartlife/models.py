"""华为智慧生活客户端的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


@dataclass
class AuthSession:
    """认证会话：分离华为账号 OAuth 与智慧生活 HMS-lite 两套 Token。"""

    account: str
    user_id: str
    service_token: str
    hms_access_token: str | None = None
    hms_refresh_token: str | None = None
    hms_expires_at: datetime | None = None
    oauth_access_token: str | None = None
    oauth_expires_at: datetime | None = None
    device_id: str = ""
    device_name: str = "huawei-smarthome-pc"
    pushtmid: str | None = None
    app_id: str = "com.huawei.smarthome-ios"
    base_url: str = "https://smarthome.hicloud.com"
    home_zone: str | None = None
    oauth_domain: str | None = None
    site_domain: str | None = None

    def is_hms_valid(self, margin_seconds: int = 300) -> bool:
        if not self.hms_access_token:
            return False
        if self.hms_expires_at is None:
            return True
        return datetime_after_now(self.hms_expires_at, margin_seconds)

    def is_oauth_valid(self, margin_seconds: int = 300) -> bool:
        if not self.oauth_access_token:
            return False
        if self.oauth_expires_at is None:
            return True
        return datetime_after_now(self.oauth_expires_at, margin_seconds)


CHALLENGE_KIND_DEVICE = "device"
CHALLENGE_KIND_SMS = "sms"
CHALLENGE_KIND_EMAIL = "email"

_SMS_ACCOUNT_TYPES = frozenset({"2", "6"})
_EMAIL_ACCOUNT_TYPES = frozenset({"1", "5"})
_DEVICE_ACCOUNT_TYPES = frozenset({"-1"})


def channel_kind(account_type: object) -> str:
    """将华为挑战渠道 accountType 分类为 device/sms/email。"""
    value = str(account_type)
    if value in _SMS_ACCOUNT_TYPES:
        return CHALLENGE_KIND_SMS
    if value in _EMAIL_ACCOUNT_TYPES:
        return CHALLENGE_KIND_EMAIL
    return CHALLENGE_KIND_DEVICE


@dataclass
class ChallengeChannel:
    """登录时服务器要求的验证渠道（短信/设备挑战码等）。"""

    name: str
    account_type: str
    kind: str
    sent: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.account_type}:{self.name}"

    @property
    def is_sms(self) -> bool:
        return self.kind == CHALLENGE_KIND_SMS


def channel_prompt(channel: ChallengeChannel) -> str:
    """把验证渠道转成一句给人看的话。

    绝不能把 accountType 直接拼进去——用户看到 "nova 6 (5G)(类型 -1)" 只会
    一头雾水，还会以为"设备验证"是程序乱发的东西。
    """
    label = channel.name
    if channel.kind == CHALLENGE_KIND_SMS:
        if channel.sent:
            return f"验证码已发送至 {label}，请查收后输入"
        return f"验证码将发送到 {label}，点「发送验证码」后查收"
    if channel.kind == CHALLENGE_KIND_EMAIL:
        if channel.sent:
            return f"验证码已发送至邮箱 {label}，请查收后输入"
        return f"验证码将发送到邮箱 {label}，点「发送验证码」后查收"
    # 设备验证：验证码由华为推到你账号下已登录的设备，客户端没有"发送"动作
    if channel.sent:
        return f"华为已推送验证码到 {label}，请尽快输入"
    return f"在你已登录的华为设备（{label}）上查看验证码"


# 华为账号登录接口的 resultCode。只收录能确认语义的几个，
# 其余一律按"验证失败"处理并把原始码带出去，方便排查。
_CHALLENGE_RESULT_HINTS = {
    "70002001": "账号或密码不正确",
    "70002002": "账号不存在",
    "70008801": "验证码不正确",
    "70008802": "验证码已过期，请重新获取",
    # 华为原话：operation without call getList or call getList too long time
    # 也就是"没先取列表，或取列表后隔太久才提交"——实际最常见的原因是
    # 用户在验证页停留超过服务端窗口（实测 2 分半就会失败）。
    "70008804": "验证码校验失败或等待超时",
}


def challenge_error_hint(
    result_code: str | None,
    channels: tuple[ChallengeChannel, ...] = (),
    current_kind: str | None = None,
) -> str:
    """把 resultCode 翻译成用户能照着做的一句话。

    华为的码在文档里查不到，只丢一句"验证码不正确"等于没说。这里补上
    最可能的原因：用户手上那条码很可能是华为推到设备上的，而我们默认停在
    短信渠道，类型对不上就必然失败——那就直接告诉他去切渠道。
    """
    code = (result_code or "").strip()
    base = _CHALLENGE_RESULT_HINTS.get(code, "验证失败")
    device = next(
        (
            channel
            for channel in channels
            if channel.kind == CHALLENGE_KIND_DEVICE and channel.sent
        ),
        None,
    )
    if device is not None and current_kind != CHALLENGE_KIND_DEVICE:
        tail = (
            f"。如果你填的是华为设备上收到的验证码，把「接收方式」切到"
            f"「华为设备 · {device.name}」再提交一次"
        )
    elif current_kind == CHALLENGE_KIND_DEVICE:
        tail = (
            "。华为的验证窗口只有两分钟左右，超时也会报这个码；"
            "点「重新获取」拿一条新验证码并尽快输入"
        )
    else:
        tail = "。请确认验证码没有过期，或重新点「发送验证码」"
    return f"{base}（华为返回 {code}）{tail}" if code else f"{base}{tail}"


@dataclass
class LoginChallenge:
    """登录挑战信息，提示用户需要哪种验证。"""

    prompt: str
    challenge_name: str
    challenge_type: str
    channels: tuple[ChallengeChannel, ...] = ()
    # 当前选中的渠道类型（sms / email / device）。界面靠它把下拉框停在同一项上，
    # 否则会出现「提示说已发到设备、下拉显示的却是短信」这种自相矛盾。
    kind: str = CHALLENGE_KIND_DEVICE


@dataclass
class LoginStart:
    """begin_login 的返回：要么直接拿到会话，要么需要挑战。"""

    session: AuthSession | None = None
    challenge: LoginChallenge | None = None


@dataclass
class Home:
    home_id: str
    name: str
    role: str | None = None


@dataclass
class Room:
    home_id: str
    room_id: str
    name: str


@dataclass
class ServiceState:
    """设备的一个服务（如开关、亮度）及其当前状态。"""

    sid: str
    service_type: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    reported_timestamp: str | None = None


@dataclass
class Device:
    """设备实例。"""

    home_id: str
    dev_id: str
    name: str
    room_id: str | None = None
    room_name: str | None = None
    gateway_id: str | None = None
    node_type: str | int | None = None
    prod_id: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    firmware_version: str | None = None
    online: bool | None = None
    services: Mapping[str, ServiceState] = field(default_factory=dict)


@dataclass
class DeviceState:
    """设备的实时状态（来自动态状态接口）。"""

    dev_id: str
    online: bool | None = None
    services: Mapping[str, ServiceState] = field(default_factory=dict)


@dataclass
class DiscoverySnapshot:
    """一次拉取的家庭与设备快照。"""

    homes: tuple[Home, ...]
    devices: tuple[Device, ...]
    device_home_index: Mapping[str, list[str]] = field(default_factory=dict)


@dataclass
class Scene:
    """智能场景（一键执行的一组自动化动作）。"""

    scene_id: str
    name: str
    home_id: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    description: str | None = None


@dataclass
class MqttConnectionSettings:
    """MQTT 连接参数。"""

    broker_host: str
    broker_port: int
    client_id: str
    subscription_filters: tuple[str, ...]
    username: str | None = None
    password: str | None = None
    control_source: str = "huawei-smarthome"
    use_tls: bool = True
    keepalive: int = 60
    subscription_qos: int = 2


@dataclass
class MessageChannelSession:
    """消息中心会话：返回 MQTT client id 与订阅 topic。"""

    mqtt_client_id: str
    mqtt_topic: str
    first_login: bool | None = None
    log_collection: str | None = None


@dataclass
class SmartHomeCloudRoute:
    """云端路由：返回 MQTT broker 主机与端口、HTTPS 端口等。"""

    smart_home_host: str
    mqtt_port: int
    https_port: int
    expires_seconds: int | None = None
    role: int | None = None


def datetime_after_now(value: datetime, margin_seconds: int) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > datetime.now(timezone.utc) + timedelta(seconds=margin_seconds)


def parse_remote_timestamp(value: Any) -> datetime | None:
    """解析智慧生活消息中的紧凑 UTC 时间戳（如 20240101T000000Z）。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    body = value[:-1]
    if "." in body:
        whole, fraction = body.split(".", 1)
        if not fraction.isdigit() or not whole:
            return None
        fraction = (fraction + "000000")[:6]
        body = f"{whole}.{fraction}"
        pattern = "%Y%m%dT%H%M%S.%f"
    else:
        pattern = "%Y%m%dT%H%M%S"
    try:
        return datetime.strptime(body, pattern).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
