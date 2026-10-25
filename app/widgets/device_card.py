"""设备卡片：主界面网格中的单个设备。

布局对齐参考实现的横向小卡（图标 → 两行文字 → 电源键）：
    ┌──────────────────────────────────┐
    │  ◯   设备名称            (电源)  │
    │      房间 · 在线                 │
    └──────────────────────────────────┘
宽高由外层网格决定（宽度按窗口计算，高度固定 76）。
点击卡片空白处 → 设备详情；点电源键 → 直接开关，不打开详情。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from .. import device_spec
from ..device_spec import switch_state_of
from .controls import PowerButton
from .icons import DeviceGlyph
from .labels import ElidedLabel

CARD_HEIGHT = 76
CARD_MIN_WIDTH = 190
_POWER_SIZE = 30


class DeviceCard(QFrame):
    """单个设备卡片。"""

    clicked = Signal(str)          # dev_id
    power_requested = Signal(str)  # dev_id：请求切换主开关

    def __init__(self, device: Device, width: int = 232, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dev_id = device.dev_id
        self.setObjectName("Card")
        self.setFixedHeight(CARD_HEIGHT)
        self.setFixedWidth(width)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setProperty("selected", "false")
        self.setProperty("offline", "false")

        self._build()
        self.update_device(device)

    # ---------- 构建 ----------
    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 12, 0)
        layout.setSpacing(11)

        self._glyph = DeviceGlyph("generic", 26, True)
        layout.addWidget(self._glyph, 0, Qt.AlignVCenter)

        text_host = QWidget()
        text_box = QVBoxLayout(text_host)
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(3)
        text_box.addStretch(1)

        self._name = ElidedLabel("设备")
        self._name.setObjectName("CardTitle")
        text_box.addWidget(self._name)

        self._meta = ElidedLabel("")
        self._meta.setObjectName("Caption")
        text_box.addWidget(self._meta)
        text_box.addStretch(1)

        layout.addWidget(text_host, 1)

        self._power = PowerButton("unknown", _POWER_SIZE)
        self._power.setCursor(Qt.PointingHandCursor)
        self._power.toggled_requested.connect(
            lambda _state: self.power_requested.emit(self._dev_id)
        )
        layout.addWidget(self._power, 0, Qt.AlignVCenter)

    # ---------- 更新 ----------
    def update_device(self, device: Device) -> None:
        """按设备最新状态刷新卡片。"""
        self._dev_id = device.dev_id
        online = device.online
        self._glyph.set_device(device_spec.device_kind(device), online is not False)

        self._name.setText(device.name or device.dev_id)
        self._name.setToolTip(device.name or device.dev_id)

        room = device.room_name or "未分配房间"
        status_text = {True: "在线", False: "离线", None: "待同步"}[online]
        self._meta.setText(f"{room} · {status_text}")

        found = device_spec.find_switch_service(device)
        if found is not None:
            self._power.set_state(switch_state_of(found[2]))
            self._power.setVisible(True)
        else:
            # 无开关能力的设备（路由、手环、传感器）不显示电源键
            self._power.setVisible(False)

        self.setProperty("offline", "true" if online is False else "false")
        self._refresh_style()

    def _refresh_style(self) -> None:
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def set_selected(self, selected: bool) -> None:
        value = "true" if selected else "false"
        if self.property("selected") == value:
            return
        self.setProperty("selected", value)
        self._refresh_style()

    @property
    def dev_id(self) -> str:
        return self._dev_id

    # ---------- 交互 ----------
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self._dev_id)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        self.setCursor(Qt.PointingHandCursor)
        super().enterEvent(event)
