"""小型自绘指示器：在线状态点、设备头像、类型角标。

用 QPainter 直接画而不是拼图标资源，好处是随主题色实时变化、无需打包
图片资源，且在高 DPI 屏上始终清晰。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..theme import theme

# 设备类型 → 头像配色（在深浅主题下都保持可辨识）
_KIND_COLORS: dict[str, str] = {
    "light": "#f2a93b",
    "socket": "#2e7cf6",
    "ac": "#22b8cf",
    "heater": "#f2683c",
    "camera": "#8b5cf6",
    "sensor": "#12b76a",
    "router": "#0ea5e9",
    "wearable": "#64748b",
    "washer": "#0891b2",
    "curtain": "#a855f7",
    "purifier": "#14b8a6",
    "generic": "#7c8b9a",
}


def kind_color(kind: str) -> QColor:
    color = QColor(_KIND_COLORS.get(kind, _KIND_COLORS["generic"]))
    # 深色主题下适当提亮，保证暗底上的可辨识度
    if theme().mode == "dark":
        color = color.lighter(118)
    return color


class StatusDot(QWidget):
    """在线状态圆点：在线=绿、离线=灰、未知=空心。"""

    def __init__(self, state: str = "unknown", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._size = 8
        self.setFixedSize(12, 12)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.update()

    @property
    def state(self) -> str:
        return self._state

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = theme().palette
        center = QRectF(
            (self.width() - self._size) / 2,
            (self.height() - self._size) / 2,
            self._size,
            self._size,
        )
        if self._state == "online":
            color = QColor(palette.ok)
            glow = QColor(palette.ok)
            glow.setAlpha(60)
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center.adjusted(-2, -2, 2, 2))
            painter.setBrush(color)
            painter.drawEllipse(center)
        elif self._state == "offline":
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(palette.text_3))
            painter.drawEllipse(center)
        else:
            pen = QPen(QColor(palette.text_3), 1.2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center.adjusted(1, 1, -1, -1))
        painter.end()


class DeviceAvatar(QWidget):
    """圆角方块头像：类型色淡底 + 设备名首字。"""

    def __init__(
        self,
        text: str = "?",
        kind: str = "generic",
        size: int = 44,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._kind = kind
        self._size = size
        self.setFixedSize(size, size)

    def set_device(self, text: str, kind: str) -> None:
        self._text = text
        self._kind = kind
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        color = kind_color(self._kind)
        rect = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        radius = self._size * 0.30

        background = QColor(color)
        background.setAlpha(48)
        painter.setPen(Qt.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, radius, radius)

        pen = QPen(QColor(color.red(), color.green(), color.blue(), 110), 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        font = QFont(self.font())
        font.setPointSizeF(max(9.0, self._size * 0.30))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignCenter, self._text)
        painter.end()


class DeviceGlyph(QWidget):
    """设备线条图标（参考 MiHome-Windows 卡片里的轮廓图标）。

    在 22×22 的逻辑坐标系里用 QPainter 画轮廓，按控件尺寸缩放。
    线条图标比「首字头像」更安静，卡片信息层级不会被彩色块抢走。
    """

    def __init__(
        self,
        kind: str = "generic",
        size: int = 26,
        online: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._size = size
        self._online = online
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_device(self, kind: str, online: bool) -> None:
        if kind == self._kind and online == self._online:
            return
        self._kind = kind
        self._online = online
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = theme().palette
        color = QColor(palette.text if self._online else palette.text_3)
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        scale = (self._size - 2) / 22.0
        painter.translate(1, 1)
        painter.scale(scale, scale)
        drawer = _GLYPH_DRAWERS.get(self._kind, _draw_generic)
        try:
            drawer(painter)
        except Exception:  # noqa: BLE001 - 单个图标绘制失败不影响界面
            _draw_generic(painter)
        painter.end()


def _draw_light(p: QPainter) -> None:
    p.drawEllipse(QRectF(5.5, 3.0, 11.0, 11.0))
    p.drawLine(8.0, 16.0, 14.0, 16.0)
    p.drawLine(9.0, 18.8, 13.0, 18.8)


def _draw_socket(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 3.5, 15.0, 15.0), 3.0, 3.0)
    p.drawLine(8.5, 8.0, 8.5, 10.5)
    p.drawLine(13.5, 8.0, 13.5, 10.5)
    p.drawLine(10.0, 14.5, 12.0, 14.5)


def _draw_ac(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(2.0, 6.0, 18.0, 7.5), 2.5, 2.5)
    p.drawLine(5.5, 17.0, 16.5, 17.0)
    p.drawLine(7.5, 19.3, 14.5, 19.3)


def _draw_camera(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.0, 6.5, 16.0, 10.0), 2.5, 2.5)
    p.drawEllipse(QRectF(7.6, 9.0, 5.8, 5.8))
    p.drawLine(16.0, 3.5, 19.0, 3.5)


def _draw_router(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(2.5, 12.5, 17.0, 5.5), 1.8, 1.8)
    p.drawLine(6.5, 12.5, 4.5, 4.5)
    p.drawLine(15.5, 12.5, 17.5, 4.5)


def _draw_wearable(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(6.0, 5.0, 10.0, 12.0), 3.0, 3.0)
    p.drawLine(9.0, 5.0, 9.0, 2.0)
    p.drawLine(13.0, 5.0, 13.0, 2.0)
    p.drawLine(9.0, 17.0, 9.0, 20.0)
    p.drawLine(13.0, 17.0, 13.0, 20.0)


def _draw_washer(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 2.5, 15.0, 17.0), 2.5, 2.5)
    p.drawEllipse(QRectF(7.5, 8.0, 7.0, 7.0))
    p.drawLine(6.5, 5.5, 8.0, 5.5)


def _draw_curtain(p: QPainter) -> None:
    p.drawLine(2.5, 4.5, 19.5, 4.5)
    p.drawLine(6.5, 4.5, 6.5, 19.5)
    p.drawLine(15.5, 4.5, 15.5, 19.5)
    p.drawLine(11.0, 4.5, 11.0, 19.5)


def _draw_purifier(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(5.0, 2.5, 12.0, 17.0), 4.0, 4.0)
    p.drawLine(5.0, 12.0, 17.0, 12.0)
    p.drawEllipse(QRectF(9.5, 5.0, 3.0, 3.0))


def _draw_heater(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4.5, 3.0, 13.0, 16.0), 4.0, 4.0)
    p.drawLine(8.0, 9.0, 13.0, 9.0)
    p.drawLine(8.0, 12.0, 13.0, 12.0)


def _draw_sensor(p: QPainter) -> None:
    p.drawEllipse(QRectF(4.0, 4.0, 14.0, 14.0))
    p.drawEllipse(QRectF(9.4, 9.4, 3.2, 3.2))


def _draw_generic(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 3.5, 15.0, 15.0), 3.0, 3.0)
    p.drawLine(8.0, 8.0, 14.0, 8.0)


_GLYPH_DRAWERS = {
    "light": _draw_light,
    "socket": _draw_socket,
    "ac": _draw_ac,
    "camera": _draw_camera,
    "router": _draw_router,
    "wearable": _draw_wearable,
    "washer": _draw_washer,
    "curtain": _draw_curtain,
    "purifier": _draw_purifier,
    "heater": _draw_heater,
    "sensor": _draw_sensor,
    "generic": _draw_generic,
}
