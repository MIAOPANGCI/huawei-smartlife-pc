"""桌面挂件：常驻桌面的横向设备条（参考 MiHome-Windows 的桌面挂件）。

- 无边框、可拖动、置顶，标题栏显示当前卡片数量
- 支持「锁定」（锁定后不能拖，防误碰）和「收起」（只留标题栏）
- 位置 / 锁定 / 收起状态都持久化，下次启动原样恢复
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from .theme import theme
from .widgets.glyph_icons import make_icon
from .widgets.labels import ElidedLabel
from .widgets.mini_card import MiniDeviceCard
from .widgets.rounded_surface import RoundedSurface

MAX_CARDS = 6
CARD_GAP = 10
TITLEBAR_HEIGHT = 30


class WidgetBoard(QWidget):
    """桌面挂件窗口。"""

    power_requested = Signal(str)
    state_changed = Signal(dict)      # {locked, collapsed, x, y}
    devices_edit_requested = Signal()  # 自选设备 / 排序

    def __init__(
        self,
        board_state: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        # parent 传 None：挂到主窗口下时，主窗口最小化 / 收进托盘会把这个
        # Qt.Tool 子窗口一起隐藏。挂件要独立存在，靠主窗口持有引用来保证存活。
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("华为智慧生活 · 桌面挂件")
        state = board_state or {}
        self._locked = bool(state.get("locked", False))
        self._collapsed = bool(state.get("collapsed", False))
        self._devices: list[Device] = []
        self._cards: dict[str, MiniDeviceCard] = {}
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        self._drag_offset: QPoint | None = None
        self._build()
        self.refresh_icons()
        self._apply_state()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)

        self._surface = RoundedSurface(radius=16)
        shell.addWidget(self._surface)

        layout = QVBoxLayout(self._surface)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)

        layout.addWidget(self._build_titlebar())

        self._cards_row = QWidget()
        self._cards_layout = QHBoxLayout(self._cards_row)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(CARD_GAP)
        layout.addWidget(self._cards_row)

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(TITLEBAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 0, 0)
        layout.setSpacing(4)

        self._title = ElidedLabel("华为智慧生活 · 桌面挂件")
        self._title.setObjectName("BoardTitle")
        self._title.setMinimumWidth(90)
        layout.addWidget(self._title, 1)

        self._edit_btn = self._icon_button("sliders", "自选设备与排序")
        self._edit_btn.clicked.connect(self._on_edit_devices)
        layout.addWidget(self._edit_btn)

        self._lock_btn = self._icon_button("unlock", "锁定位置")
        self._lock_btn.clicked.connect(self.toggle_lock)
        layout.addWidget(self._lock_btn)

        self._collapse_btn = self._icon_button("chevron_down", "收起")
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        layout.addWidget(self._collapse_btn)

        close_btn = self._icon_button("close", "隐藏挂件")
        close_btn.clicked.connect(self.hide)
        layout.addWidget(close_btn)

        bar.mousePressEvent = self._bar_press      # type: ignore[method-assign]
        bar.mouseMoveEvent = self._bar_move        # type: ignore[method-assign]
        bar.mouseReleaseEvent = self._bar_release  # type: ignore[method-assign]
        self._titlebar = bar
        return bar

    def _on_edit_devices(self) -> None:
        QTimer.singleShot(0, self.devices_edit_requested.emit)

    def _icon_button(self, name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("PopoverButton")
        button.setFixedSize(24, 24)
        button.setIconSize(QSize(14, 14))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        self._icon_buttons.append((button, name))
        return button

    def refresh_icons(self) -> None:
        color = theme().palette.text_2
        for button, name in self._icon_buttons:
            button.setIcon(make_icon(name, color, 14, width=1.4))

    def _set_icon(self, button: QPushButton, name: str) -> None:
        for index, (target, _old) in enumerate(self._icon_buttons):
            if target is button:
                self._icon_buttons[index] = (button, name)
                break
        button.setIcon(make_icon(name, theme().palette.text_2, 14, width=1.4))

    # ==================================================================
    # 数据
    # ==================================================================
    def set_devices(self, devices: list[Device]) -> None:
        self._devices = devices[:MAX_CARDS]
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 不要 setParent(None)：那会把它变成顶层窗口，
                # Windows 上会短暂出现一个任务栏按钮（"一堆窗口一闪"）
                widget.hide()
                widget.deleteLater()
        self._cards.clear()

        for device in self._devices:
            card = MiniDeviceCard(device)
            card.power_requested.connect(self.power_requested.emit)
            self._cards_layout.addWidget(card)
            self._cards[device.dev_id] = card

        self._title.setText(f"华为智慧生活 · 桌面挂件 · {len(self._devices)}")
        self._resize_to_content()

    def refresh_device(self, device: Device | None) -> None:
        if device is None:
            return
        card = self._cards.get(device.dev_id)
        if card is not None:
            card.update_device(device)

    def _resize_to_content(self) -> None:
        count = max(1, len(self._devices))
        width = 12 * 2 + count * 152 + max(0, count - 1) * CARD_GAP
        self.setFixedWidth(max(300, width))
        if self._collapsed:
            self.setFixedHeight(8 + TITLEBAR_HEIGHT + 10)
        else:
            self.setFixedHeight(8 + TITLEBAR_HEIGHT + 6 + 62 + 10)

    # ==================================================================
    # 状态
    # ==================================================================
    def toggle_lock(self) -> None:
        self._locked = not self._locked
        self._apply_state()
        self._emit_state()

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._apply_state()
        self._emit_state()

    def _apply_state(self) -> None:
        self._set_icon(self._lock_btn, "lock" if self._locked else "unlock")
        self._lock_btn.setToolTip("已锁定，点击解锁" if self._locked else "锁定位置")
        self._set_icon(
            self._collapse_btn, "chevron_up" if self._collapsed else "chevron_down"
        )
        self._collapse_btn.setToolTip("展开" if self._collapsed else "收起")
        self._cards_row.setVisible(not self._collapsed)
        self._resize_to_content()

    def _emit_state(self) -> None:
        self.state_changed.emit(
            {
                "locked": self._locked,
                "collapsed": self._collapsed,
                "x": self.x(),
                "y": self.y(),
            }
        )

    @property
    def locked(self) -> bool:
        return self._locked

    # ==================================================================
    # 拖动
    # ==================================================================
    def _bar_press(self, event) -> None:
        if event.button() != Qt.LeftButton or self._locked:
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        event.accept()

    def _bar_move(self, event) -> None:
        if self._drag_offset is None or not (event.buttons() & Qt.LeftButton):
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def _bar_release(self, event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._emit_state()
        event.accept()

    def show_near(self, anchor: QWidget | None = None) -> None:
        """首次显示时放到父窗口右下角上方；之后沿用保存的位置。"""
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else self.rect()
        if self.pos().x() < 0 or self.pos().y() < 0 or self.pos() == QPoint(0, 0):
            self.move(
                available.right() - self.width() - 40,
                available.bottom() - self.height() - 80,
            )
        self.show()
        self.raise_()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().moveEvent(event)
        if self.isVisible():
            self._emit_state()

    def closeEvent(self, event) -> None:  # noqa: N802
        # 挂件只隐藏，不销毁：随时可以从托盘再唤出
        event.ignore()
        self.hide()
