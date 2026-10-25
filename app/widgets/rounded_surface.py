"""自绘圆角面板。

为什么不用 QSS 的 ``border-radius``：Qt 画圆角时**不做抗锯齿**，边缘是
阶梯状像素，在半透明窗口上与背景混合后就是一圈灰色毛边（托盘面板 / 桌面
挂件那圈"角上不对劲"的来源）。这里用 QPainter + Antialiasing 自己画，
边缘平滑，也不依赖 Windows 11 的 DWM 圆角是否生效。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QWidget

from ..theme import theme


class RoundedSurface(QFrame):
    """圆角矩形面板：填充 + 1px 描边，四角抗锯齿。"""

    def __init__(
        self,
        radius: int = 14,
        background: str | None = None,
        border: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radius = radius
        self._background = background      # None = 跟随主题
        self._border = border
        # 自己画背景，别让 QSS 再叠一层
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_radius(self, radius: int) -> None:
        self._radius = radius
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        palette = theme().palette
        background = QColor(self._background or palette.bg_elev)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # 内缩半像素：1px 描边正好压在边界上，不会糊成两像素
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)

        painter.fillPath(path, background)

        # 深色面板在浅色壁纸上靠明度差就够，加描边反而显出一条亮线；
        # 浅色主题下白面板白背景才需要描边定边界。
        border = self._border
        if border is None and palette.mode == "light":
            border = palette.border_soft
        if border:
            painter.setPen(QPen(QColor(border), 1))
            painter.drawPath(path)
        painter.end()
