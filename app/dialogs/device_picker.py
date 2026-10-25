"""设备自选与排序对话框：托盘面板 / 桌面挂件共用。

左侧是「已显示」列表（顺序即展示顺序，可拖动也可用按钮上下移动），
右侧是「可选设备」（点一下就加进左侧）。

设计取舍：
- 顺序用**列表本身**表达，而不是序号输入框——用户看到的就是最终顺序。
- 已选为空时视为「用默认规则」，保存空列表就恢复自动排序。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from ..theme import theme
from ..widgets.glyph_icons import make_icon

DEV_ID_ROLE = Qt.UserRole + 1


class DevicePickerDialog(QDialog):
    """选择「哪些设备显示」以及「按什么顺序显示」。"""

    def __init__(
        self,
        title: str,
        devices: list[Device],
        selected_ids: list[str],
        max_count: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(680, 470)
        self.resize(720, 500)

        self._devices = {device.dev_id: device for device in devices}
        self._max_count = max_count
        self._title_text = title

        self._build()
        self._fill(selected_ids)


    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        head = QLabel(self._title_text)
        head.setObjectName("DialogTitle")
        outer.addWidget(head)

        limit_text = f"，最多 {self._max_count} 个" if self._max_count else ""
        hint = QLabel(
            f"左列是显示内容与顺序（可拖动排序）{limit_text}；右列是尚未加入的设备，点击即加入。"
        )
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_selected_column(), 1)
        body.addWidget(self._build_actions(), 0)
        body.addWidget(self._build_available_column(), 1)
        outer.addLayout(body, 1)

        outer.addLayout(self._build_footer())

    def _build_selected_column(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label = QLabel("已显示（按顺序）")
        label.setObjectName("SectionTitle")
        layout.addWidget(label)

        self._selected = QListWidget()
        self._selected.setObjectName("PickerList")
        self._selected.setSelectionMode(QAbstractItemView.SingleSelection)
        self._selected.setDragDropMode(QAbstractItemView.InternalMove)
        self._selected.setDefaultDropAction(Qt.MoveAction)
        self._selected.setIconSize(QSize(18, 18))
        self._selected.setAlternatingRowColors(False)
        self._selected.itemDoubleClicked.connect(lambda _item: self._remove_current())
        layout.addWidget(self._selected, 1)

        self._selected_hint = QLabel("")
        self._selected_hint.setObjectName("Caption")
        layout.addWidget(self._selected_hint)
        return box

    def _build_available_column(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label = QLabel("可选设备")
        label.setObjectName("SectionTitle")
        layout.addWidget(label)

        self._available = QListWidget()
        self._available.setObjectName("PickerList")
        self._available.setSelectionMode(QAbstractItemView.SingleSelection)
        self._available.setIconSize(QSize(18, 18))
        self._available.itemDoubleClicked.connect(lambda _item: self._add_current())
        layout.addWidget(self._available, 1)

        self._available_hint = QLabel("")
        self._available_hint.setObjectName("Caption")
        layout.addWidget(self._available_hint)
        return box

    def _build_actions(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)

        self._add_btn = self._action_button("plus", "加入", self._add_current)
        layout.addWidget(self._add_btn)

        self._remove_btn = self._action_button(
            "minus", "移出", self._remove_current
        )
        layout.addWidget(self._remove_btn)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        self._up_btn = self._action_button("chevron_up", "上移", lambda: self._move(-1))
        layout.addWidget(self._up_btn)

        self._down_btn = self._action_button(
            "chevron_down", "下移", lambda: self._move(1)
        )
        layout.addWidget(self._down_btn)
        layout.addStretch(1)
        return box

    def _action_button(self, icon: str, text: str, slot) -> QPushButton:
        """图标 + 文字：只放图标在这个尺寸下几乎看不出含义。"""
        button = QPushButton(f" {text}")
        button.setObjectName("PickerAction")
        button.setFixedSize(74, 30)
        button.setIconSize(QSize(13, 13))
        button.setIcon(make_icon(icon, theme().palette.text_2, 13, width=1.8))
        button.setToolTip(text)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        reset = QPushButton("恢复默认")
        reset.setObjectName("Ghost")
        reset.setCursor(Qt.PointingHandCursor)
        reset.setToolTip("清空自选，改回自动排序（在线优先）")
        reset.clicked.connect(self._reset_default)
        row.addWidget(reset)

        row.addStretch(1)

        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.setCursor(Qt.PointingHandCursor)
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(save)
        return row

    # ==================================================================
    # 数据填充
    # ==================================================================
    def _fill(self, selected_ids: list[str]) -> None:
        self._selected.clear()
        seen: set[str] = set()
        for dev_id in selected_ids:
            device = self._devices.get(dev_id)
            if device is None or dev_id in seen:
                continue
            seen.add(dev_id)
            self._selected.addItem(self._make_item(device))

        for device in self._devices.values():
            if device.dev_id in seen:
                continue
            self._available.addItem(self._make_item(device))

        self._refresh_hints()

    def _make_item(self, device: Device) -> QListWidgetItem:
        room = device.room_name or "未分配"
        state = {True: "在线", False: "离线", None: "待同步"}[device.online]
        name = device.name or device.dev_id
        # 列表宽度有限：只显示「名称 · 房间」，完整信息放 tooltip，否则文字被裁
        item = QListWidgetItem(f"{name}  ·  {room}")
        item.setData(DEV_ID_ROLE, device.dev_id)
        item.setToolTip(f"{name}\n{room} · {state}\n型号：{device.model or '未知'}")
        if device.online is False:
            item.setForeground(QColor(theme().palette.text_3))
        return item

    def _refresh_hints(self) -> None:
        chosen = self._selected.count()
        if self._max_count:
            self._selected_hint.setText(f"已选 {chosen} / {self._max_count}")
        else:
            self._selected_hint.setText(f"已选 {chosen} 个")
        self._available_hint.setText(f"可选 {self._available.count()} 个")
        full = self._max_count is not None and chosen >= self._max_count
        self._add_btn.setEnabled(not full and self._available.count() > 0)

    # ==================================================================
    # 交互
    # ==================================================================
    def _add_current(self) -> None:
        item = self._available.currentItem()
        if item is None:
            return
        if self._max_count is not None and self._selected.count() >= self._max_count:
            return
        self._available.takeItem(self._available.row(item))
        self._selected.addItem(item)
        self._selected.setCurrentItem(item)
        self._refresh_hints()

    def _remove_current(self) -> None:
        item = self._selected.currentItem()
        if item is None:
            return
        self._selected.takeItem(self._selected.row(item))
        self._available.addItem(item)
        self._available.setCurrentItem(item)
        self._refresh_hints()

    def _move(self, delta: int) -> None:
        row = self._selected.currentRow()
        if row < 0:
            return
        target = row + delta
        if not 0 <= target < self._selected.count():
            return
        item = self._selected.takeItem(row)
        self._selected.insertItem(target, item)
        self._selected.setCurrentRow(target)

    def _reset_default(self) -> None:
        self._selected.clear()
        self._available.clear()
        for device in self._devices.values():
            self._available.addItem(self._make_item(device))
        self._refresh_hints()

    # ==================================================================
    # 结果
    # ==================================================================
    def selected_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self._selected.count()):
            item = self._selected.item(row)
            dev_id = item.data(DEV_ID_ROLE)
            if isinstance(dev_id, str):
                ids.append(dev_id)
        return ids
