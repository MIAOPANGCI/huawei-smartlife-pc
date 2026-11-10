"""桌面磁贴：每台设备一块独立小窗，各自摆在桌面任意位置。

和桌面挂件（``widget_board.py``）的分工：

- 挂件是一条横向设备条，整体拖动、可以贴边隐藏，适合"一排开关"
- 磁贴是每台一块，各自记坐标，适合按房间的实际位置摆（客厅在左、门口在右）

两者并存，各自在托盘菜单里开关，互不影响。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from . import device_spec
from .widgets.controls import PowerButton
from .widgets.icons import DeviceGlyph
from .widgets.labels import ElidedLabel

TILE_WIDTH = 150
TILE_HEIGHT = 110
TILE_GAP = 12
# 自动排布时离屏幕边缘留的余量（避开任务栏和屏幕边）
EDGE_MARGIN = 40


def _screen_area() -> QRect:
    """主屏可用区（磁贴按主屏摆放；副屏上的位置会在显示时被夹回来）。"""
    screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)


class DeviceTile(QWidget):
    """一块磁贴：图标 + 名称 + 状态摘要 + 电源键。

    整块可拖动；只有右下角的电源键响应点击——和挂件一致，避免拖一下就误开关设备。
    """

    power_requested = Signal(str)
    moved = Signal(str, int, int)
    dismissed = Signal(str)          # 空串表示"隐藏全部"

    def __init__(
        self,
        device: Device,
        pos: QPoint | None = None,
        parent: QWidget | None = None,
    ) -> None:
        # parent 传 None：挂到主窗口下的话，主窗口最小化会把磁贴一起藏起来
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)
        self._dev_id = device.dev_id
        self._drag_offset: QPoint | None = None
        self.setCursor(Qt.PointingHandCursor)
        self._build()
        self.update_device(device)
        if pos is not None:
            self.move(pos)

    # ---- 构建 ----
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)

        self._body = QFrame()
        self._body.setObjectName("DesktopTile")
        # 不接收鼠标，事件直接落到 DeviceTile 上（拖动才不会中途丢掉）；
        # 电源键是它的子控件，仍然自己收点击
        self._body.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        shell.addWidget(self._body)

        box = QVBoxLayout(self._body)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self._glyph = DeviceGlyph("generic", 30, True)
        self._glyph.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(self._glyph, 0, Qt.AlignTop)
        top.addStretch(1)
        self._power = PowerButton("unknown", 22)
        self._power.toggled_requested.connect(
            lambda _state: self.power_requested.emit(self._dev_id)
        )
        top.addWidget(self._power, 0, Qt.AlignTop)
        box.addLayout(top)

        self._name = ElidedLabel("设备")
        self._name.setObjectName("TileName")
        self._name.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        box.addWidget(self._name)

        self._summary = QLabel("")
        self._summary.setObjectName("TileSummary")
        self._summary.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        box.addWidget(self._summary)
        box.addStretch(1)

    # ---- 内容 ----
    def update_device(self, device: Device) -> None:
        self._dev_id = device.dev_id
        name = device.name or device.dev_id
        self._name.setText(name)
        self._name.setToolTip(name)
        self.setWindowTitle(f"桌面磁贴 · {name}")
        self._glyph.set_device(device_spec.device_kind(device), device.online is not False)

        if device.online is False:
            summary = "离线"
        else:
            summary = device_spec.device_summary(device)
            if not summary:
                summary = "在线" if device.online else "待同步"
        self._summary.setText(summary)

        found = device_spec.find_switch_service(device)
        if found is not None:
            self._power.set_state(device_spec.switch_state_of(found[2]))
            self._power.show()
        else:
            self._power.hide()

        offline = "true" if device.online is False else "false"
        if self._body.property("offline") != offline:
            self._body.setProperty("offline", offline)
            self._body.style().unpolish(self._body)
            self._body.style().polish(self._body)

    @property
    def dev_id(self) -> str:
        return self._dev_id

    # ---- 显示 / 位置 ----
    def show_at(self, pos: QPoint | None = None) -> None:
        """显示，并把位置夹回屏幕内（换了分辨率 / 拔了副屏也不会跑丢）。"""
        area = _screen_area()
        target = pos if pos is not None else self.pos()
        x = max(area.left(), min(target.x(), area.right() - self.width()))
        y = max(area.top(), min(target.y(), area.bottom() - self.height()))
        self.move(x, y)
        self.show()
        self.raise_()

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if self._drag_offset is not None:
            self._drag_offset = None
            self._remember_pos()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _remember_pos(self) -> None:
        pos = self.pos()
        self.moved.emit(self._dev_id, pos.x(), pos.y())

    def flush_pos(self) -> None:
        """退出前把当前位置补报一次（拖动中途退出的情况）。"""
        if self._drag_offset is not None:
            self._drag_offset = None
        self._remember_pos()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        menu = QMenu(self)
        menu.addAction("隐藏这块磁贴", lambda: self.dismissed.emit(self._dev_id))
        menu.addAction("隐藏全部磁贴", lambda: self.dismissed.emit(""))
        menu.exec(event.globalPos())


class TileBoard:
    """管理所有磁贴：谁显示、各自在哪、状态怎么同步。

    不是 QWidget——磁贴是各自独立的顶层窗口，这里只负责持有和协调。
    """

    def __init__(self, positions: dict | None = None) -> None:
        self._positions: dict[str, QPoint] = {}
        for dev_id, value in (positions or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                self._positions[str(dev_id)] = QPoint(int(value["x"]), int(value["y"]))
            except (KeyError, TypeError, ValueError):
                continue
        self._devices: list[Device] = []
        self._tiles: dict[str, DeviceTile] = {}
        self._visible = False
        self.on_power: Callable[[str], None] | None = None
        self.on_state_changed: Callable[[dict], None] | None = None
        self.on_dismissed: Callable[[str], None] | None = None

    # ---- 同步 ----
    def set_devices(self, devices: list[Device]) -> None:
        """按新的设备列表增删磁贴；已有的只更新内容，不动位置也不重建窗口。"""
        self._devices = list(devices)
        wanted = {device.dev_id for device in devices}

        for dev_id in list(self._tiles):
            if dev_id not in wanted:
                tile = self._tiles.pop(dev_id)
                tile.hide()
                tile.deleteLater()

        for index, device in enumerate(devices):
            tile = self._tiles.get(device.dev_id)
            if tile is not None:
                tile.update_device(device)
                continue
            pos = self._positions.get(device.dev_id) or self._auto_pos(index)
            tile = DeviceTile(device, pos)
            tile.power_requested.connect(self._forward_power)
            tile.moved.connect(self._on_moved)
            tile.dismissed.connect(self._forward_dismissed)
            self._tiles[device.dev_id] = tile
            if self._visible:
                tile.show_at(pos)

    def _auto_pos(self, index: int) -> QPoint:
        """新磁贴从屏幕右上角往下排，排满一列往左挪一列。"""
        area = _screen_area()
        per_column = max(1, (area.height() - EDGE_MARGIN * 2) // (TILE_HEIGHT + TILE_GAP))
        column = index // per_column
        row = index % per_column
        x = area.right() - EDGE_MARGIN - TILE_WIDTH - column * (TILE_WIDTH + TILE_GAP)
        y = area.top() + EDGE_MARGIN + row * (TILE_HEIGHT + TILE_GAP)
        return QPoint(x, y)

    # ---- 回调 ----
    def _forward_power(self, dev_id: str) -> None:
        if self.on_power is not None:
            self.on_power(dev_id)

    def _forward_dismissed(self, dev_id: str) -> None:
        if self.on_dismissed is not None:
            self.on_dismissed(dev_id)

    def _on_moved(self, dev_id: str, x: int, y: int) -> None:
        self._positions[dev_id] = QPoint(x, y)
        if self.on_state_changed is not None:
            self.on_state_changed(self.snapshot())

    # ---- 显示控制 ----
    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def count(self) -> int:
        return len(self._tiles)

    def show_all(self) -> None:
        self._visible = True
        for dev_id, tile in self._tiles.items():
            tile.show_at(self._positions.get(dev_id, tile.pos()))

    def hide_all(self) -> None:
        self._visible = False
        for tile in self._tiles.values():
            tile.hide()

    def hide_one(self, dev_id: str) -> None:
        tile = self._tiles.get(dev_id)
        if tile is not None:
            tile.hide()
        if not any(tile.isVisible() for tile in self._tiles.values()):
            self._visible = False

    def close_all(self) -> None:
        """退出程序时调用：flush 位置后全部关掉，别留孤儿窗口。"""
        tiles = list(self._tiles.values())
        self._tiles.clear()
        self._visible = False
        for tile in tiles:
            tile.flush_pos()
            tile.hide()
            tile.close()
            # 交给 Qt 回收：磁贴是顶层窗口，只靠 Python 侧 GC 容易出现
            # "Python 对象先销毁、C++ 窗口还在"的顺序问题
            tile.deleteLater()
        self._visible = False

    def refresh(self, device: Device) -> None:
        """某台设备状态变了，只更新对应那一块。"""
        tile = self._tiles.get(device.dev_id)
        if tile is not None:
            tile.update_device(device)

    # ---- 持久化 ----
    def snapshot(self) -> dict:
        return {
            dev_id: {"x": pos.x(), "y": pos.y()}
            for dev_id, pos in self._positions.items()
        }
