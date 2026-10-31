"""桌面挂件：常驻桌面的横向设备条（参考 MiHome-Windows 的桌面挂件）。

- 无边框、可拖动、置顶，标题栏显示当前卡片数量
- 支持「锁定」（锁定后不能拖，防误碰）和「收起」（只留标题栏）
- 拖到屏幕左 / 右 / 上边缘附近会自动吸附，并缩进屏幕外只留一条窄边；
  鼠标碰到窄边滑出，移开后自动缩回（贴边自动隐藏）
- 位置 / 锁定 / 收起 / 吸附边都持久化，下次启动原样恢复
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from .theme import theme
from .widgets import motion
from .widgets.glyph_icons import make_icon
from .widgets.labels import ElidedLabel
from .widgets.mini_card import MiniDeviceCard
from .widgets.rounded_surface import RoundedSurface

MAX_CARDS = 6
CARD_GAP = 10
TITLEBAR_HEIGHT = 30

# ---- 贴边自动隐藏 ----
EDGE_SNAP = 28          # 松手时距屏幕边缘多少像素内就吸附
EDGE_PEEK = 6           # 缩进去之后留在屏幕里的窄边宽度（还能摸到的那一条）
EDGE_POLL_MS = 100      # 鼠标位置轮询间隔
EDGE_HIDE_DELAY = 650   # 鼠标移开后多久缩回
EDGE_ANIM_MS = 150      # 滑出 / 缩回的时长
EDGES = ("left", "right", "top")


def _clamp(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return max(low, min(high, value))


class WidgetBoard(QWidget):
    """桌面挂件窗口。"""

    power_requested = Signal(str)
    state_changed = Signal(dict)      # {locked, collapsed, x, y, edge}
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
        edge = state.get("edge")
        self._edge: str | None = edge if edge in EDGES else None
        self._peeked = False                # 当前是否缩在屏幕外
        self._expanded_pos: QPoint | None = None
        self._away_ms = 0                   # 鼠标已离开多久（累计）
        self._anim: QPropertyAnimation | None = None
        self._animating = False
        self._devices: list[Device] = []
        self._cards: dict[str, MiniDeviceCard] = {}
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        self._drag_offset: QPoint | None = None
        self._build()
        self._build_timers()
        self.refresh_icons()
        self._apply_state()

    def _build_timers(self) -> None:
        # 无边框 Qt.Tool 窗口收不到可靠的 enter / leave（窗口大部分在屏幕外时
        # 更不可靠），所以用轮询全局光标来决定滑出与缩回。
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(EDGE_POLL_MS)
        self._edge_timer.timeout.connect(self._poll_cursor)

        # 拖动时 moveEvent 一帧一发，落盘要节流，否则拖一次写几十次配置文件
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(400)
        self._state_timer.timeout.connect(self._emit_state)

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
        # 宽度随卡片数量变，贴右边 / 左右居中的挂件要重新对齐
        self._realign()

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
        # 收起 / 展开会改高度，贴顶部时位置要跟着重算
        self._realign()

    def _state_pos(self) -> QPoint:
        """落盘用的坐标：永远记「完整展开」的位置。

        缩回后窗口在屏幕外，把那个坐标存下来，下次启动挂件就跑到屏幕外了。
        """
        if self._edge is not None and self._expanded_pos is not None:
            return self._expanded_pos
        return self.pos()

    def _emit_state(self) -> None:
        self._state_timer.stop()
        pos = self._state_pos()
        self.state_changed.emit(
            {
                "locked": self._locked,
                "collapsed": self._collapsed,
                "x": pos.x(),
                "y": pos.y(),
                "edge": self._edge,
            }
        )

    def _schedule_state(self) -> None:
        self._state_timer.start()

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def edge(self) -> str | None:
        return self._edge

    @property
    def peeked(self) -> bool:
        return self._peeked

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
            self._maybe_dock()   # 松手时看要不要吸附到屏幕边缘
        event.accept()

    # ==================================================================
    # 贴边自动隐藏
    # ==================================================================
    def _screen_area(self) -> QRect:
        """窗口所在屏幕的可用区域（已扣掉任务栏）。"""
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else None
        if screen is None:
            screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return self.rect()
        return screen.availableGeometry()

    def _edge_anchor(self, edge: str, area: QRect) -> QPoint:
        """贴边状态下「完整展开」应该摆在哪。"""
        source = self._expanded_pos if self._expanded_pos is not None else self.pos()
        x, y = source.x(), source.y()
        if edge == "left":
            x = area.left()
            y = _clamp(y, area.top(), area.bottom() - self.height() + 1)
        elif edge == "right":
            x = area.right() - self.width() + 1
            y = _clamp(y, area.top(), area.bottom() - self.height() + 1)
        else:  # top
            y = area.top()
            x = _clamp(x, area.left(), area.right() - self.width() + 1)
        return QPoint(x, y)

    def _peek_target(self) -> QPoint:
        """缩进屏幕外、只留 EDGE_PEEK 像素时窗口该在哪。"""
        edge = self._edge
        area = self._screen_area()
        if edge is None:
            return self.pos()
        anchor = self._edge_anchor(edge, area)
        x, y = anchor.x(), anchor.y()
        if edge == "left":
            x = area.left() - self.width() + EDGE_PEEK
        elif edge == "right":
            x = area.right() - EDGE_PEEK + 1
        else:
            y = area.top() - self.height() + EDGE_PEEK
        return QPoint(x, y)

    def _slide_to(self, target: QPoint) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
            self._animating = False
        if not motion.enabled() or target == self.pos():
            self.move(target)
            return
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(EDGE_ANIM_MS)
        anim.setStartValue(self.pos())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._on_slide_done)
        self._anim = anim
        self._animating = True
        anim.start()

    def _on_slide_done(self) -> None:
        self._animating = False
        self._anim = None
        self._away_ms = 0

    def _realign(self) -> None:
        """尺寸变化后重新摆正（贴边时宽高改了必须重算坐标）。"""
        if self._edge is None or not self.isVisible():
            return
        area = self._screen_area()
        self._expanded_pos = self._edge_anchor(self._edge, area)
        self._slide_to(self._peek_target() if self._peeked else self._expanded_pos)

    def _maybe_dock(self) -> None:
        """松手后：靠近屏幕边缘就吸附并启用自动隐藏，否则脱离。"""
        area = self._screen_area()
        geo = self.frameGeometry()
        candidates = [
            ("left", abs(geo.left() - area.left())),
            ("right", abs(area.right() - geo.right())),
            ("top", abs(geo.top() - area.top())),
        ]
        edge, distance = min(candidates, key=lambda item: item[1])
        if distance > EDGE_SNAP:
            self._edge = None
            self._peeked = False
            self._expanded_pos = self.pos()
            self._away_ms = 0
            self._emit_state()
            return
        self._edge = edge
        self._peeked = False
        self._expanded_pos = self._edge_anchor(edge, area)
        self._away_ms = 0
        self._slide_to(self._expanded_pos)
        self._emit_state()

    def _collapse_to_edge(self) -> None:
        if self._edge is None:
            return
        self._peeked = True
        self._slide_to(self._peek_target())

    def _expand_from_edge(self) -> None:
        if self._edge is None:
            return
        self._peeked = False
        area = self._screen_area()
        self._expanded_pos = self._edge_anchor(self._edge, area)
        self._away_ms = 0
        self._slide_to(self._expanded_pos)

    def _poll_cursor(self) -> None:
        """轮询全局光标：碰到窄边就滑出，移开久了就缩回。"""
        if self._edge is None or not self.isVisible():
            return
        if self._animating or self._drag_offset is not None:
            return

        cursor = QCursor.pos()
        area = self._screen_area()

        if self._peeked:
            # 只剩 EDGE_PEEK 像素在屏幕内，热区就是露出来那条（留 2px 余量）
            visible = self.frameGeometry().intersected(area)
            if visible.adjusted(-2, -2, 2, 2).contains(cursor):
                self._expand_from_edge()
            return

        hot = self.frameGeometry().adjusted(-4, -4, 4, 4)
        if hot.contains(cursor) or QApplication.activePopupWidget() is not None:
            self._away_ms = 0
            return

        self._away_ms += EDGE_POLL_MS
        if self._away_ms >= EDGE_HIDE_DELAY:
            self._away_ms = 0
            self._collapse_to_edge()

    def show_near(self, anchor: QWidget | None = None) -> None:
        """首次显示时放到父窗口右下角上方；之后沿用保存的位置。"""
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else self.rect()
        self._away_ms = 0
        if self._edge is not None:
            # 上次贴在边上：先完整展开给用户看，鼠标移开后自己缩回去
            self._peeked = False
            self._expanded_pos = self._edge_anchor(self._edge, available)
            self.move(self._expanded_pos)
        elif self.pos().x() < 0 or self.pos().y() < 0 or self.pos() == QPoint(0, 0):
            self.move(
                available.right() - self.width() - 40,
                available.bottom() - self.height() - 80,
            )
        self.show()
        self.raise_()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._away_ms = 0
        self._edge_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().hideEvent(event)
        self._edge_timer.stop()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().moveEvent(event)
        if not self.isVisible():
            return
        if self._edge is not None or self._animating:
            # 贴边后的坐标由 _emit_state 里的展开位置负责；缩回动画的
            # 中间帧更不能落盘，否则下次启动窗口会在屏幕外
            return
        self._schedule_state()

    def closeEvent(self, event) -> None:  # noqa: N802
        # 挂件只隐藏，不销毁：随时可以从托盘再唤出
        event.ignore()
        self.hide()
