"""托盘面板：点系统托盘图标弹出的设备浮层（参考 MiHome-Windows 的托盘项目）。

结构：标题栏（主界面 / 布局切换 / 关闭）+ 设备卡片网格。
用 ``Qt.Popup`` 实现——点面板外面任何地方都会自动收起，符合托盘浮层的直觉。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from .theme import theme
from .widgets.glyph_icons import make_icon
from .widgets.mini_card import MiniDeviceCard
from .widgets.rounded_surface import RoundedSurface

PANEL_WIDTH = 328
MAX_VISIBLE_DEVICES = 12


class TrayPanel(QWidget):
    """托盘弹出面板。"""

    power_requested = Signal(str)
    open_main = Signal()
    layout_changed = Signal(str)      # "grid" | "list"
    devices_edit_requested = Signal()  # 自选设备 / 排序

    def __init__(
        self,
        layout_mode: str = "grid",
        parent: QWidget | None = None,
    ) -> None:
        # 不挂到主窗口下：主窗口最小化 / 收起托盘后，子窗口会被连带隐藏，
        # 托盘面板就弹不出来了。独立顶层窗口 + 自己持有引用即可。
        super().__init__(
            None,
            # NoDropShadowWindowHint：Windows 给浮层加的是矩形投影，
            # 会在圆角外面描出一个方形轮廓，看起来就是"角上方方的"
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("华为智慧生活 · 托盘项目")
        self._layout_mode = "list" if layout_mode == "list" else "grid"
        self._devices: list[Device] = []
        self._cards: dict[str, MiniDeviceCard] = {}
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        self._titlebar: QWidget | None = None
        self._surface = None
        self._build()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        # 圆角由 QSS 画，窗口本身贴合内容，不留阴影预留边（那圈半透明雾就是这么来的）
        shell.setContentsMargins(0, 0, 0, 0)

        # 自绘圆角：QSS 的 border-radius 没有抗锯齿，会在四角留一圈毛边
        surface = RoundedSurface(radius=16)
        self._surface = surface
        shell.addWidget(surface)

        layout = QVBoxLayout(surface)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)

        self._titlebar = self._build_titlebar()
        layout.addWidget(self._titlebar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(300)

        host = QWidget()
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self._scroll.setWidget(host)
        layout.addWidget(self._scroll)

        self.setFixedWidth(PANEL_WIDTH)
        self.refresh_icons()

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 0, 0)
        layout.setSpacing(4)

        title = QLabel("托盘项目")
        title.setObjectName("PopoverTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        home_btn = self._icon_button("home", "打开主界面")
        home_btn.clicked.connect(self._on_open_main)
        layout.addWidget(home_btn)

        edit_btn = self._icon_button("sliders", "自选设备与排序")
        edit_btn.clicked.connect(self._on_edit_devices)
        layout.addWidget(edit_btn)

        self._layout_btn = self._icon_button(
            "list" if self._layout_mode == "grid" else "grid",
            "切换单列 / 双列",
        )
        self._layout_btn.clicked.connect(self._toggle_layout)
        layout.addWidget(self._layout_btn)

        close_btn = self._icon_button("close", "关闭")
        close_btn.clicked.connect(self.hide)
        layout.addWidget(close_btn)
        return bar

    def _icon_button(self, name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("PopoverButton")
        button.setFixedSize(28, 28)
        button.setIconSize(QSize(15, 15))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        self._icon_buttons.append((button, name))
        return button

    def refresh_icons(self) -> None:
        color = theme().palette.text_2
        for button, name in self._icon_buttons:
            button.setIcon(make_icon(name, color, 15, width=1.4))

    # ==================================================================
    # 数据
    # ==================================================================
    def set_devices(self, devices: list[Device]) -> None:
        """按传入顺序渲染（顺序由主窗口按用户自选结果给出）。"""
        self._devices = list(devices[:MAX_VISIBLE_DEVICES])
        self._rebuild()

    def _rebuild(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 不要 setParent(None)：那会把它变成顶层窗口，
                # Windows 上会短暂出现一个任务栏按钮（"一堆窗口一闪"）
                widget.hide()
                widget.deleteLater()
        self._cards.clear()

        if not self._devices:
            hint = QLabel("还没有设备，先在主界面刷新一次。")
            hint.setObjectName("Caption")
            hint.setAlignment(Qt.AlignCenter)
            self._grid.addWidget(hint, 0, 0, 1, 2)
            return

        columns = 2 if self._layout_mode == "grid" else 1
        # 可用宽度 = PANEL_WIDTH - 左右内边距(28) - 滚动条(10) = 290
        width = 140 if columns == 2 else 288
        for index, device in enumerate(self._devices):
            card = MiniDeviceCard(device, width=width)
            card.power_requested.connect(self.power_requested.emit)
            self._grid.addWidget(card, index // columns, index % columns)
            self._cards[device.dev_id] = card

        # 最多显示 4 行，超出滚动；高度按整行算，避免最后一行被切一半
        rows = (len(self._devices) + columns - 1) // columns
        visible_rows = min(rows, 4)
        self._scroll.setFixedHeight(visible_rows * 62 + (visible_rows - 1) * 10 + 4)
        # 单列 / 双列的行数不一样，窗口高度必须跟着内容收放。
        # 少了这一步，切到单列后底部会拖出一大片空白。
        self._fit_to_content()

    def _fit_to_content(self) -> None:
        """按内容重算窗口高度，并保证不越出屏幕。

        不能指望 ``layout.sizeHint()``：滚动区刚 ``setFixedHeight`` 时，
        同一个调用栈里布局的 sizeHint 还是旧值（实测差 72px），
        所以按「内边距 + 标题栏 + 间距 + 滚动区」直接把高度算出来。
        """
        # 要算的是 surface 这一层的内边距，不是窗口外壳的 shell layout
        # （shell 的 margins/spacing 都是 0，按它算会矮 24px，把 surface
        # 的底边连同圆角一起挤出窗口——底角就是这么变成直角的）。
        layout = self._surface.layout() if self._surface is not None else None
        if layout is None or self._titlebar is None:
            return
        margins = layout.contentsMargins()
        height = (
            margins.top()
            + self._titlebar.sizeHint().height()
            + layout.spacing()
            + self._scroll.height()
            + margins.bottom()
        )
        self.setFixedHeight(height)
        if not self.isVisible():
            return
        # 高度变了可能顶出屏幕底部，往回挪一下
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = max(area.left(), min(self.x(), area.right() - self.width()))
        y = max(area.top(), min(self.y(), area.bottom() - self.height()))
        self.move(x, y)
        if not self.isVisible():
            return
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = max(area.left(), min(self.x(), area.right() - self.width()))
        y = max(area.top(), min(self.y(), area.bottom() - self.height()))
        self.move(x, y)

    def refresh_device(self, device: Device | None) -> None:
        if device is None:
            return
        card = self._cards.get(device.dev_id)
        if card is not None:
            card.update_device(device)
        elif any(item.dev_id == device.dev_id for item in self._devices):
            self._rebuild()

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_open_main(self) -> None:
        self.hide()
        self.open_main.emit()

    def _on_edit_devices(self) -> None:
        self.hide()
        # 延后一拍再开模态对话框：Qt.Popup 收起本身会释放鼠标抓取、触发焦点切换，
        # 在同一个事件里启动模态循环容易让 Qt 内部状态错乱。
        QTimer.singleShot(0, self.devices_edit_requested.emit)

    def _toggle_layout(self) -> None:
        self._layout_mode = "list" if self._layout_mode == "grid" else "grid"
        label, name = (
            ("切换单列 / 双列", "list")
            if self._layout_mode == "grid"
            else ("切换单列 / 双列", "grid")
        )
        self._layout_btn.setToolTip(label)
        for index, (button, _old) in enumerate(self._icon_buttons):
            if button is self._layout_btn:
                self._icon_buttons[index] = (button, name)
        self._layout_btn.setIcon(make_icon(name, theme().palette.text_2, 15, width=1.4))
        self.layout_changed.emit(self._layout_mode)
        self._rebuild()

    # ==================================================================
    # 弹出定位
    # ==================================================================
    def popup_at(self, global_pos: QPoint) -> None:
        """在托盘图标附近弹出：优先在光标上方，空间不足则放下方。"""
        self.adjustSize()
        size = self.sizeHint()
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else self.rect()

        x = global_pos.x() - size.width() // 2
        x = max(available.left() + 8, min(x, available.right() - size.width() - 8))
        y = global_pos.y() - size.height() - 8
        if y < available.top() + 8:
            y = global_pos.y() + 12
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        # 弹层隐藏时清掉残影，避免下次弹出看到旧数据
        super().hideEvent(event)
