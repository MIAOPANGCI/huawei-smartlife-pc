"""紧凑设备卡片：托盘面板与桌面挂件共用。

比主界面卡片更小（默认 152×62），只保留「名称 + 房间·状态 + 电源键」，
在 300px 宽的浮层里也能一眼看清。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from huawei_smartlife.models import Device

from .. import device_spec
from ..device_spec import switch_state_of
from .controls import PowerButton
from .labels import ElidedLabel

CARD_WIDTH = 152
CARD_HEIGHT = 62


class MiniDeviceCard(QFrame):
    """紧凑设备卡。

    只响应电源键：托盘浮层和桌面挂件是「快速开关」的场景，点卡片弹详情
    会打断这个流程（还容易误触），所以整卡不做点击响应。
    """

    power_requested = Signal(str)

    def __init__(
        self,
        device: Device,
        width: int = CARD_WIDTH,
        height: int = CARD_HEIGHT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._dev_id = device.dev_id
        self.setObjectName("MiniCard")
        self.setFixedSize(width, height)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setProperty("offline", "false")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 8, 9, 8)
        layout.setSpacing(6)

        text_host = QWidget()
        text_box = QVBoxLayout(text_host)
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(3)
        text_box.addStretch(1)

        self._name = ElidedLabel("设备")
        self._name.setObjectName("MiniCardTitle")
        text_box.addWidget(self._name)

        self._meta = ElidedLabel("")
        self._meta.setObjectName("MiniCardMeta")
        text_box.addWidget(self._meta)
        text_box.addStretch(1)

        layout.addWidget(text_host, 1)

        self._power = PowerButton("unknown", 24)
        self._power.setCursor(Qt.PointingHandCursor)
        self._power.toggled_requested.connect(
            lambda _state: self.power_requested.emit(self._dev_id)
        )
        layout.addWidget(self._power, 0, Qt.AlignVCenter)

        self.update_device(device)

    def update_device(self, device: Device) -> None:
        self._dev_id = device.dev_id
        self._name.setText(device.name or device.dev_id)
        self._name.setToolTip(device.name or device.dev_id)

        room = device.room_name or "未分配房间"
        state_text = {True: "在线", False: "离线", None: "待同步"}[device.online]
        self._meta.setText(f"{room} · {state_text}")

        found = device_spec.find_switch_service(device)
        if found is not None:
            self._power.set_state(switch_state_of(found[2]))
            self._power.setVisible(True)
        else:
            self._power.setVisible(False)

        self.setProperty("offline", "true" if device.online is False else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    @property
    def dev_id(self) -> str:
        return self._dev_id


