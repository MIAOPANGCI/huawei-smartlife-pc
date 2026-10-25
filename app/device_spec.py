"""设备能力推断：把华为协议里的 services/data 翻译成可渲染的 UI 规格。

这一层**不依赖 Qt**，只做纯数据判断，便于单元测试与复用。

核心约定
--------
华为智慧生活的设备模型是「服务（service） + 属性（data 键值）」，没有统一的
开关字段。不同厂商用 ``on`` / ``switch`` / ``powerOn`` 等不同键；有些服务的
``power`` 键其实是「实时功率（W）」而非电源开关。因此必须按规则推断：

1. **服务级黑名单**：电量统计、自检、固件更新等服务整体只读，其下任何键都
   不允许下发（否则会误触设备，甚至触发智能场景）。
2. **开关识别**：先按服务 ID 优先级挑主电源，排除指示灯等附属开关，再退回
   按属性名识别。键必须真实存在于 data 中，避免猜测性下发。
3. **数值可调性**：没有 min/max 元数据且值超出 0–100 的，视为累计统计量
   （如总用电量 403977），只做只读展示，不给滑块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from huawei_smartlife.models import Device, ServiceState

# ---------------------------------------------------------------------------
# 协议字段规则
# ---------------------------------------------------------------------------
SWITCH_KEYS: tuple[str, ...] = ("on", "switch", "power", "powerOn", "state")
SWITCH_SERVICE_IDS: tuple[str, ...] = (
    "switch",
    "power",
    "powerswitch",
    "socket",
    "airconditioner",
)

# 附属开关服务：指示灯 / 童锁等，不能当作设备主电源
NON_POWER_SERVICE_IDS: frozenset[str] = frozenset(
    {"indicator", "indicatorlight", "childlock", "lock", "screen", "display"}
)

# 只读服务：电量统计 / 诊断 / 固件更新 / 端到端加密状态。
# 典型反例：powerElectricity 服务的 power 键是实时功率（W），点它下发
# {"power": 1} 会被设备拒绝（errcode=-201），甚至误触发场景。
READONLY_SERVICE_IDS: frozenset[str] = frozenset(
    {
        "powerelectricity",
        "electricity",
        "energy",
        "powerstatistics",
        "statistics",
        "meter",
        "metering",
        "totalpower",
        "totalelectricity",
        "faultdetection",
        "netinfo",
        "diagnose",
        "update",
        "e2ectrlstate",
    }
)

# 只读数值键：元数据与云端遥测统计量
READONLY_NUMERIC_KEYS: frozenset[str] = frozenset(
    {
        "boottime",
        "action",
        "progress",
        "timestamp",
        "ts",
        "id",
        "duration",
        "updatetime",
        "createtime",
        "reporttime",
        "version",
        "runtime",
        "totalelectricity",
        "totalpower",
        "totalpowerconsumption",
        "electricity",
        "powerconsumption",
        "voltage",
        "current",
        "energy",
        "frequency",
        "signal",
        "rssi",
        "snr",
    }
)

# 只读字符串键：标识 / 描述类
READONLY_STRING_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "displayname",
        "display",
        "name",
        "model",
        "version",
        "firmware",
        "sn",
        "mac",
        "ip",
        "timestamp",
        "boottime",
        "timezone",
        "description",
        "manufacturer",
        "hardwareversion",
        "softwareversion",
    }
)

# 枚举语义的键：值为整数但代表档位/模式编号，没有取值范围元数据时
# 用滑块「猜」一个值几乎一定下发错误命令（例如洗衣机 state=1 是某种程序），
# 因此判为只读，需要精确下发时走通用命令面板。
ENUM_LIKE_KEYS: frozenset[str] = frozenset(
    {
        "mode",
        "state",
        "status",
        "action",
        "level",
        "gear",
        "speed",
        "fanspeed",
        "windspeed",
        "workmode",
        "program",
        "pattern",
        "scene",
        "step",
    }
)

_RANGE_META_KEYS: tuple[str, ...] = (
    "min",
    "minimum",
    "range_min",
    "rangemin",
    "max",
    "maximum",
    "range_max",
    "rangemax",
    "step",
    "precision",
)


# ---------------------------------------------------------------------------
# 中文标签
# ---------------------------------------------------------------------------
_PROTO_LABELS: dict[str, str] = {
    # 服务 ID
    "switch": "开关",
    "powerswitch": "电源开关",
    "socket": "插座",
    "plug": "插座",
    "outlet": "插座",
    "airconditioner": "空调",
    "aircondition": "空调",
    "powerelectricity": "电量统计",
    "energy": "能耗",
    "powerstatistics": "功率统计",
    "statistics": "统计",
    "statistic": "统计",
    "meter": "计量",
    "metering": "计量",
    "totalpower": "总功率",
    "totalelectricity": "总用电量",
    "faultdetection": "故障检测",
    "fault": "故障",
    "netinfo": "网络信息",
    "network": "网络",
    "net": "网络",
    "diagnose": "设备诊断",
    "diagnostics": "设备诊断",
    "info": "信息",
    "update": "固件更新",
    "updateservice": "固件更新",
    "e2ectrlstate": "加密控制状态",
    "e2e": "加密控制",
    "camera": "摄像头",
    "light": "灯光",
    "lamp": "灯",
    "curtain": "窗帘",
    "blind": "百叶窗",
    "sensor": "传感器",
    "thermostat": "温控器",
    "fan": "风扇",
    "humidifier": "加湿器",
    "purifier": "净化器",
    "doorbell": "门铃",
    "vacuum": "扫地机",
    "tv": "电视",
    "speaker": "音箱",
    "router": "路由器",
    "gateway": "网关",
    "indicator": "指示灯",
    "childlock": "童锁",
    "heater": "热水器",
    "waterheater": "热水器",
    # 属性键
    "on": "开关",
    "state": "状态",
    "status": "状态",
    "poweron": "电源开关",
    "enable": "启用",
    "enabled": "启用",
    "brightness": "亮度",
    "bright": "亮度",
    "dim": "调暗",
    "volume": "音量",
    "vol": "音量",
    "mute": "静音",
    "temperature": "温度",
    "temp": "温度",
    "humidity": "湿度",
    "humid": "湿度",
    "mode": "模式",
    "speed": "风速",
    "level": "档位",
    "gear": "档位",
    "color": "颜色",
    "colour": "颜色",
    "colortemp": "色温",
    "colortemperature": "色温",
    "ct": "色温",
    "hue": "色相",
    "sat": "饱和度",
    "saturation": "饱和度",
    "lock": "锁定",
    "unlock": "解锁",
    "open": "开启",
    "close": "关闭",
    "closed": "关闭",
    "play": "播放",
    "pause": "暂停",
    "stop": "停止",
    "battery": "电量",
    "voltage": "电压",
    "current": "电流",
    "energy": "能耗",
    "electricity": "功率",
    "power": "功率",
    "loadpower": "负载功率",
    "rssi": "信号强度",
    "signal": "信号",
    "snr": "信噪比",
    "boottime": "启动时间",
    "boot": "启动",
    "time": "时间",
    "timestamp": "时间戳",
    "duration": "时长",
    "action": "动作",
    "progress": "进度",
    "version": "版本",
    "firmware": "固件",
    "model": "型号",
    "mac": "MAC 地址",
    "ip": "IP 地址",
    "ipaddress": "IP 地址",
    "sn": "序列号",
    "serial": "序列号",
    "manufacturer": "厂商",
    "brand": "品牌",
    "timezone": "时区",
    "frequency": "频率",
    "freq": "频率",
    "count": "计数",
    "total": "总计",
    "remain": "剩余",
    "remaining": "剩余",
    "left": "剩余",
    "lefttime": "剩余时间",
    "resttime": "剩余时间",
    "target": "目标",
    "targettemp": "目标温度",
    "settemp": "设定温度",
    "ambient": "环境",
    "ambienttemp": "环境温度",
    "illumination": "光照",
    "lux": "光照",
    "pm25": "PM2.5",
    "co2": "二氧化碳",
    "motion": "移动",
    "presence": "有人",
    "water": "水浸",
    "smoke": "烟雾",
    "gas": "燃气",
    "angle": "角度",
    "position": "位置",
    "percent": "百分比",
    "ratio": "比率",
}

_SID_OVERRIDE: dict[str, str] = {
    "power": "电源",
    "powerswitch": "电源开关",
    "plug": "插座",
    "outlet": "插座",
    "electricity": "电量统计",
    "heater": "热水器",
    "lock": "门锁",
}

_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("light", ("灯", "light", "lamp", "bulb", "彩光", "吸顶")),
    ("socket", ("插座", "socket", "plug", "outlet", "排插")),
    ("ac", ("空调", "aircondition", "ac", "冷气")),
    ("heater", ("热水器", "heater", "waterheater", "浴")),
    ("camera", ("摄像", "camera", "ipc", "监控")),
    ("sensor", ("传感", "sensor", "探测", "门窗", "温湿")),
    ("router", ("路由", "router", "ap", "be7", "be3")),
    ("wearable", ("watch", "band", "freeclip", "耳机", "手环", "手表")),
    ("washer", ("洗衣", "washer", "干衣")),
    ("curtain", ("窗帘", "curtain", "blind")),
    ("purifier", ("净化", "purifier", "加湿", "humidifier")),
)


def humanize_key(key: str, kind: str = "prop") -> str:
    """协议字段名 → 可读中文（仅用于展示，下发时始终用原始字段名）。"""
    text = str(key).strip()
    if not text:
        return text
    low = text.lower()
    if kind == "sid" and low in _SID_OVERRIDE:
        return _SID_OVERRIDE[low]
    if low in _PROTO_LABELS:
        return _PROTO_LABELS[low]
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", text)
    if words:
        translated: list[str] = []
        for word in words:
            low_word = word.lower()
            if low_word not in _PROTO_LABELS:
                return " ".join(w[:1].upper() + w[1:] for w in words)
            translated.append(_PROTO_LABELS[low_word])
        return "".join(translated)
    return text


def device_kind(device: Device) -> str:
    """按名称 / 型号 / 服务推断设备类型，用于卡片配色与图标。"""
    haystack = " ".join(
        filter(
            None,
            [
                device.name or "",
                device.model or "",
                device.prod_id or "",
                " ".join(device.services.keys()),
            ],
        )
    ).lower()
    for kind, hints in _TYPE_HINTS:
        if any(hint in haystack for hint in hints):
            return kind
    return "generic"


def device_initial(device: Device) -> str:
    """卡片头像字符：取名称首个非空白字符。"""
    for char in (device.name or ""):
        if char.strip():
            return char
    return "?"


# ---------------------------------------------------------------------------
# 开关识别
# ---------------------------------------------------------------------------
def _is_boolish(key: str, value: object) -> bool:
    if isinstance(value, bool):
        return True
    return isinstance(value, int) and str(key).lower() in {
        k.lower() for k in SWITCH_KEYS
    }


def switch_state_of(value: object) -> str:
    """把开关值归一为 on/off/unknown（供三态电源按钮使用）。"""
    if value in (1, True):
        return "on"
    if value in (0, False):
        return "off"
    return "unknown"


def find_switch_service(device: Device) -> tuple[str, str, object] | None:
    """找设备主开关，返回 (service_id, key, value)；找不到返回 None。

    多开关设备（例如热水器同时有 ``indicator`` 指示灯与 ``switch`` 主电源）
    必须先排除附属开关，再按主电源命名优先级挑选，不能依赖字典遍历顺序。
    """
    services = [
        svc
        for svc in device.services.values()
        if svc.sid.lower() not in NON_POWER_SERVICE_IDS
        and svc.sid.lower() not in READONLY_SERVICE_IDS
    ]

    # 第一轮：服务 ID 命中主电源命名（无数据也接受，状态记为 unknown 仍可点）
    for wanted in SWITCH_SERVICE_IDS:
        for svc in services:
            if svc.sid.lower() != wanted:
                continue
            has_switch_key = any(
                str(key).lower() in {k.lower() for k in SWITCH_KEYS}
                and isinstance(value, (bool, int))
                for key, value in svc.data.items()
            )
            # data 非空却没有开关键 → 统计类服务，跳过
            if svc.data and not has_switch_key:
                continue
            for key, value in svc.data.items():
                if str(key).lower() in {k.lower() for k in SWITCH_KEYS} and isinstance(
                    value, (bool, int)
                ):
                    return svc.sid, key, value
            return svc.sid, "on", svc.data.get("on")

    # 第二轮：按属性名识别（服务命名不规范的设备，如 light）
    best: tuple[int, str, str, object] | None = None
    switch_keys_lower = [k.lower() for k in SWITCH_KEYS]
    for svc in services:
        for key, value in svc.data.items():
            kl = str(key).lower()
            if kl in switch_keys_lower and isinstance(value, (bool, int)):
                rank = switch_keys_lower.index(kl)
                if best is None or rank < best[0]:
                    best = (rank, svc.sid, key, value)
                break
    if best is not None:
        return best[1], best[2], best[3]
    return None


# ---------------------------------------------------------------------------
# 数值可调性
# ---------------------------------------------------------------------------
def _has_range_meta(data: Mapping[str, Any]) -> bool:
    return any(str(k).lower() in _RANGE_META_KEYS for k in data)


def slider_unsuitable(key: str, value: float, data: Mapping[str, Any]) -> bool:
    """数值是否不适合作为滑块（只读遥测 / 元数据 / 超范围累计量）。"""
    if str(key).lower() in READONLY_NUMERIC_KEYS:
        return True
    if not _has_range_meta(data) and (value > 100 or value < 0):
        return True
    return False


def infer_range(
    data: Mapping[str, Any], current: object
) -> tuple[float, float, float, bool]:
    """推断 (min, max, step, is_int)。优先用 data 中的显式范围元数据。"""
    lower = {str(k).lower(): k for k in data}
    is_int = isinstance(current, int) and not isinstance(current, bool)
    step = 1.0 if is_int else 0.5
    minimum: float | None = None
    maximum: float | None = None

    for name in ("min", "minimum", "range_min", "rangemin"):
        if name in lower and isinstance(data[lower[name]], (int, float)):
            minimum = float(data[lower[name]])
            break
    for name in ("max", "maximum", "range_max", "rangemax"):
        if name in lower and isinstance(data[lower[name]], (int, float)):
            maximum = float(data[lower[name]])
            break
    for name in ("step", "precision"):
        if name in lower and isinstance(data[lower[name]], (int, float)):
            candidate = float(data[lower[name]])
            if candidate > 0:
                step = candidate
            break

    if minimum is None:
        minimum = 0.0
    if maximum is None:
        maximum = 100.0 if minimum == 0.0 else minimum + 100.0
    if maximum <= minimum:
        maximum = minimum + 100.0
    return minimum, maximum, step, is_int


# ---------------------------------------------------------------------------
# 渲染规格
# ---------------------------------------------------------------------------
KIND_TOGGLE = "toggle"
KIND_SLIDER = "slider"
KIND_TEXT = "text"
KIND_READONLY_NUMBER = "readonly_number"
KIND_READONLY_TEXT = "readonly_text"
KIND_JSON = "json"


@dataclass
class PropertySpec:
    """单个属性应渲染成什么控件。"""

    sid: str
    key: str
    label: str
    kind: str
    value: Any
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    is_int: bool = True


@dataclass
class ServiceSpec:
    """一个服务分组。"""

    sid: str
    label: str
    readonly: bool
    properties: list[PropertySpec] = field(default_factory=list)


def build_service_specs(device: Device) -> list[ServiceSpec]:
    """把设备所有服务转成 UI 可渲染的规格列表。

    规则与旧版保持一致：统计类服务整体只读；开关 → 开关控件；可调数值 →
    滑块；超范围数值 → 只读；标识类字符串 → 只读；复合值 → JSON 预览。
    """
    specs: list[ServiceSpec] = []
    for svc in device.services.values():
        specs.append(_build_service_spec(svc))
    # 有可操作项的服务排前面，纯只读的排在后面
    specs.sort(key=lambda s: (s.readonly, s.sid))
    return specs


def _build_service_spec(svc: ServiceState) -> ServiceSpec:
    sid = svc.sid
    readonly = sid.lower() in READONLY_SERVICE_IDS
    spec = ServiceSpec(sid=sid, label=humanize_key(sid, kind="sid"), readonly=readonly)
    data = dict(svc.data or {})

    for key, value in data.items():
        kl = str(key).lower()
        if kl in _RANGE_META_KEYS:
            continue  # 范围元数据不单独展示
        label = humanize_key(key)

        if readonly:
            spec.properties.append(_readonly_spec(sid, key, label, value))
            continue

        if _is_boolish(key, value):
            spec.properties.append(
                PropertySpec(sid, key, label, KIND_TOGGLE, bool(value))
            )
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            enum_like = kl in ENUM_LIKE_KEYS and not _has_range_meta(data)
            if enum_like or slider_unsuitable(key, float(value), data):
                spec.properties.append(
                    PropertySpec(sid, key, label, KIND_READONLY_NUMBER, value)
                )
            else:
                minimum, maximum, step, is_int = infer_range(data, value)
                spec.properties.append(
                    PropertySpec(
                        sid,
                        key,
                        label,
                        KIND_SLIDER,
                        value,
                        minimum=minimum,
                        maximum=maximum,
                        step=step,
                        is_int=is_int,
                    )
                )
        elif isinstance(value, str):
            kind = (
                KIND_READONLY_TEXT
                if kl in READONLY_STRING_KEYS
                else KIND_TEXT
            )
            spec.properties.append(PropertySpec(sid, key, label, kind, value))
        elif isinstance(value, (dict, list)):
            spec.properties.append(
                PropertySpec(sid, key, label, KIND_JSON, value)
            )
        else:
            spec.properties.append(
                PropertySpec(sid, key, label, KIND_READONLY_TEXT, str(value))
            )
    return spec


def _readonly_spec(sid: str, key: str, label: str, value: Any) -> PropertySpec:
    if isinstance(value, bool):
        return PropertySpec(sid, key, label, KIND_READONLY_TEXT, "开" if value else "关")
    if isinstance(value, (int, float)):
        return PropertySpec(sid, key, label, KIND_READONLY_NUMBER, value)
    if isinstance(value, (dict, list)):
        return PropertySpec(sid, key, label, KIND_JSON, value)
    return PropertySpec(sid, key, label, KIND_READONLY_TEXT, str(value))


_SUMMARY_METRICS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("brightness",), "亮度", "%"),
    (("targettemp", "settemp"), "设定", "°C"),
    (("temperature", "ambienttemp"), "温度", "°C"),
    (("humidity",), "湿度", "%"),
    (("volume",), "音量", ""),
    (("speed", "level", "gear"), "档位", ""),
    (("pm25",), "PM2.5", ""),
    (("battery",), "电量", "%"),
)


def device_summary(device: Device) -> str:
    """卡片底部的一句话状态：开关状态 + 一个最有信息量的指标。

    例：``已开启 · 亮度 60%`` / ``离线`` / ``在线 · 温度 26°C``。
    """
    parts: list[str] = []

    found = find_switch_service(device)
    if found is not None:
        state = switch_state_of(found[2])
        if state == "on":
            parts.append("已开启")
        elif state == "off":
            parts.append("已关闭")

    metric_text = ""
    for keys, label, unit in _SUMMARY_METRICS:
        for svc in device.services.values():
            data = svc.data or {}
            for key, value in data.items():
                if str(key).lower() not in keys:
                    continue
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                shown = f"{value:g}" if isinstance(value, float) else str(value)
                metric_text = f"{label} {shown}{unit}"
                break
            if metric_text:
                break
        if metric_text:
            break

    if metric_text:
        parts.append(metric_text)

    # 没有开关也没有可展示的指标时返回空串：卡片会隐藏这一行，
    # 避免与「房间 · 在线状态」重复表达同一件事
    return " · ".join(parts)


def format_number(value: Any) -> str:
    """数值展示：整数加千分位，浮点保留至多 2 位。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)
