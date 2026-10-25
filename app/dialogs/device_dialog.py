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

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
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
)
from ..theme import theme
from ..widgets.controls import SwitchButton
from ..widgets.icons import DeviceAvatar, StatusDot
from ..widgets.glyph_icons import make_icon


class DeviceDialog(QDialog):
    """设备控制详情窗。"""

    command_requested = Signal(str, str, dict, dict)  # dev_id, sid, data, overrides
    refresh_requested = Signal(str)

    def __init__(self, device: Device, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._device = device
        self._rows: list[tuple[PropertySpec, QWidget]] = []
        self._structure: list[tuple[str, str, str]] = []
        self._drag_offset: QPoint | None = None
        self._suppress = False

        self.setWindowTitle(f"设备 · {device.name}")
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowSystemMenuHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 520)
        self._build()
        self.reload(device)

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
        layout.addWidget(self._build_header())

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

        layout.addWidget(self._build_command_panel())
        layout.addWidget(self._build_footer())

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
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        outer.addWidget(self._advanced_toggle)

        self._advanced_body = QWidget()
        body = QVBoxLayout(self._advanced_body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(self._mini_label("服务"))
        self._svc_combo = QComboBox()
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

        self._hint = QLabel("命令下发后，设备响应会显示在主窗口「运行日志」中。")
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
        self._name.setText(device.name or device.dev_id)

        online = device.online
        state = "online" if online is True else "offline" if online is False else "unknown"
        self._dot.set_state(state)
        online_text = {"online": "在线", "offline": "离线", "unknown": "状态待同步"}[state]
        self._meta.setText(f"{online_text} · {device.room_name or '未分配房间'}")
        bits = []
        if device.model:
            bits.append(f"型号 {device.model}")
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
        for service in specs:
            properties = list(service.properties)
            # 头部已有三态电源开关，服务块里不再重复同一个开关属性
            if main_switch is not None:
                properties = [
                    item
                    for item in properties
                    if not (item.sid == main_switch[0] and item.key == main_switch[1])
                ]
            if not properties:
                continue
            self._body.addWidget(self._build_service_block(service, properties))
        self._body.addStretch(1)
        self._refresh_service_combo(specs)

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
            edit = QLineEdit(str(prop.value))
            edit.setFixedWidth(180)
            edit.returnPressed.connect(
                lambda s=prop.sid, k=prop.key, e=edit: self._send(s, {k: e.text().strip()})
            )
            widget = edit
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
                format_number(prop.value)
                if prop.kind == KIND_READONLY_NUMBER
                else str(prop.value)
            )
            value = QLabel(text)
            value.setObjectName("Caption" if prop.kind == KIND_READONLY_TEXT else "Value")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setWordWrap(True)
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

        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setSingleStep(1)
        slider.setValue(int(round(float(prop.value) * scale)))

        value_label = QLabel(self._format_slider_value(prop, slider.value() / scale))
        value_label.setObjectName("Value")
        value_label.setFixedWidth(52)
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
    def _format_slider_value(prop: PropertySpec, value: float) -> str:
        if prop.is_int:
            return str(int(round(value)))
        return f"{value:.2f}".rstrip("0").rstrip(".")

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

    def _on_advanced_toggled(self, expanded: bool) -> None:
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
