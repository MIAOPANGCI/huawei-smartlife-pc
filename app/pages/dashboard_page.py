"""看板页：顶部筛选区 + 状态胶囊 + 设备卡片网格。

结构对齐参考实现（MiHome-Windows）::

    ┌──────────────────────────────────────────────────────────┐
    │ 全部设备 ▾   共 17 台设备 · 12 台在线      🔍  ⟳  ▤  ⚙   │
    │ [场景] [全部] [我的家] [父母家]                             │
    │ ● 22.1°C   ● 66%   ● 开启 7   ● 离线 3                    │
    ├──────────────────────────────────────────────────────────┤
    │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
    │ │ ◯ 客厅灯 ⏻│ │ ◯ 热水器 ⏻│ │ ◯ 空调   │ │ ◯ 路由   │      │
    │ └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
    └──────────────────────────────────────────────────────────┘

卡片宽度按窗口宽度动态计算，保证每行铺满且不出现半截卡片。
"""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device, Scene

from .. import device_spec
from ..state import DeviceStore
from ..theme import theme
from ..widgets.device_card import CARD_MIN_WIDTH, DeviceCard
from ..widgets.flow_layout import FlowLayout
from ..widgets.glyph_icons import make_icon

_GRID_GAP = 14
_CARD_MAX_WIDTH = 268


class _Chip(QFrame):
    """顶部状态胶囊：小圆点 + 文本。"""

    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Chip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 11, 4)
        layout.setSpacing(6)

        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(f"background: {color}; border-radius: 3px;")
        layout.addWidget(dot, 0, Qt.AlignVCenter)

        self._label = QLabel(text)
        self._label.setObjectName("ChipText")
        layout.addWidget(self._label, 0, Qt.AlignVCenter)


class FlowContainer(QWidget):
    """承载 FlowLayout 的容器：高度随内容行数变化。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = FlowLayout(self, margin=0, h_spacing=_GRID_GAP, v_spacing=_GRID_GAP)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

    @property
    def flow(self) -> FlowLayout:
        return self._flow

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 接口
        return self._flow.heightForWidth(width)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        needed = self._flow.heightForWidth(self.width())
        if needed > 0 and self.minimumHeight() != needed:
            self.setMinimumHeight(needed)


class SceneCard(QFrame):
    """场景卡片：名称 + 家庭 + 执行按钮。"""

    execute_requested = Signal(object)

    def __init__(self, scene: Scene, home_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = scene
        self.setObjectName("Card")
        self.setFixedWidth(232)
        self.setMinimumHeight(96)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        name = QLabel(scene.name or "未命名场景")
        name.setObjectName("CardTitle")
        name.setWordWrap(True)
        layout.addWidget(name)

        meta = QLabel(home_name)
        meta.setObjectName("Caption")
        layout.addWidget(meta)

        layout.addStretch(1)
        button = QPushButton("执行场景")
        button.setObjectName("Ghost")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda: self.execute_requested.emit(self._scene))
        layout.addWidget(button, 0, Qt.AlignLeft)


def _escape_keep_indent(text: str) -> str:
    """转义 HTML 并保留行首缩进。

    HTML 会把连续空格折叠成一个，原始协议那种缩进 JSON 贴进日志就成一坨了。
    """
    lines: list[str] = []
    for line in str(text).split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        lines.append("&nbsp;" * indent + html.escape(stripped) if indent else html.escape(line))
    return "<br/>".join(lines)


class DashboardPage(QWidget):
    """主看板。"""

    refresh_requested = Signal()
    device_open_requested = Signal(str)
    device_power_requested = Signal(str)
    scenes_requested = Signal()
    scene_execute_requested = Signal(object)
    selection_changed = Signal(object)   # Device | None
    logout_requested = Signal()
    settings_requested = Signal()
    mqtt_toggle_requested = Signal()

    VIEW_DEVICES = 0
    VIEW_SCENES = 1
    VIEW_LOGS = 2

    def __init__(self, store: DeviceStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._cards: dict[str, DeviceCard] = {}
        self._tab_buttons: dict[str, QPushButton] = {}
        self._filter: str | None = None       # None=全部；home:<id>；other
        self._search_text = ""
        self._selected_dev_id: str | None = None
        self._mqtt_connected = False
        self._account = ""
        self._card_width = 232
        self._tool_icons: list[tuple[QPushButton, str]] = []
        # 日志分两档：常用信息常显，设备遥测（每秒都可能来）默认折叠。
        # 缓存一份完整条目，切换「显示遥测」时整页重建，切回去历史还在。
        self._log_entries: list[tuple[str, str, str]] = []
        self._log_verbose = False
        self._build()
        self.refresh_icons()
        self._apply_view(self.VIEW_DEVICES)

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 0)
        root.setSpacing(8)

        root.addWidget(self._build_header())
        self._stack_widget = self._build_stack()
        root.addWidget(self._stack_widget, 1)

    # ---------- 顶部 ----------
    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 第一行：标题 + 统计 + 工具
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self._title_button = QPushButton("全部设备")
        self._title_button.setObjectName("FamilyTitle")
        self._title_button.setCursor(Qt.PointingHandCursor)
        self._title_button.setFlat(True)
        self._title_button.clicked.connect(self._show_title_menu)
        row.addWidget(self._title_button, 0, Qt.AlignVCenter)

        self._summary = QLabel("")
        self._summary.setObjectName("PageSummary")
        row.addWidget(self._summary, 0, Qt.AlignBottom)
        row.addStretch(1)

        self._search = QLineEdit()
        self._search.setObjectName("SearchBox")
        self._search.setPlaceholderText("搜索设备 / 房间")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(172)
        self._search.textChanged.connect(self._on_search_changed)
        row.addWidget(self._search, 0, Qt.AlignVCenter)

        self._refresh_button = self._tool_button("refresh", "刷新设备列表")
        self._refresh_button.clicked.connect(self.refresh_requested.emit)
        row.addWidget(self._refresh_button, 0, Qt.AlignVCenter)

        self._log_button = self._tool_button("logs", "运行日志")
        self._log_button.clicked.connect(lambda: self._apply_view(self.VIEW_LOGS))
        row.addWidget(self._log_button, 0, Qt.AlignVCenter)

        self._settings_button = self._tool_button("settings", "设置")
        self._settings_button.clicked.connect(self.settings_requested.emit)
        row.addWidget(self._settings_button, 0, Qt.AlignVCenter)

        layout.addLayout(row)

        # 第二行：胶囊 Tab
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(6)
        self._tab_box = tab_row
        layout.addLayout(tab_row)

        # 第三行：状态胶囊
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(8)
        self._chip_box = chip_row
        layout.addLayout(chip_row)

        return header

    def _tool_button(self, name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("ToolButton")
        button.setFixedSize(32, 32)
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        button.setIconSize(QSize(18, 18))
        self._tool_icons.append((button, name))
        return button

    def refresh_icons(self) -> None:
        """主题切换后重建工具图标（图标色跟随文字色）。"""
        color = theme().palette.text_2
        for button, name in self._tool_icons:
            button.setIcon(make_icon(name, color, 18, width=1.5))

    # ---------- 内容页 ----------
    def _build_stack(self) -> QWidget:
        stack = QStackedWidget()

        # 设备网格
        self._device_scroll = QScrollArea()
        self._device_scroll.setWidgetResizable(True)
        self._device_scroll.setFrameShape(QFrame.NoFrame)
        self._device_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        device_layout = QVBoxLayout(content)
        device_layout.setContentsMargins(0, 2, 0, 24)
        device_layout.setSpacing(0)
        self._device_container = FlowContainer()
        device_layout.addWidget(self._device_container)
        device_layout.addStretch(1)
        self._device_scroll.setWidget(content)
        stack.addWidget(self._device_scroll)

        # 场景
        scene_wrap = QWidget()
        scene_layout = QVBoxLayout(scene_wrap)
        scene_layout.setContentsMargins(0, 2, 0, 24)
        scene_layout.setSpacing(10)
        self._scene_status = QLabel("正在加载场景…")
        self._scene_status.setObjectName("Body")
        scene_layout.addWidget(self._scene_status)
        self._scene_container = FlowContainer()
        scene_layout.addWidget(self._scene_container)
        scene_layout.addStretch(1)
        scene_scroll = QScrollArea()
        scene_scroll.setWidgetResizable(True)
        scene_scroll.setFrameShape(QFrame.NoFrame)
        scene_scroll.setWidget(scene_wrap)
        stack.addWidget(scene_scroll)

        # 日志
        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        log_layout.setContentsMargins(0, 2, 0, 24)
        log_layout.setSpacing(8)
        head = QHBoxLayout()
        caption = QLabel("操作、命令响应与错误都会记在这里；设备遥测默认折叠。")
        caption.setObjectName("Caption")
        head.addWidget(caption)
        head.addStretch(1)
        self._log_verbose_box = QCheckBox("显示遥测")
        self._log_verbose_box.setToolTip(
            "设备主动上报的状态（如插座电流、摄像头访客点）每秒都可能来一条，\n"
            "默认折叠以免刷屏。排查问题时再勾选。"
        )
        self._log_verbose_box.toggled.connect(self.set_log_verbose)
        head.addWidget(self._log_verbose_box)
        clear_button = QPushButton("清空")
        clear_button.setObjectName("Ghost")
        clear_button.clicked.connect(self.clear_logs)
        head.addWidget(clear_button)
        log_layout.addLayout(head)

        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("LogView")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(3000)
        log_layout.addWidget(self._log_view, 1)
        stack.addWidget(log_wrap)

        return stack

    # ==================================================================
    # 视图与筛选
    # ==================================================================
    def _apply_view(self, view: int) -> None:
        self._stack_widget.setCurrentIndex(view)
        self._sync_tool_state()
        if view == self.VIEW_LOGS:
            self._title_button.setText("运行日志  ▾")
            self._summary.setText("")
            for button in self._tab_buttons.values():
                button.setChecked(False)
            return
        if view == self.VIEW_SCENES:
            self.scenes_requested.emit()
            return
        self._update_header()

    def _sync_tool_state(self) -> None:
        view = self._stack_widget.currentIndex()
        for button, active in (
            (self._log_button, view == self.VIEW_LOGS),
        ):
            button.setProperty("active", "true" if active else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    def _apply_filter(self, key: str | None) -> None:
        """key: None=全部；home:<id>；other。"""
        self._filter = key
        self._stack_widget.setCurrentIndex(self.VIEW_DEVICES)
        self._sync_tabs()
        self._sync_tool_state()
        self._update_header()
        self.rebuild_cards()

    def _sync_tabs(self) -> None:
        target = "all" if self._filter is None else self._filter
        for key, button in self._tab_buttons.items():
            button.setChecked(key == target)

    def _update_header(self) -> None:
        if self._filter is None:
            self._title_button.setText("全部设备  ▾")
        elif self._filter == "other":
            self._title_button.setText("其他设备  ▾")
        else:
            home_id = self._filter.split(":", 1)[1]
            self._title_button.setText(f"{self._store.home_name(home_id)}  ▾")

        devices = self._filtered_devices()
        online = sum(1 for device in devices if device.online is True)
        self._summary.setText(f"共 {len(devices)} 台设备 · {online} 台在线")
        self._refresh_chips()

    def _show_title_menu(self) -> None:
        menu = QMenu(self)
        action = QAction("全部设备", menu)
        action.triggered.connect(lambda: self._apply_filter(None))
        menu.addAction(action)
        menu.addSeparator()
        for home, _count in self._store.homes_with_counts():
            act = QAction(home.name, menu)
            act.triggered.connect(
                lambda _checked=False, h=home.home_id: self._apply_filter(f"home:{h}")
            )
            menu.addAction(act)
        if self._has_orphans():
            act = QAction("其他设备", menu)
            act.triggered.connect(lambda: self._apply_filter("other"))
            menu.addAction(act)
        menu.addSeparator()
        scene_action = QAction("智能场景", menu)
        scene_action.triggered.connect(lambda: self._apply_view(self.VIEW_SCENES))
        menu.addAction(scene_action)
        log_action = QAction("运行日志", menu)
        log_action.triggered.connect(lambda: self._apply_view(self.VIEW_LOGS))
        menu.addAction(log_action)

        origin = self._title_button.mapToGlobal(QPoint(0, self._title_button.height() + 4))
        menu.exec(origin)

    # ==================================================================
    # 数据
    # ==================================================================
    def reload_homes(self) -> None:
        """重建 Tab（家庭变化后调用）。"""
        while self._tab_box.count():
            item = self._tab_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 不要 setParent(None)：那会把它变成顶层窗口，
                # Windows 上会短暂出现一个任务栏按钮（"一堆窗口一闪"）
                widget.hide()
                widget.deleteLater()
        self._tab_buttons.clear()

        self._tab_box.addWidget(self._make_tab("场景", "scenes"))
        self._tab_box.addWidget(self._make_tab("全部", "all"))
        for home, _count in self._store.homes_with_counts():
            self._tab_box.addWidget(self._make_tab(home.name, f"home:{home.home_id}"))
        if self._has_orphans():
            self._tab_box.addWidget(self._make_tab("其他设备", "other"))
        self._tab_box.addStretch(1)
        self._sync_tabs()

    def _make_tab(self, text: str, key: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("TabButton")
        button.setCheckable(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(30)
        button.clicked.connect(lambda _checked=False, k=key: self._on_tab_clicked(k))
        self._tab_buttons[key] = button
        return button

    def _on_tab_clicked(self, key: str) -> None:
        if key == "scenes":
            for button_key, button in self._tab_buttons.items():
                button.setChecked(button_key == "scenes")
            self._apply_view(self.VIEW_SCENES)
            return
        if key == "all":
            self._apply_filter(None)
        else:
            self._apply_filter(key)

    def _has_orphans(self) -> bool:
        assigned = {
            device.dev_id
            for home in self._store.homes
            for device in self._store.devices_of_home(home.home_id)
        }
        return any(device.dev_id not in assigned for device in self._store.all())

    def _orphan_devices(self) -> list[Device]:
        assigned = {
            device.dev_id
            for home in self._store.homes
            for device in self._store.devices_of_home(home.home_id)
        }
        return [d for d in self._store.all() if d.dev_id not in assigned]

    def _filtered_devices(self) -> list[Device]:
        devices = self._store.all()
        if self._filter == "other":
            devices = self._orphan_devices()
        elif self._filter is not None:
            home_id = self._filter.split(":", 1)[1]
            devices = [d for d in devices if d.home_id == home_id]

        keyword = self._search_text.strip().lower()
        if keyword:
            devices = [
                d
                for d in devices
                if keyword in (d.name or "").lower()
                or keyword in (d.room_name or "").lower()
                or keyword in (d.model or "").lower()
            ]
        # 排序：在线 → 状态未知 → 离线；同组内有房间的靠前，再按房间/名称排
        def _rank(device: Device) -> tuple:
            online_rank = 0 if device.online is True else (1 if device.online is None else 2)
            has_room = 0 if device.room_name else 1
            return (online_rank, has_room, device.room_name or "", device.name or "")

        devices.sort(key=_rank)
        return devices

    # ---------- 卡片 ----------
    def rebuild_cards(self) -> None:
        layout = self._device_container.flow
        layout.clear()
        self._cards.clear()

        devices = self._filtered_devices()
        if not devices:
            hint = QLabel(
                "没有匹配的设备。试试清空搜索框，或点右上角刷新。"
                if self._search_text
                else "暂无设备。点右上角刷新重新拉取设备列表。"
            )
            hint.setObjectName("Body")
            hint.setAlignment(Qt.AlignCenter)
            hint.setMinimumHeight(140)
            layout.addWidget(hint)
            return

        for device in devices:
            card = DeviceCard(device, self._card_width)
            card.clicked.connect(self._on_card_clicked)
            card.power_requested.connect(self.device_power_requested.emit)
            if device.dev_id == self._selected_dev_id:
                card.set_selected(True)
            layout.addWidget(card)
            self._cards[device.dev_id] = card

        self._recalc_card_width()
        self._update_container_height()

    def _update_container_height(self) -> None:
        container = self._device_container
        needed = container.flow.heightForWidth(container.width())
        if needed > 0:
            container.setMinimumHeight(needed)

    def _recalc_card_width(self) -> None:
        """按可用宽度算列数与卡片宽度，保证铺满且不留半截卡片。"""
        viewport = self._device_scroll.viewport().width()
        if viewport <= 0:
            return
        available = viewport - 2  # 预留滚动条
        columns = max(2, (available + _GRID_GAP) // (CARD_MIN_WIDTH + _GRID_GAP))
        width = int((available - (columns - 1) * _GRID_GAP) / columns)
        width = max(CARD_MIN_WIDTH, min(_CARD_MAX_WIDTH, width))
        if width == self._card_width:
            return
        self._card_width = width
        for card in self._cards.values():
            card.setFixedWidth(width)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._recalc_card_width()
        self._update_container_height()

    # ---------- 状态胶囊 ----------
    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 不要 setParent(None)：那会把它变成顶层窗口，
                # Windows 上会短暂出现一个任务栏按钮（"一堆窗口一闪"）
                widget.hide()
                widget.deleteLater()

    def _refresh_chips(self) -> None:
        self._clear_layout(self._chip_box)

        devices = self._filtered_devices()
        palette = theme().palette
        chips: list[tuple[str, str]] = []

        temperature = self._first_value(devices, ("temperature", "ambienttemp"))
        if temperature is not None:
            chips.append((f"{temperature:g}°C", palette.warn))
        humidity = self._first_value(devices, ("humidity",))
        if humidity is not None:
            chips.append((f"{humidity:g}%", "#3aa9ff"))

        on_count = 0
        for device in devices:
            found = device_spec.find_switch_service(device)
            if found is not None and found[2] in (1, True):
                on_count += 1
        if on_count:
            chips.append((f"开启 {on_count}", palette.accent))

        offline = sum(1 for device in devices if device.online is False)
        if offline:
            chips.append((f"离线 {offline}", palette.text_3))

        for text, color in chips:
            self._chip_box.addWidget(_Chip(text, color), 0, Qt.AlignVCenter)
        self._chip_box.addStretch(1)

    @staticmethod
    def _first_value(devices: list[Device], keys: tuple[str, ...]) -> float | None:
        for device in devices:
            for service in device.services.values():
                for key, value in (service.data or {}).items():
                    if str(key).lower() in keys and isinstance(value, (int, float)):
                        if not isinstance(value, bool):
                            return float(value)
        return None

    # ==================================================================
    # 外部更新
    # ==================================================================
    def update_stats(self, mqtt_connected: bool) -> None:
        self._mqtt_connected = mqtt_connected
        self._update_header()

    def update_device(self, dev_id: str) -> None:
        device = self._store.get(dev_id)
        card = self._cards.get(dev_id)
        if device is not None and card is not None:
            card.update_device(device)
        self._refresh_chips()

    def set_mqtt_state(self, connected: bool) -> None:
        self._mqtt_connected = connected
        self._refresh_chips()

    def set_account(self, account: str) -> None:
        self._account = account

    def set_title(self, title: str, subtitle: str = "") -> None:
        """保留接口：标题与统计由当前筛选状态自动生成。"""
        if self._stack_widget.currentIndex() == self.VIEW_DEVICES:
            self._update_header()

    def set_busy(self, busy: bool) -> None:
        self._refresh_button.setEnabled(not busy)
        self._refresh_button.setToolTip("正在刷新…" if busy else "刷新设备列表")

    # ---------- 场景 ----------
    def show_scenes(self, scenes: list[Scene]) -> None:
        layout = self._scene_container.flow
        layout.clear()

        if not scenes:
            self._scene_status.setText(
                "未获取到场景。云端未返回场景列表，或该账号下没有手动场景。"
            )
            return
        self._scene_status.setText(f"共 {len(scenes)} 个智能场景")
        for scene in scenes:
            home_name = self._store.home_name(scene.home_id or "")
            card = SceneCard(scene, home_name)
            card.execute_requested.connect(self.scene_execute_requested.emit)
            layout.addWidget(card)

    # ---------- 日志 ----------
    _LOG_CACHE_LIMIT = 1500

    def append_log(self, message: str, kind: str = "info") -> None:
        """追加一条运行日志。

        kind="detail" 是设备遥测：默认不显示，勾选「显示遥测」后才出现。
        """
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_entries.append((stamp, message, kind))
        if len(self._log_entries) > self._LOG_CACHE_LIMIT:
            del self._log_entries[: len(self._log_entries) - self._LOG_CACHE_LIMIT]
        if kind == "detail" and not self._log_verbose:
            return
        self._render_log(stamp, message, kind)

    def _render_log(self, stamp: str, message: str, kind: str) -> None:
        palette = theme().palette
        color = {
            "success": palette.ok,
            "warning": palette.warn,
            "danger": palette.danger,
            "tag": palette.accent,
            "detail": palette.text_3,
        }.get(kind, palette.text_2)
        safe = _escape_keep_indent(message)
        self._log_view.appendHtml(
            f'<span style="color:{palette.text_3}">{stamp}</span> '
            f'<span style="color:{color}">{safe}</span>'
        )

    def set_log_verbose(self, enabled: bool) -> None:
        """切换是否显示设备遥测，并按缓存重建整个日志视图。"""
        enabled = bool(enabled)
        if enabled == self._log_verbose:
            return
        self._log_verbose = enabled
        # 勾选框与内部状态保持一致：从代码调用本方法时也要同步 UI，
        # 否则会出现「日志已经展开了，勾选框还是空的」。
        box = getattr(self, "_log_verbose_box", None)
        if box is not None and box.isChecked() != enabled:
            box.blockSignals(True)
            box.setChecked(enabled)
            box.blockSignals(False)
        self._log_view.clear()
        for stamp, message, kind in self._log_entries:
            if kind == "detail" and not enabled:
                continue
            self._render_log(stamp, message, kind)
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def log_verbose(self) -> bool:
        return self._log_verbose

    def clear_logs(self) -> None:
        """清空日志：缓存一起清，否则切回「显示遥测」会把旧条目又渲染出来。"""
        self._log_entries.clear()
        self._log_view.clear()

    # ---------- 交互 ----------
    def _on_search_changed(self, text: str) -> None:
        self._search_text = text
        if self._stack_widget.currentIndex() == self.VIEW_DEVICES:
            self.rebuild_cards()
            self._refresh_chips()

    def _on_card_clicked(self, dev_id: str) -> None:
        device = self._store.get(dev_id)
        if device is None:
            return
        self.select_device(dev_id)
        self.device_open_requested.emit(dev_id)

    def select_device(self, dev_id: str | None) -> None:
        previous = self._selected_dev_id
        self._selected_dev_id = dev_id
        if previous and previous in self._cards:
            self._cards[previous].set_selected(False)
        if dev_id and dev_id in self._cards:
            self._cards[dev_id].set_selected(True)
        self.selection_changed.emit(self._store.get(dev_id) if dev_id else None)

    def show_devices(self) -> None:
        self._apply_view(self.VIEW_DEVICES)

    def show_view(self, key: str) -> None:
        """按设置的键切换视图：all / scenes / logs / home:<id> / other。"""
        if key == "scenes":
            self._on_tab_clicked("scenes")
            return
        if key == "logs":
            self._apply_view(self.VIEW_LOGS)
            return
        if key.startswith("home:"):
            home_id = key.split(":", 1)[1]
            if any(home.home_id == home_id for home in self._store.homes):
                self._apply_filter(key)
                return
            self._apply_filter(None)   # 家庭已不存在，回退到全部
            return
        if key == "other":
            self._apply_filter("other")
            return
        self._apply_filter(None)
