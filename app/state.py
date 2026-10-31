"""应用数据层：设备快照、实时状态合并、遥测应用。

这一层不依赖 Qt，只维护「当前看到的设备状态」这一份事实，UI 只负责渲染。
把它单独抽出来，一方面便于单元测试（无 GUI 也能验证合并逻辑），另一方面
避免状态逻辑散落在窗口类里。

合并策略（与协议现实强相关）
---------------------------
* REST 快照的 ``services[].data`` 对部分设备（如空调）恒为空，真实状态只从
  MQTT 遥测来。所以刷新快照时**不能**直接整体替换设备对象，否则刚收到的遥测
  会被空数据抹掉。
* 规则：REST 有数据的服务以 REST 为准（刚拉的，最新）；REST 为空的保留遥测；
  仅存在于遥测里的服务整体保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from huawei_smartlife.models import (
    Device,
    DeviceState,
    DiscoverySnapshot,
    Home,
    ServiceState,
)

from .device_spec import find_switch_service, switch_state_of

# 回包里跟业务无关的协议字段。查询回包常带着 errcode（成功时是 0），
# 直接合并会把它们变成服务属性，界面上就多出一个莫名其妙的「错误码 0」。
_PROTOCOL_KEYS: frozenset[str] = frozenset({"errcode", "errmessage", "errmsg"})


@dataclass
class DeviceStore:
    """设备集合与状态合并。"""

    homes: list[Home] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    _by_id: dict[str, Device] = field(default_factory=dict, repr=False)
    # 监听器：设备变化时通知 UI（changed device ids）
    listeners: list = field(default_factory=list, repr=False)

    # ---------------- 基础查询 ----------------
    def clear(self) -> None:
        self.homes.clear()
        self.devices.clear()
        self._by_id.clear()

    def get(self, dev_id: str) -> Device | None:
        return self._by_id.get(dev_id)

    def all(self) -> list[Device]:
        return list(self.devices)

    def home_name(self, home_id: str) -> str:
        for home in self.homes:
            if home.home_id == home_id:
                return home.name
        return "未分配家庭"

    def devices_of_home(self, home_id: str) -> list[Device]:
        return [d for d in self.devices if d.home_id == home_id]

    def homes_with_counts(self) -> list[tuple[Home, int]]:
        """家庭 + 设备数，按设备数降序（家庭列表展示用）。"""
        result = [(home, len(self.devices_of_home(home.home_id))) for home in self.homes]
        result.sort(key=lambda item: (-item[1], item[0].name))
        return result

    @property
    def total(self) -> int:
        return len(self.devices)

    @property
    def online_count(self) -> int:
        return sum(1 for d in self.devices if d.online is True)

    @property
    def offline_count(self) -> int:
        return sum(1 for d in self.devices if d.online is False)

    @property
    def unknown_count(self) -> int:
        return sum(1 for d in self.devices if d.online is None)

    @property
    def on_count(self) -> int:
        """当前处于「开」状态的设备数量（供状态面板统计）。"""
        count = 0
        for device in self.devices:
            found = find_switch_service(device)
            if found and switch_state_of(found[2]) == "on":
                count += 1
        return count

    def switch_summary(self, device: Device) -> tuple[str, str, object] | None:
        return find_switch_service(device)

    # ---------------- 变更通知 ----------------
    def add_listener(self, callback) -> None:
        """callback(dev_id: str | None)，None 表示整体刷新。"""
        self.listeners.append(callback)

    def _notify(self, dev_id: str | None = None) -> None:
        for callback in list(self.listeners):
            try:
                callback(dev_id)
            except Exception:  # noqa: BLE001 - 单个监听器异常不影响其他
                pass

    # ---------------- 快照 ----------------
    def replace_snapshot(
        self,
        snapshot: DiscoverySnapshot,
        states: list[DeviceState] | None = None,
        *,
        preserve_previous: bool = True,
    ) -> None:
        """用新快照替换设备集合，保留上一轮遥测（preserve_previous）。"""
        previous = {d.dev_id: d for d in self.devices} if preserve_previous else {}
        self.homes = list(snapshot.homes)
        self.devices = []
        self._by_id.clear()

        for device in snapshot.devices:
            old = previous.get(device.dev_id)
            if old is not None:
                self.merge_device(device, old)
            self.devices.append(device)
            self._by_id[device.dev_id] = device

        # 合并批量实时状态（含在线状态）
        for state in states or []:
            device = self._by_id.get(state.dev_id)
            if device is not None:
                self.merge_dynamic_state(device, state)

        # 设备可能挂在多个家庭下：以首次出现的家庭为准，其余记在 extra 里
        self._notify(None)

    @staticmethod
    def merge_device(target: Device, source: Device) -> None:
        """把 source（可能含遥测）合并进 target（新快照），保留非空状态。"""
        for field_name in (
            "online",
            "room_id",
            "room_name",
            "prod_id",
            "model",
            "manufacturer",
            "firmware_version",
        ):
            if getattr(target, field_name, None) is None:
                setattr(target, field_name, getattr(source, field_name, None))

        for sid, src_svc in source.services.items():
            tgt_svc = target.services.get(sid)
            if tgt_svc is None:
                target.services[sid] = src_svc
                continue
            if dict(tgt_svc.data):
                merged = {**dict(src_svc.data), **dict(tgt_svc.data)}
            else:
                merged = dict(src_svc.data)
            tgt_svc.data.clear()
            tgt_svc.data.update(merged)
            if not tgt_svc.reported_timestamp and src_svc.reported_timestamp:
                tgt_svc.reported_timestamp = src_svc.reported_timestamp

    @staticmethod
    def merge_dynamic_state(device: Device, state: DeviceState) -> bool:
        """把实时状态合并进设备，返回是否有实际变化。"""
        changed = False
        if state.online is not None and device.online != state.online:
            device.online = state.online
            changed = True

        for sid, svc in state.services.items():
            current = device.services.get(sid)
            if current is None:
                device.services[sid] = ServiceState(
                    sid=sid,
                    service_type=svc.service_type,
                    data=dict(svc.data),
                    reported_timestamp=svc.reported_timestamp,
                )
                changed = True
                continue
            if dict(svc.data):
                merged = {**dict(current.data), **dict(svc.data)}
                if merged != dict(current.data):
                    current.data.clear()
                    current.data.update(merged)
                    changed = True
        return changed

    # ---------------- MQTT 通知 ----------------
    def apply_telemetry(self, payload: dict) -> tuple[str, bool] | None:
        """处理 deviceDataChanged 遥测，返回 (dev_id, 是否有变化)。"""
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return None
        dev_id = body.get("devId")
        if not isinstance(dev_id, str):
            return None
        device = self._by_id.get(dev_id)
        if device is None:
            return None
        services = body.get("services")
        if not isinstance(services, list):
            return None

        changed = False
        for item in services:
            if not isinstance(item, dict):
                continue
            sid = item.get("sid")
            data = item.get("data")
            if not isinstance(sid, str) or not isinstance(data, dict):
                continue
            svc = device.services.get(sid)
            if svc is None:
                device.services[sid] = ServiceState(sid=sid, data=dict(data))
                changed = True
                continue
            merged = {**dict(svc.data), **data}
            if merged != dict(svc.data):
                svc.data.clear()
                svc.data.update(merged)
                changed = True

        if changed:
            self._notify(dev_id)
        else:
            # 即使没有状态变化，也把原始遥测交给 UI 记日志
            self._notify(dev_id)
        return dev_id, changed

    def apply_device_status(self, payload: dict) -> tuple[str, bool] | None:
        """处理 deviceStatus（上下线），返回 (dev_id, 是否有变化)。"""
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return None
        dev_id = body.get("devId")
        status = body.get("status")
        if not isinstance(dev_id, str) or status not in ("online", "offline"):
            return None
        device = self._by_id.get(dev_id)
        if device is None:
            return None
        online = status == "online"
        if device.online == online:
            return dev_id, False
        device.online = online
        self._notify(dev_id)
        return dev_id, True

    def apply_service_data(self, dev_id: str, sid: str, data: object) -> bool:
        """把一次查询响应（method=GET）里的服务数据合并进设备。

        设备回包的写法有两种：直接给该服务的 data（``{"on": 1}``），
        或者再包一层 ``{"services": [{"sid": ..., "data": ...}]}``；都按服务数据收。
        返回是否有实际变化。
        """
        device = self._by_id.get(dev_id)
        if device is None or not isinstance(data, dict):
            return False

        services = data.get("services")
        if isinstance(services, list):
            changed = False
            for item in services:
                if not isinstance(item, dict):
                    continue
                item_data = item.get("data")
                if not isinstance(item_data, dict):
                    continue
                item_sid = item.get("sid") or sid
                changed = self._merge_service(device, str(item_sid), item_data) or changed
            if changed:
                self._notify(dev_id)
            return changed

        if not data:
            return False
        changed = self._merge_service(device, sid, data)
        if changed:
            self._notify(dev_id)
        return changed

    @staticmethod
    def _merge_service(device: Device, sid: str, data: dict) -> bool:
        if not isinstance(sid, str) or not sid:
            return False
        clean = {
            key: value for key, value in data.items() if key not in _PROTOCOL_KEYS
        }
        # 只剩 errcode 的回包（查询失败）没有任何可用的服务属性，直接忽略
        if not clean:
            return False
        svc = device.services.get(sid)
        if svc is None:
            device.services[sid] = ServiceState(sid=sid, data=dict(clean))
            return True
        merged = {**dict(svc.data), **clean}
        if merged == dict(svc.data):
            return False
        svc.data.clear()
        svc.data.update(merged)
        return True

    def mark_switch(self, dev_id: str, sid: str, key: str, value: object) -> None:
        """本地乐观更新开关值（命令下发后立即反映到 UI）。

        真实状态仍以设备回传的 commandRsp / 遥测为准；这里只是避免按钮
        长时间停留在旧状态。
        """
        device = self._by_id.get(dev_id)
        if device is None:
            return
        svc = device.services.get(sid)
        if svc is None:
            device.services[sid] = ServiceState(sid=sid, data={key: value})
        else:
            svc.data = {**dict(svc.data), key: value}
        self._notify(dev_id)
