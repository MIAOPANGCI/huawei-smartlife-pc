"""自绘开关与三态电源按钮。

为什么自绘：QSS 无法给 QCheckBox 做出「轨道 + 滑块」的过渡动画，而开关
是三态电源按钮之外最高频的交互控件，值得多花一点代码。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from ..theme import theme
from . import motion


def _mix(start: QColor, end: QColor, ratio: float) -> QColor:
    """按 ratio 在两色之间插值（含 alpha）。ratio=0 取 start，1 取 end。"""
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        int(start.red() + (end.red() - start.red()) * ratio),
        int(start.green() + (end.green() - start.green()) * ratio),
        int(start.blue() + (end.blue() - start.blue()) * ratio),
        int(start.alpha() + (end.alpha() - start.alpha()) * ratio),
    )


class SwitchButton(QAbstractButton):
    """滑动开关。勾选状态变化时发出 ``toggled``（继承自 QAbstractButton）。"""

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 24)

        self._position = 1.0 if checked else 0.0
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    # -- 动画属性 --
    def get_position(self) -> float:
        return self._position

    def set_position(self, value: float) -> None:
        self._position = max(0.0, min(1.0, float(value)))
        self.update()

    position = Property(float, get_position, set_position)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 接口
        return QSize(44, 24)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = theme().palette

        track = QRectF(1, 2, self.width() - 2, self.height() - 4)
        radius = track.height() / 2

        off_color = QColor(palette.surface_3)
        on_color = QColor(palette.accent)
        # 按动画进度在开/关底色之间插值
        ratio = self._position
        color = QColor(
            int(off_color.red() + (on_color.red() - off_color.red()) * ratio),
            int(off_color.green() + (on_color.green() - off_color.green()) * ratio),
            int(off_color.blue() + (on_color.blue() - off_color.blue()) * ratio),
        )

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, radius, radius)

        knob_diameter = track.height() - 4
        knob_x = track.left() + 2 + (track.width() - knob_diameter - 4) * ratio
        knob = QRectF(knob_x, track.top() + 2, knob_diameter, knob_diameter)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(knob)

        if not self.isEnabled():
            painter.setBrush(QColor(0, 0, 0, 40))
            painter.drawRoundedRect(track, radius, radius)
        painter.end()


class PowerButton(QAbstractButton):
    """三态电源按钮：开（强调色实底）/ 关（灰底）/ 未知（描边问号）。

    点它不会自己改状态——真实状态由设备回传决定，避免「点了就以为开了」。
    UI 层收到 ``clicked`` 后下发命令，并在收到响应/遥测时更新状态。
    """

    toggled_requested = Signal(str)  # 当前状态 "on"/"off"/"unknown"

    def __init__(
        self, state: str = "unknown", diameter: int = 34, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._prev_state = state
        # 0 = 还停在旧状态的样子，1 = 完全切换到新状态。
        # 设备回传状态是异步的，按钮直接跳色会显得"闪一下"，
        # 所以在两态之间插值，读起来像状态真的过渡过去了。
        self._blend = 1.0
        self._diameter = diameter
        # 外圈留 3px 给「开启」时的淡光晕，避免绘制被控件边界裁掉
        self._pad = 3
        self.setFixedSize(diameter + self._pad * 2, diameter + self._pad * 2)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(lambda: self.toggled_requested.emit(self._state))

        self._transition = QPropertyAnimation(self, b"blend", self)
        self._transition.setDuration(180)
        self._transition.setEasingCurve(QEasingCurve.OutCubic)

    # -- 动画属性 --
    def get_blend(self) -> float:
        return self._blend

    def set_blend(self, value: float) -> None:
        self._blend = max(0.0, min(1.0, float(value)))
        self.update()

    blend = Property(float, get_blend, set_blend)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._prev_state = self._state
        self._state = state
        self.setToolTip(
            {"on": "已开启，点击关闭", "off": "已关闭，点击开启"}.get(
                state, "状态未知，点击尝试开启"
            )
        )
        if not motion.enabled():
            self._blend = 1.0
            self.update()
            return
        self._transition.stop()
        self._transition.setStartValue(0.0)
        self._transition.setEndValue(1.0)
        self._blend = 0.0
        self._transition.start()

    @property
    def state(self) -> str:
        return self._state

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = theme().palette
        pad = self._pad
        rect = QRectF(pad + 0.5, pad + 0.5, self._diameter - 1, self._diameter - 1)

        ratio = self._blend

        def fill_of(state: str) -> QColor:
            if state == "on":
                return QColor(palette.accent)
            if state == "off":
                return QColor(palette.surface_3)
            return QColor(palette.surface_2)

        def symbol_of(state: str) -> QColor:
            if state == "on":
                return QColor(palette.on_accent)
            if state == "off":
                return QColor(palette.text_2)
            return QColor(palette.text_3)

        fill = _mix(fill_of(self._prev_state), fill_of(self._state), ratio)
        symbol_color = _mix(symbol_of(self._prev_state), symbol_of(self._state), ratio)

        # 光晕只在「开」这一侧存在，跟着过渡一起淡入/淡出，
        # 而不是开关瞬间"啪"地出现或消失。
        if self._prev_state == self._state:
            glow_ratio = 1.0 if self._state == "on" else 0.0
        elif self._state == "on":
            glow_ratio = ratio
        else:
            glow_ratio = 1.0 - ratio

        if glow_ratio > 0.01:
            glow = QColor(palette.accent)
            glow.setAlpha(int(38 * glow_ratio))
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-2.6, -2.6, 2.6, 2.6))

        # 未知态才有描边，过渡时让它同步淡出/淡入
        outline = (1.0 - ratio) if self._state != "unknown" else ratio
        if self._prev_state == self._state:
            outline = 1.0 if self._state == "unknown" else 0.0
        if outline > 0.01:
            border = QColor(palette.text_3)
            border.setAlpha(int(255 * outline))
            painter.setPen(QPen(border, 1.2))
        else:
            painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(rect)

        if not self.isEnabled():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 30))
            painter.drawEllipse(rect)
            symbol_color.setAlpha(110)

        # 电源符号：顶部竖线 + 顶部留缺口的圆环
        # 注意 drawArc 的角度：0°=3 点钟，正值=逆时针，90°=12 点钟。
        # 从 120° 逆时针扫 300° 到 60°，缺口恰好落在顶部正中，容竖线穿过。
        center = rect.center()
        diameter = rect.width()
        radius = diameter * 0.20
        pen = QPen(symbol_color, max(1.6, diameter * 0.055))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        painter.drawLine(
            QPointF(center.x(), center.y() - radius * 1.18),
            QPointF(center.x(), center.y() - radius * 0.06),
        )
        arc_rect = QRectF(
            center.x() - radius,
            center.y() - radius,
            radius * 2.0,
            radius * 2.0,
        )
        painter.drawArc(arc_rect, int(120 * 16), int(300 * 16))
        painter.end()
