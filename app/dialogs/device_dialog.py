"""设备详情对话框：状态面板 + 自动生成的属性控件 + 通用命令下发。

控件生成规则（来自 ``app.device_spec``）
------------------------------------
* 开关语义属性 → 滑动开关，切换即下发
* 可调数值 → 滑块（范围/步长按协议元数据推断），松手下发
* 字符串 → 输入框，回车下发
* 累计量 / 元数据 / 标识 → 只读文本
* 复合值 → JSON 预览

对话框是非模态的：可以同时看主窗口日志里的 commandRsp 结果。
遥测到达时只更新控件值（不重建），避免拖滑块拖到一半被打断；
服务结构变化时才整体重建。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from .. import device_spec
from ..device_spec import (
    KIND_JSON,
    KIND_READONLY_NUMBER,
    KIND_READONLY_TEXT,
    KIND_SLIDER,
    KIND_TEXT,
    KIND_TOGGLE,
    PropertySpec,
    ServiceSpec,
    format_number,
    translate_value,
    unit_of,
)
from ..theme import theme
from ..widgets.controls import SwitchButton
from ..widgets.no_wheel import NoWheelComboBox, NoWheelSlider

# 只读标志位的人话解释（鼠标悬停时显示）。
# 这些字段的值是 0/1，但语义不是"能开能关"，直接摆出来只会让人困惑。
_FLAG_HINTS: dict[str, str] = {
    "ctlcapability": "设备支持的控制能力位图，由固件决定，只读",
    "capability": "设备能力位图，由固件决定，只读",
    "capabilities": "设备能力位图，由固件决定，只读",
    "kitauth": "配网鉴权状态：设备是否已完成配网认证，只读",
    "auth": "鉴权状态，只读",
    "authentication": "鉴权状态，只读",
    "support": "能力支持标记，只读",
    "bound": "是否已绑定账号，只读",
    "pairing": "是否处于配对状态，只读",
    "fault": "故障标记，只读",
    "errorcode": "错误码，只读",
}

# 用户能直接操作的控件类型。默认视图只保留这些，
# 加上下面白名单里的少数只读指标。
_ACTIONABLE_KINDS = (KIND_TOGGLE, KIND_SLIDER, KIND_TEXT)

# 日常关心的指标：高级信息关着时也显示。
# 档位/风速这类字段在缺少 min/max 元数据时会被推断成"只读枚举"，
# 但它们本身是用户想调的（净化器风速、风扇档位），不能一起收起来。
_DAILY_METRIC_KEYS = frozenset(
    {
        "temperature",
        "ambienttemp",
        "humidity",
        "battery",
        "pm25",
        "brightness",
        "speed",
        "fanspeed",
        "windspeed",
        "level",
        "gear",
    }
)
from ..widgets.icons import DeviceAvatar, StatusDot
from ..widgets.glyph_icons import make_icon


class DeviceDialog(QDialog):
    """设备控制详情窗。"""

    command_requested = Signal(str, str, dict, dict)  # dev_id, sid, data, overrides
    refresh_requested = Signal(str)
    advanced_changed = Signal(bool)

    def __init__(
        self,
        device: Device,
        parent: QWidget | None = None,
        show_advanced: bool = False,
    ) -> None:
        super().__init__(parent)
        self._device = device
        # 「显示高级信息」：默认关。关着的时候只留用户能动手操作的控件，
        # 以及温度/电量这类日常指标；只读服务（电量统计、网络信息、故障检测）
        # 和通用命令下发属于排查工具，收到这个开关后面。
        self._show_advanced = bool(show_advanced)
        self._rows: list[tuple[PropertySpec, QWidget]] = []
        self._structure: list[tuple[str, str, str]] = []
        self._drag_offset: QPoint | None = None
        self._suppress = False
        self._empty_hint: QLabel | None = None

        self.setWindowTitle(f"设备 · {device.name}")
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowSystemMenuHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 520)
        self._build()
        self.reload(device)
        self._fit_to_content()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(14, 14, 14, 14)

        surface = QFrame()
        surface.setObjectName("DialogSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(Qt.black)
        surface.setGraphicsEffect(shadow)
        shell.addWidget(surface)

        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_titlebar())
        self._header = self._build_header()
        layout.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(18, 4, 18, 12)
        self._body.setSpacing(12)
        self._scroll.setWidget(content)
        layout.addWidget(self._scroll, 1)

        self._command_panel = self._build_command_panel()
        layout.addWidget(self._command_panel)
        self._footer = self._build_footer()
        layout.addWidget(self._footer)
        self._apply_advanced()

    # ---------- 标题栏 ----------
    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("DialogTitleBar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 10, 0)

        title = QLabel("设备控制")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        close = QPushButton()
        close.setObjectName("TitleButtonClose")
        close.setFixedSize(32, 28)
        close.setFlat(True)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("关闭")
        close.setIconSize(QSize(15, 15))
        close.setIcon(make_icon("close", theme().palette.text_2, 15))
        close.clicked.connect(self.close)
        layout.addWidget(close)

        bar.mousePressEvent = self._bar_press  # type: ignore[method-assign]
        bar.mouseMoveEvent = self._bar_move  # type: ignore[method-assign]
        bar.mouseReleaseEvent = self._bar_release  # type: ignore[method-assign]
        self._titlebar = bar
        return bar

    def _bar_press(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _bar_move(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _bar_release(self, event) -> None:
        self._drag_offset = None

    # ---------- 头部 ----------
    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 14)
        layout.setSpacing(12)

        self._avatar = DeviceAvatar("?", "generic", 52)
        layout.addWidget(self._avatar, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        self._name = QLabel("")
        self._name.setObjectName("CardTitle")
        self._name.setStyleSheet("font-size: 17px; font-weight: 700;")
        text_box.addWidget(self._name)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self._dot = StatusDot("unknown")
        meta_row.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._meta = QLabel("")
        self._meta.setObjectName("Caption")
        meta_row.addWidget(self._meta)
        meta_row.addStretch(1)
        text_box.addLayout(meta_row)

        self._detail = QLabel("")
        self._detail.setObjectName("Caption")
        text_box.addWidget(self._detail)

        layout.addLayout(text_box, 1)

        self._power = SwitchButton(False)
        self._power.setToolTip("主开关")
        self._power.toggled.connect(self._on_power_toggled)
        self._power_box = QWidget()
        power_layout = QVBoxLayout(self._power_box)
        power_layout.setContentsMargins(0, 2, 0, 0)
        power_layout.setSpacing(2)
        power_label = QLabel("电源")
        power_label.setObjectName("Caption")
        power_label.setAlignment(Qt.AlignHCenter)
        power_layout.addWidget(self._power, 0, Qt.AlignHCenter)
        power_layout.addWidget(power_label, 0, Qt.AlignHCenter)
        layout.addWidget(self._power_box, 0, Qt.AlignTop)

        return header

    # ---------- 通用命令 ----------
    def _build_command_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("PanelFlat")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 10, 16, 12)
        outer.setSpacing(8)

        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("通用命令下发（协议调试）")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setChecked(False)
        self._advanced_toggle.setCursor(Qt.PointingHandCursor)
        self._advanced_toggle.setStyleSheet(
            "QToolButton { border: none; color: %s; font-size: 12px; }"
            % theme().palette.text_2
        )
        self._advanced_toggle.toggled.connect(self._on_command_panel_toggled)
        outer.addWidget(self._advanced_toggle)

        self._advanced_body = QWidget()
        body = QVBoxLayout(self._advanced_body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(self._mini_label("服务"))
        self._svc_combo = NoWheelComboBox()
        self._svc_combo.setMinimumWidth(140)
        row1.addWidget(self._svc_combo, 1)
        row1.addWidget(self._mini_label("data (JSON)"))
        self._data_edit = QLineEdit("{}")
        self._data_edit.setPlaceholderText('例如 {"on": 1}')
        row1.addWidget(self._data_edit, 2)
        body.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        for placeholder, attr in (
            ("topic（留空=默认）", "_topic_edit"),
            ("to（留空=默认）", "_to_edit"),
            ("method（留空=POST）", "_method_edit"),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            setattr(self, attr, edit)
            row2.addWidget(edit, 1)
        body.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        row3.addWidget(self._mini_label("raw_body"))
        self._raw_body_edit = QLineEdit()
        self._raw_body_edit.setPlaceholderText("可选：直接覆盖整个 body（JSON）")
        row3.addWidget(self._raw_body_edit, 1)
        self._send_button = QPushButton("发送命令")
        self._send_button.setObjectName("Primary")
        self._send_button.clicked.connect(self._on_send_command)
        row3.addWidget(self._send_button)
        body.addLayout(row3)

        self._advanced_body.hide()
        outer.addWidget(self._advanced_body)
        return panel

    def _fit_to_content(self) -> None:
        """按内容定高。

        原先固定 520：只有「电量」一个属性的门锁也只占三分之一，下面全是空。
        不能用 ``self.sizeHint()``——中间的滚动区给出的高度是它自己的默认值，
        不反映里面到底放了多少行，算出来永远是同一个数（内容会被切掉）。
        所以按「标题栏 + 头部 + 内容 + 命令面板 + 底部」逐块累加。
        """
        content = self._scroll.widget()
        body = content.sizeHint().height() if content is not None else 160
        chrome = (
            self._titlebar.sizeHint().height()
            + self._header.sizeHint().height()
            + self._footer.sizeHint().height()
            + 28  # shell 的外边距 14 * 2
        )
        if self._show_advanced and self._command_panel.isVisible():
            chrome += self._command_panel.sizeHint().height()
        self.resize(560, max(380, min(700, chrome + body + 18)))

    # ---------- 高级信息 ----------
    def _apply_advanced(self, rebuild: bool = False) -> None:
        """按「显示高级信息」切换命令面板、提示语与属性列表。"""
        panel = getattr(self, "_command_panel", None)
        if panel is not None:
            panel.setVisible(self._show_advanced)
        hint = getattr(self, "_hint", None)
        if hint is not None:
            hint.setText(
                "命令下发后，设备响应会显示在主窗口「运行日志」中。"
                if self._show_advanced
                else "只显示可操作的选项；只读诊断信息已收起。"
            )
        if rebuild:
            self._refresh_header(self._device)
            self._rebuild_properties(device_spec.build_service_specs(self._device))
            # 布局刚换，sizeHint 要等这一轮事件循环结束才是新的；
            # 立刻取会按旧内容算高度，把新增的行挤到滚动区外面。
            QTimer.singleShot(0, self._fit_to_content)

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._show_advanced = bool(checked)
        self._apply_advanced(rebuild=True)
        self.advanced_changed.emit(self._show_advanced)

    def show_advanced(self) -> bool:
        return self._show_advanced

    def set_show_advanced(self, enabled: bool) -> None:
        """由设置对话框改动时同步过来。

        不重新 emit ``advanced_changed``：调用方（主窗口）已经知道这件事，
        再发一遍会变成「存盘 → 同步 → 再存盘」的来回打转。
        """
        enabled = bool(enabled)
        if enabled == self._show_advanced:
            return
        box = self._advanced_box
        box.blockSignals(True)
        box.setChecked(enabled)
        box.blockSignals(False)
        self._show_advanced = enabled
        self._apply_advanced(rebuild=True)

    def _mini_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Caption")
        return label

    # ---------- 底部 ----------
    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("DialogButtonBar")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 10, 18, 14)
        layout.setSpacing(8)

        self._advanced_box = QCheckBox("显示高级信息")
        self._advanced_box.setToolTip(
            "只读诊断字段（信号强度、电压、原始数据）与协议命令下发默认收起，\n"
            "勾选后一并显示。"
        )
        self._advanced_box.setChecked(self._show_advanced)
        self._advanced_box.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self._advanced_box)

        self._hint = QLabel("")
        self._hint.setObjectName("Caption")
        layout.addWidget(self._hint, 1)

        refresh = QPushButton("刷新状态")
        refresh.setObjectName("Ghost")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(lambda: self.refresh_requested.emit(self._device.dev_id))
        layout.addWidget(refresh)

        close = QPushButton("关闭")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.close)
        layout.addWidget(close)
        return footer

    # ==================================================================
    # 数据刷新
    # ==================================================================
    @property
    def dev_id(self) -> str:
        return self._device.dev_id

    def reload(self, device: Device) -> None:
        """整体刷新：头部信息 + 按需重建属性区。"""
        self._device = device
        self._refresh_header(device)
        specs = device_spec.build_service_specs(device)
        structure = [
            (service.sid, prop.key, prop.kind)
            for service in specs
            for prop in service.properties
        ]
        if structure != self._structure:
            self._structure = structure
            self._rebuild_properties(specs)
        else:
            self._update_values(specs)

    def _refresh_header(self, device: Device) -> None:
        kind = device_spec.device_kind(device)
        self._avatar.set_device(device_spec.device_initial(device), kind)
        self._name.setText(device.name or "未命名设备")

        online = device.online
        state = "online" if online is True else "offline" if online is False else "unknown"
        self._dot.set_state(state)
        online_text = {"online": "在线", "offline": "离线", "unknown": "状态待同步"}[state]
        self._meta.setText(f"{online_text} · {device.room_name or '未分配房间'}")
        bits = []
        if device.model:
            bits.append(f"型号 {device.model}")
        if self._show_advanced:
            # 固件版本和厂商是排查时才会看的，默认不占地方
            if device.firmware_version:
                bits.append(f"固件 {device.firmware_version}")
            if device.manufacturer:
                bits.append(device.manufacturer)
        self._detail.setText(" · ".join(bits) if bits else "—")

        found = device_spec.find_switch_service(device)
        self._power_box.setVisible(found is not None)
        if found is not None:
            self._suppress = True
            self._power.setChecked(device_spec.switch_state_of(found[2]) == "on")
            self._suppress = False

    def _rebuild_properties(self, specs: list[ServiceSpec]) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 不要 setParent(None)：那会把它变成顶层窗口，
                # Windows 上会短暂出现一个任务栏按钮（"一堆窗口一闪"）
                widget.hide()
                widget.deleteLater()
        self._rows.clear()

        if not specs:
            hint = QLabel(
                "该设备暂未上报任何服务状态。\n"
                "可点击「刷新状态」重试，或用下方「通用命令下发」直接指定服务。"
            )
            hint.setObjectName("Body")
            hint.setWordWrap(True)
            self._body.addWidget(hint)
            self._body.addStretch(1)
            self._refresh_service_combo([])
            return

        main_switch = device_spec.find_switch_service(self._device)
        added = 0
        for service in specs:
            # 只读服务（电量统计、网络信息、故障检测、固件更新）默认不展示：
            # 它们是排查工具，不是日常操作。
            # 但定时任务是「设备现在设了几个定时、什么时候执行」，
            # 属于状态而非诊断，默认要看得见。
            if not self._show_advanced and service.readonly:
                has_timer_tasks = any(
                    prop.label == "定时任务" for prop in service.properties
                )
                if not has_timer_tasks:
                    continue
            properties = list(service.properties)
            # 头部已有三态电源开关，服务块里不再重复同一个开关属性
            if main_switch is not None:
                properties = [
                    item
                    for item in properties
                    if not (item.sid == main_switch[0] and item.key == main_switch[1])
                ]
            if not self._show_advanced:
                properties = [item for item in properties if self._is_daily_property(item)]
            if not properties:
                continue
            self._body.addWidget(self._build_service_block(service, properties))
            added += 1
        if added == 0:
            # 例如只有「开关 + 电量统计」的插座：开关挪到了头部，
            # 统计服务默认收起，过滤后一块都不剩——不说一句会让人以为坏了。
            self._empty_hint = QLabel(
                "该设备没有可直接操作的选项。\n"
                "勾选「显示高级信息」可以查看只读状态与协议细节。"
            )
            self._empty_hint.setObjectName("Body")
            self._empty_hint.setWordWrap(True)
            self._body.addWidget(self._empty_hint)
        else:
            self._empty_hint = None
        self._body.addStretch(1)
        self._refresh_service_combo(specs)

    @staticmethod
    def _is_daily_property(prop: PropertySpec) -> bool:
        """这个属性要不要出现在默认视图里。

        能动手的一律保留；只读的只留温度、电量这类日常指标。
        信号强度、电压、原始 JSON 是排查协议用的，归到「显示高级信息」。
        """
        # 定时任务是设备当前状态（设了几个、什么时候执行），不是诊断信息
        if prop.label == "定时任务":
            return True
        if prop.kind in _ACTIONABLE_KINDS:
            return True
        return prop.key.lower() in _DAILY_METRIC_KEYS

    def _build_service_block(
        self, service: ServiceSpec, properties: list[PropertySpec]
    ) -> QWidget:
        block = QFrame()
        block.setObjectName("PanelFlat")
        layout = QVBoxLayout(block)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)

        # 服务只有一个属性、且标签与服务名相同时（如 action/action），
        # 不再重复两行标题，直接给控件行
        merged = len(properties) == 1

        if not merged:
            head = QHBoxLayout()
            title = QLabel(service.label)
            title.setObjectName("SectionTitle")
            head.addWidget(title)
            if service.readonly:
                badge = QLabel("只读")
                badge.setObjectName("Caption")
                head.addWidget(badge)
            head.addStretch(1)
            layout.addLayout(head)

        for prop in properties:
            layout.addWidget(
                self._build_property_row(prop, merged_label=service.label if merged else None)
            )
        return block

    def _build_property_row(
        self, prop: PropertySpec, merged_label: str | None = None
    ) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel(merged_label or prop.label)
        label.setObjectName("Body")
        label.setMinimumWidth(76)
        layout.addWidget(label)

        widget: QWidget
        if prop.kind == KIND_TOGGLE:
            switch = SwitchButton(bool(prop.value))
            switch.toggled.connect(
                lambda checked, s=prop.sid, k=prop.key: self._send(s, {k: 1 if checked else 0})
            )
            widget = switch
            layout.addWidget(widget, 0, Qt.AlignRight)
        elif prop.kind == KIND_SLIDER:
            widget = self._build_slider(prop)
            layout.addWidget(widget, 1)
        elif prop.kind == KIND_TEXT:
            # 定时 / 倒计时这类：给一个明确的数字输入框 + 单位，
            # 只写一个「0」的话既看不出能输入，也不知道是分钟还是秒。
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setSpacing(6)

            edit = QLineEdit(str(prop.value))
            edit.setFixedWidth(76)          # 太窄会被数字撑开，太宽像文本框
            edit.setAlignment(Qt.AlignRight)
            edit.setPlaceholderText("0")
            edit.setToolTip(
                f"点这里输入数字，按回车下发\n原始字段：{prop.key}"
            )
            edit.returnPressed.connect(
                lambda s=prop.sid, k=prop.key, p=prop, e=edit: self._send(
                    s, {k: self._coerce_text_value(p, e.text())}
                )
            )
            holder_layout.addWidget(edit)

            # 一功能一服务的设备里 data 只有一个 on，单位要看服务名
            unit_text = (
                unit_of(prop.key)
                or unit_of(prop.sid)
                or self._duration_unit(prop.key)
                or self._duration_unit(prop.sid)
            )
            if unit_text:
                unit_label = QLabel(unit_text)
                unit_label.setObjectName("Caption")
                unit_label.setToolTip("单位按华为协议惯例，具体以设备为准")
                holder_layout.addWidget(unit_label)

            widget = holder
            layout.addWidget(widget, 0, Qt.AlignRight)
        elif prop.kind == KIND_JSON:
            preview = QPlainTextEdit(json.dumps(prop.value, ensure_ascii=False, indent=2))
            preview.setReadOnly(True)
            preview.setObjectName("LogView")
            preview.setFixedHeight(min(120, 22 * (str(prop.value).count(",") + 2)))
            widget = preview
            layout.addWidget(widget, 1)
        else:  # 只读文本 / 数值
            text = (
                f"{format_number(prop.value)}{unit_of(prop.key) or unit_of(prop.sid)}"
                if prop.kind == KIND_READONLY_NUMBER
                else translate_value(prop.value)
            )
            value = QLabel(text)
            value.setObjectName("Caption" if prop.kind == KIND_READONLY_TEXT else "Value")
            # 多行（定时任务列表）左对齐，单值右对齐贴边
            value.setAlignment(
                (Qt.AlignLeft if "\n" in text else Qt.AlignRight) | Qt.AlignVCenter
            )
            value.setWordWrap(True)
            # 只读项默认不展示（收在「显示高级信息」后面），能看到的都是
            # 排查场景，悬停给出原始字段名便于对照协议
            hint = _FLAG_HINTS.get(prop.key.lower(), "只读，不能下发")
            value.setToolTip(f"{hint}\n原始字段：{prop.key}")
            label.setToolTip(value.toolTip())
            widget = value
            layout.addWidget(widget, 1)

        self._rows.append((prop, widget))
        return row

    def _build_slider(self, prop: PropertySpec) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        step = prop.step if prop.step > 0 else 1.0
        scale = max(1, int(round(1.0 / step))) if step < 1 else int(step)
        minimum = int(round(prop.minimum * scale))
        maximum = int(round(prop.maximum * scale))

        slider = NoWheelSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setSingleStep(1)
        slider.setValue(int(round(float(prop.value) * scale)))

        value_label = QLabel(self._format_slider_value(prop, slider.value() / scale))
        value_label.setObjectName("Value")
        value_label.setFixedWidth(66)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def on_change(raw: int) -> None:
            value_label.setText(self._format_slider_value(prop, raw / scale))

        def on_release() -> None:
            value = slider.value() / scale
            out = int(round(value)) if prop.is_int else round(value, 2)
            self._send(prop.sid, {prop.key: out})

        slider.valueChanged.connect(on_change)
        slider.sliderReleased.connect(on_release)
        # 键盘调节也应有反馈（无 sliderReleased）
        slider.actionTriggered.connect(lambda _a: on_release())

        layout.addWidget(slider, 1)
        layout.addWidget(value_label, 0)
        box._slider = slider  # type: ignore[attr-defined]
        box._scale = scale  # type: ignore[attr-defined]
        return box

    @staticmethod
    def _duration_unit(key: str) -> str:
        """定时类字段的单位。

        华为这几类字段常见是分钟（热水器预约、插座倒计时），但不保证，
        界面上只作为提示，下发时始终发原始数字。
        """
        if str(key).strip().lower() in {
            "timer",
            "timing",
            "delay",
            "delaytime",
            "countdown",
            "appointment",
        }:
            return "分钟"
        return ""

    @staticmethod
    def _coerce_text_value(prop: PropertySpec, text: str) -> object:
        """输入框按原值类型提交。

        定时 / 倒计时这类字段在协议里是整数秒，原样把字符串 ``"30"`` 发上去
        设备多半不认（或者当成非法值）。这里按当前值的类型还原一下。
        """
        text = str(text).strip()
        if isinstance(prop.value, bool):
            return {"true": 1, "false": 0}.get(text.lower(), text)
        if isinstance(prop.value, int):
            try:
                return int(float(text))
            except ValueError:
                return text
        if isinstance(prop.value, float):
            try:
                return float(text)
            except ValueError:
                return text
        return text

    @staticmethod
    def _format_slider_value(prop: PropertySpec, value: float) -> str:
        if prop.is_int:
            text = str(int(round(value)))
        else:
            text = f"{value:.2f}".rstrip("0").rstrip(".")
        # 只写「45」看不出是度还是档位，带上单位
        return f"{text}{unit_of(prop.key) or unit_of(prop.sid)}"

    def _update_values(self, specs: list[ServiceSpec]) -> None:
        """结构未变时，仅同步控件值（不重建，避免打断拖动）。"""
        by_key = {(p.sid, p.key): p for s in specs for p in s.properties}
        for prop, widget in self._rows:
            fresh = by_key.get((prop.sid, prop.key))
            if fresh is None:
                continue
            if prop.kind == KIND_TOGGLE and isinstance(widget, SwitchButton):
                widget.blockSignals(True)
                widget.setChecked(bool(fresh.value))
                widget.blockSignals(False)
            elif prop.kind == KIND_SLIDER and hasattr(widget, "_slider"):
                slider = widget._slider  # type: ignore[attr-defined]
                scale = widget._scale  # type: ignore[attr-defined]
                if not slider.isSliderDown():
                    slider.blockSignals(True)
                    slider.setValue(int(round(float(fresh.value) * scale)))
                    slider.blockSignals(False)
            elif prop.kind == KIND_READONLY_NUMBER and isinstance(widget, QLabel):
                widget.setText(format_number(fresh.value))
            elif prop.kind == KIND_READONLY_TEXT and isinstance(widget, QLabel):
                widget.setText(str(fresh.value))

    def _refresh_service_combo(self, specs: list[ServiceSpec]) -> None:
        self._svc_combo.clear()
        self._svc_labels: dict[str, str] = {}
        for service in specs:
            label = service.label
            if label in self._svc_labels:
                label = service.sid
            self._svc_labels[label] = service.sid
            self._svc_combo.addItem(label)

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_power_toggled(self, checked: bool) -> None:
        if self._suppress:
            return
        found = device_spec.find_switch_service(self._device)
        if found is None:
            return
        sid, key, _value = found
        self._send(sid, {key: 1 if checked else 0})

    def _on_command_panel_toggled(self, expanded: bool) -> None:
        self._advanced_body.setVisible(expanded)
        self.adjustSize()

    def _send(self, sid: str, data: dict, overrides: dict | None = None) -> None:
        self.command_requested.emit(self._device.dev_id, sid, data, overrides or {})

    def _on_send_command(self) -> None:
        label = self._svc_combo.currentText()
        sid = getattr(self, "_svc_labels", {}).get(label, label)
        if not sid:
            self._hint.setText("请先选择要下发的服务。")
            return
        try:
            data = json.loads(self._data_edit.text().strip() or "{}")
            if not isinstance(data, dict):
                raise ValueError("data 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as error:
            self._hint.setText(f"data 不是合法的 JSON 对象：{error}")
            return

        overrides: dict[str, object] = {}
        if self._topic_edit.text().strip():
            overrides["topic"] = self._topic_edit.text().strip()
        if self._to_edit.text().strip():
            overrides["target"] = self._to_edit.text().strip()
        if self._method_edit.text().strip():
            overrides["method"] = self._method_edit.text().strip()
        raw_text = self._raw_body_edit.text().strip()
        if raw_text:
            try:
                raw_body = json.loads(raw_text)
                if not isinstance(raw_body, dict):
                    raise ValueError("raw_body 必须是 JSON 对象")
                overrides["raw_body"] = raw_body
            except (json.JSONDecodeError, ValueError) as error:
                self._hint.setText(f"raw_body 不是合法的 JSON 对象：{error}")
                return

        self._hint.setText("命令已提交，等待设备响应…")
        self._send(sid, data, overrides)
