"""把 MQTT 报文翻译成中文摘要，供「运行日志」显示。

为什么需要这一层：设备遥测是**主动上报**的，一个功率计量设备每 3 秒就发一条，
信封里的 JSON 少则十几行。原先直接把整包 dump 到界面，几小时就能刷到几十 MB，
真正的错误全被埋掉。

所以界面一律显示这里的摘要（``客厅插座 · 功率 1717W``），信封原文降级到
``app.log``，只在排查协议时去翻。

本模块不依赖 Qt，可单独测试。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from .device_spec import humanize_key, translate_value, unit_of

# 单位统一由 device_spec.unit_of() 提供（与设备详情共用一份）


# 这些键的 0/1 是开关语义，不要显示成数字
_SWITCH_KEYS = frozenset(
    {
        "on",
        "enable",
        "enabled",
        "poweron",
        "open",
        "opened",
        "close",
        "closed",
        "lock",
        "locked",
        "mute",
        "move",
        "anion",
        "swing",
        "childlock",
        "motion",
        "presence",
    }
)

# 单个服务下最多列几个字段，超出只报数量
_MAX_FIELDS = 4


def _format_value(key: str, value: Any) -> str:
    low = key.lower()
    if isinstance(value, bool):
        return "开" if value else "关"
    if isinstance(value, (int, float)):
        if low in _SWITCH_KEYS and value in (0, 1):
            return "开" if value else "关"
        unit = unit_of(low)
        text = f"{value:g}" if isinstance(value, float) else str(value)
        return f"{text}{unit}"
    if isinstance(value, str):
        return translate_value(value)
    if isinstance(value, Mapping):
        return f"{len(value)} 项"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} 项"
    return str(value)


def _service_label(service: Mapping[str, Any]) -> str:
    """服务名：优先 sid（实例 ID），退回 st（类型）。"""
    raw = str(service.get("sid") or service.get("st") or "").strip()
    if not raw:
        return "数据"
    # visitPoint5 / visitPoint6 这种带序号尾巴的，去掉数字再翻译
    return humanize_key(re.sub(r"\d+$", "", raw), kind="sid") or raw


def _describe_service(service: Mapping[str, Any]) -> str:
    label = _service_label(service)
    data = service.get("data")
    if not isinstance(data, Mapping) or not data:
        return label
    parts: list[str] = []
    for key, value in data.items():
        # 空字符串 / None 是"未设置"，列出来只是噪音
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        key_label = humanize_key(str(key))
        rendered = _format_value(str(key), value)
        # 服务名已经包含属性名时别再重复一遍：
        # sid=videoSwitch（视频开关）+ key=on（开关）→「视频开关 关」
        if key_label and (key_label in label or label in key_label):
            parts.append(rendered)
        else:
            parts.append(f"{key_label} {rendered}")
    if not parts:
        return label
    if len(parts) > _MAX_FIELDS:
        rest = len(parts) - _MAX_FIELDS
        parts = parts[:_MAX_FIELDS] + [f"另有 {rest} 项"]
    return f"{label} {'，'.join(parts)}"


def describe_telemetry(
    payload: Any,
    name_of: Callable[[str], str] | None = None,
) -> str:
    """``deviceDataChanged`` 报文 → 一行中文。

    name_of: dev_id → 设备显示名；拿不到时退化成短 ID。
    """
    body = payload.get("body") if isinstance(payload, Mapping) else None
    if not isinstance(body, Mapping):
        return "设备上报（无法解析）"
    dev_id = str(body.get("devId") or "").strip()
    name = ""
    if name_of is not None and dev_id:
        name = name_of(dev_id) or ""
    if not name:
        name = dev_id[:8] or "未知设备"
    services = body.get("services")
    if not isinstance(services, (list, tuple)) or not services:
        return f"{name} 上报（无数据）"
    parts = [_describe_service(item) for item in services if isinstance(item, Mapping)]
    parts = [part for part in parts if part]
    if not parts:
        return f"{name} 上报（无数据）"
    return f"{name} · {'；'.join(parts)}"


def describe_query(from_uri: Any, size: int) -> str:
    """查询响应 → 可读路径。

    原始 ``from`` 是 ``/devices/<uuid>/services/gateway/ntwk/WlanBasic``，
    直接把整串打到界面上没有意义（UUID 还占半行），这里翻成
    ``网关 · 网络 · WiFi 信息``。
    """
    text = str(from_uri or "").strip()
    if not text:
        return f"查询结果（{size} 项）"
    marker = "/services/"
    path = text.split(marker, 1)[1] if marker in text else text
    segments = [seg.split("?", 1)[0] for seg in path.split("/") if seg.strip()]
    labels = [humanize_key(seg, kind="sid") for seg in segments[:3]]
    labels = [label for label in labels if label]
    where = " · ".join(labels) if labels else "查询"
    return f"{where}（{size} 项）"
