"""自绘界面图标（刷新 / 日志 / 设置 / 主题 / 窗口按钮）。

为什么不用图标字体：``Segoe MDL2 Assets``、``Segoe Fluent Icons`` 在部分
Windows 环境（精简系统、某些远程会话）里并不存在，``QFontDatabase`` 查不到，
用码点渲染会变成残缺笔画或方块。这里统一用 QPainter 画到 QPixmap 生成 QIcon，
不依赖任何字体或图片资源，颜色随主题重生成即可。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_SCALE = 2  # 2 倍尺寸生成，保证高 DPI 下清晰


def _pen(color: QColor, width: float = 1.5) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw_refresh(p: QPainter) -> None:
    rect = QRectF(3.0, 3.0, 12.0, 12.0)
    p.drawArc(rect, 60 * 16, 260 * 16)
    # 箭头
    p.drawLine(QPointF(13.0, 3.0), QPointF(13.0, 6.4))
    p.drawLine(QPointF(13.0, 3.0), QPointF(9.6, 3.0))


def _draw_logs(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4.0, 2.5, 10.0, 13.0), 2.0, 2.0)
    p.drawLine(QPointF(6.5, 6.0), QPointF(11.5, 6.0))
    p.drawLine(QPointF(6.5, 9.0), QPointF(11.5, 9.0))
    p.drawLine(QPointF(6.5, 12.0), QPointF(9.5, 12.0))


def _draw_camera(p: QPainter) -> None:
    # 机身 + 右侧镜头楔形 + 镜头圆，16×16 内不与其他图标混淆
    p.drawRoundedRect(QRectF(2.0, 5.6, 9.2, 6.8), 1.6, 1.6)
    p.drawLine(QPointF(11.2, 8.2), QPointF(14.0, 6.9))
    p.drawLine(QPointF(14.0, 6.9), QPointF(14.0, 11.1))
    p.drawLine(QPointF(14.0, 11.1), QPointF(11.2, 9.8))
    p.drawEllipse(QRectF(4.7, 7.5, 3.8, 3.8))


def _draw_settings(p: QPainter) -> None:
    # 滑杆式设置图标（比齿轮在小尺寸下更清晰）
    for y, knob_x in ((5.0, 11.5), (9.0, 6.5), (13.0, 10.0)):
        p.drawLine(QPointF(3.0, y), QPointF(15.0, y))
        p.drawEllipse(QRectF(knob_x - 1.6, y - 1.6, 3.2, 3.2))


def _draw_sun(p: QPainter) -> None:
    p.drawEllipse(QRectF(6.2, 6.2, 5.6, 5.6))
    for index in range(8):
        angle = math.radians(index * 45)
        inner = 6.3
        outer = 8.1
        p.drawLine(
            QPointF(9 + inner * math.cos(angle), 9 + inner * math.sin(angle)),
            QPointF(9 + outer * math.cos(angle), 9 + outer * math.sin(angle)),
        )


def _draw_moon(p: QPainter) -> None:
    path = QPainterPath()
    path.addEllipse(QRectF(4.0, 3.0, 10.0, 12.0))
    cut = QPainterPath()
    cut.addEllipse(QRectF(7.0, 1.6, 9.0, 12.0))
    p.drawPath(path.subtracted(cut))


def _draw_minimize(p: QPainter) -> None:
    p.drawLine(QPointF(4.0, 9.0), QPointF(14.0, 9.0))


def _draw_maximize(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4.5, 4.5, 9.0, 9.0), 1.5, 1.5)


def _draw_restore(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 3.5, 8.0, 8.0), 1.5, 1.5)
    p.drawRoundedRect(QRectF(6.5, 6.5, 8.0, 8.0), 1.5, 1.5)


def _draw_close(p: QPainter) -> None:
    p.drawLine(QPointF(4.6, 4.6), QPointF(13.4, 13.4))
    p.drawLine(QPointF(13.4, 4.6), QPointF(4.6, 13.4))


def _draw_home(p: QPainter) -> None:
    p.drawPolyline(
        [QPointF(2.8, 9.2), QPointF(9.0, 3.4), QPointF(15.2, 9.2)]
    )
    p.drawLine(QPointF(4.6, 8.4), QPointF(4.6, 14.6))
    p.drawLine(QPointF(13.4, 8.4), QPointF(13.4, 14.6))
    p.drawLine(QPointF(4.6, 14.6), QPointF(13.4, 14.6))


def _draw_grid(p: QPainter) -> None:
    for x, y in ((3.2, 3.2), (9.6, 3.2), (3.2, 9.6), (9.6, 9.6)):
        p.drawRoundedRect(QRectF(x, y, 5.2, 5.2), 1.4, 1.4)


def _draw_list(p: QPainter) -> None:
    for y in (5.0, 9.0, 13.0):
        p.drawLine(QPointF(3.4, y), QPointF(14.6, y))


def _draw_lock(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4.2, 8.2, 9.6, 7.0), 1.8, 1.8)
    p.drawArc(QRectF(6.0, 3.4, 6.0, 7.0), 0, 180 * 16)


def _draw_unlock(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4.2, 8.2, 9.6, 7.0), 1.8, 1.8)
    p.drawArc(QRectF(7.4, 3.4, 6.0, 7.0), 0, 150 * 16)


def _draw_chevron_down(p: QPainter) -> None:
    p.drawPolyline(
        [QPointF(4.4, 6.8), QPointF(9.0, 11.4), QPointF(13.6, 6.8)]
    )


def _draw_chevron_up(p: QPainter) -> None:
    p.drawPolyline(
        [QPointF(4.4, 11.2), QPointF(9.0, 6.6), QPointF(13.6, 11.2)]
    )


def _draw_sliders(p: QPainter) -> None:
    for y, knob_x in ((5.2, 11.4), (9.0, 6.6), (12.8, 12.0)):
        p.drawLine(QPointF(2.6, y), QPointF(15.4, y))
        p.setBrush(QColor(p.pen().color()))
        p.drawEllipse(QPointF(knob_x, y), 1.7, 1.7)
        p.setBrush(Qt.NoBrush)


def _draw_plus(p: QPainter) -> None:
    p.drawLine(QPointF(9.0, 3.6), QPointF(9.0, 14.4))
    p.drawLine(QPointF(3.6, 9.0), QPointF(14.4, 9.0))


def _draw_minus(p: QPainter) -> None:
    p.drawLine(QPointF(3.6, 9.0), QPointF(14.4, 9.0))


def _draw_power(p: QPainter) -> None:
    p.drawArc(QRectF(3.2, 3.6, 11.6, 11.6), 120 * 16, -240 * 16)
    p.drawLine(QPointF(9.0, 2.4), QPointF(9.0, 8.0))


_DRAWERS = {
    "refresh": _draw_refresh,
    "logs": _draw_logs,
    "camera": _draw_camera,
    "settings": _draw_settings,
    "sun": _draw_sun,
    "moon": _draw_moon,
    "minimize": _draw_minimize,
    "maximize": _draw_maximize,
    "restore": _draw_restore,
    "close": _draw_close,
    "power": _draw_power,
    "home": _draw_home,
    "grid": _draw_grid,
    "list": _draw_list,
    "lock": _draw_lock,
    "unlock": _draw_unlock,
    "chevron_down": _draw_chevron_down,
    "chevron_up": _draw_chevron_up,
    "sliders": _draw_sliders,
    "plus": _draw_plus,
    "minus": _draw_minus,
}


def make_icon(name: str, color: str, size: int = 18, width: float = 1.5) -> QIcon:
    """生成图标。``color`` 传当前主题的文字色，切换主题时重新生成。"""
    canvas = QPixmap(size * _SCALE, size * _SCALE)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(size * _SCALE / 18.0, size * _SCALE / 18.0)
    painter.setPen(_pen(QColor(color), width))
    painter.setBrush(Qt.NoBrush)
    drawer = _DRAWERS.get(name)
    if drawer is not None:
        drawer(painter)
    painter.end()
    # 画布是 size×2，交给 QIcon 缩放显示，高 DPI 下自然清晰
    return QIcon(canvas)


def make_app_icon(accent: str = "#d92b3a", size: int = 64) -> QIcon:
    """应用 / 托盘图标：圆角方块 + 强调色 + "H"。"""
    canvas = QPixmap(size * _SCALE, size * _SCALE)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(_SCALE, _SCALE)

    path = QPainterPath()
    path.addRoundedRect(2, 2, size - 4, size - 4, size * 0.22, size * 0.22)
    painter.fillPath(path, QColor(accent))

    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPointSizeF(size * 0.44)
    font.setWeight(font.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "H")
    painter.end()
    return QIcon(canvas)
