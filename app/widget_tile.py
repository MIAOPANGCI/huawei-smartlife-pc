"""桌面磁贴：每台设备一块独立小窗，各自摆在桌面任意位置。

和桌面挂件（``widget_board.py``）的分工：

- 挂件是一条横向设备条，整体拖动、可以贴边隐藏，适合"一排开关"
- 磁贴是每台一块，各自记坐标，适合按房间的实际位置摆（客厅在左、门口在右）

两者并存，各自在托盘菜单里开关，互不影响。

**壁纸层**（``wallpaper_mode``）：把窗口 ``SetParent`` 到桌面那一层，它就变成桌面
的一部分——其它窗口正常盖住它，Win+D「显示桌面」时它还在。代价是子窗口不支持
分层（半透明），所以壁纸模式下窗口必须不透明，四角用 region 遮罩切掉，光晕也只能
画在卡片内部（见 ``app/wallpaper.py`` 的说明）。
"""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBitmap,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QRegion,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import Device

from . import device_spec, wallpaper
from .theme import mix, theme
from .widgets.controls import PowerButton
from .widgets.icons import DeviceGlyph
from .widgets.labels import ElidedLabel

# 尺寸档位：(宽, 高, 图标尺寸, 电源键直径, 是否显示状态文字)
# 这里的宽高是**窗口**尺寸，卡片本身比它四周各小 GLOW_PAD（留给光晕外溢）。
TILE_SIZES: dict[str, tuple[int, int, int, int, bool]] = {
    "compact": (164, 112, 24, 26, False),
    "standard": (212, 136, 32, 34, True),
    "large": (248, 156, 36, 38, True),
    "card": (364, 232, 40, 48, True),
}
DEFAULT_SIZE = "standard"
CARD_SIZE = "card"

# 光晕在卡片四周留出的余量：窗口尺寸 = 卡片 + 2*GLOW_PAD。
# 壁纸模式下这块会被 region 遮罩切掉，所以两种模式的窗口尺寸一致，位置不会跳。
GLOW_PAD = 12
CARD_RADIUS = 14

# 沉浸光感强度
GLOW_LEVELS: dict[str, float] = {
    "off": 0.0,
    "weak": 0.40,
    "balanced": 0.72,
    "strong": 1.0,
}
DEFAULT_GLOW = "balanced"
GLOW_MODES = tuple(GLOW_LEVELS)

# 卡片底色不透明度（0~1）。壁纸层下这个值直接决定"透出多少壁纸"
DEFAULT_OPACITY = 0.55
MIN_OPACITY = 0.20

# 标准档的尺寸，保留这两个名字给自检和默认位置计算用
TILE_WIDTH = TILE_SIZES[DEFAULT_SIZE][0]
TILE_HEIGHT = TILE_SIZES[DEFAULT_SIZE][1]

TILE_GAP = 12
# 自动排布时离屏幕边缘留的余量（避开任务栏和屏幕边）
EDGE_MARGIN = 40
# 距离屏幕边缘多近算"要贴上去"
SNAP_DISTANCE = 24

ALIGN_MODES = ("free", "grid", "edge")

# 磁贴最多几块：再多屏幕就摆不下了，也让「自选设备」有个明确上限
MAX_TILES = 12

# 壁纸层掉了（Explorer 重启）之后多久重挂一次
WATCH_INTERVAL_MS = 5000


def _screen_area() -> QRect:
    """主屏可用区（磁贴按主屏摆放；副屏上的位置会在显示时被夹回来）。"""
    screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)


def size_spec(size_key: str) -> tuple[int, int, int, int, bool]:
    return TILE_SIZES.get(size_key, TILE_SIZES[DEFAULT_SIZE])


def _text_color(color: str, alpha: int) -> QColor:
    out = QColor(color)
    out.setAlpha(alpha)
    return out


class _Stepper(QWidget):
    """温度加减条：``−  温度 27 °C  +``。

    点一下发一次绝对值（不是增量），避免快速连点把设备刷成意外的值。
    """

    changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TileStepper")
        self.setFixedHeight(38)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)

        self._minus = self._button("−")
        self._minus.clicked.connect(lambda: self._nudge(-1))
        row.addWidget(self._minus)

        self._label = QLabel("温度")
        self._label.setObjectName("StepperLabel")
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self._label, 0, Qt.AlignVCenter)
        row.addStretch(1)

        self._value = QLabel("—")
        self._value.setObjectName("StepperValue")
        self._value.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self._value, 0, Qt.AlignVCenter)
        row.addStretch(1)

        self._plus = self._button("+")
        self._plus.clicked.connect(lambda: self._nudge(1))
        row.addWidget(self._plus)

        self._current = 0.0
        self._minimum = 16.0
        self._maximum = 30.0
        self._step = 1.0

    def _button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("StepperButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(30, 30)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def set_range(self, value: float, minimum: float, maximum: float, step: float) -> None:
        self._current = float(value)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._step = max(0.5, float(step))
        self._refresh()

    def _nudge(self, direction: int) -> None:
        target = self._current + direction * self._step
        target = max(self._minimum, min(self._maximum, target))
        if abs(target - self._current) < 1e-6:
            return
        self._current = target
        self._refresh()
        self.changed.emit(target)

    def _refresh(self) -> None:
        shown = f"{self._current:g}"
        self._value.setText(f"{shown} °C")
        self._minus.setEnabled(self._current > self._minimum + 1e-6)
        self._plus.setEnabled(self._current < self._maximum - 1e-6)


class _ChipRow(QWidget):
    """底部模式切换：几个模式块 + 一个「更多」。

    「更多」不在卡片上猜协议，直接打开设备详情——模式编号因厂商而异，猜错就是
    把设备设成别的模式。
    """

    picked = Signal(str)
    more_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TileChips")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._buttons: list[QPushButton] = []

    def _clear(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._buttons.clear()

    def set_modes(self, current: str, options: list[tuple[str, str]]) -> None:
        self._clear()
        ordered = [(key, label) for key, label in options]
        # 当前模式排第一个，后面跟最多两个，凑成「当前 / 另一个 / 更多」
        ordered.sort(key=lambda item: 0 if item[0] == current else 1)
        for key, label in ordered[:3]:
            button = QPushButton(label)
            button.setObjectName("TileChip")
            button.setCursor(Qt.PointingHandCursor)
            button.setCheckable(True)
            button.setChecked(key == current)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _checked, k=key: self.picked.emit(k))
            self._row.addWidget(button, 1)
            self._buttons.append(button)
        self._add_more()

    def set_empty(self) -> None:
        self._clear()
        self._add_more()

    def _add_more(self) -> None:
        more = QPushButton("更多")
        more.setObjectName("TileChip")
        more.setCursor(Qt.PointingHandCursor)
        more.setFocusPolicy(Qt.NoFocus)
        more.clicked.connect(self.more_requested.emit)
        self._row.addWidget(more, 1)
        self._buttons.append(more)


class DeviceTile(QWidget):
    """一块磁贴：图标 + 名称 + 状态（或控件） + 电源键。

    整块可拖动；只有控件（电源键 / 加减 / 模式块）响应点击——和挂件一致，
    避免拖一下就误开关设备。
    """

    power_requested = Signal(str)
    command_requested = Signal(str, str, dict)   # dev_id, sid, data
    detail_requested = Signal(str)
    moved = Signal(str, int, int)
    dismissed = Signal(str)          # 空串表示"隐藏全部"
    settings_requested = Signal()    # 右键「选择设备…」

    def __init__(
        self,
        device: Device,
        pos: QPoint | None = None,
        parent: QWidget | None = None,
        *,
        size_key: str = DEFAULT_SIZE,
        show_summary: bool = True,
        align: str = "free",
        glow: str = DEFAULT_GLOW,
        wallpaper_mode: bool = False,
        opacity: float = DEFAULT_OPACITY,
    ) -> None:
        # parent 传 None：挂到主窗口下的话，主窗口最小化会把磁贴一起藏起来。
        #
        # 不要 WindowStaysOnTopHint：磁贴是"桌面上的小方块"，不该永远压在别的
        # 窗口上面。用 Qt.Tool 是为了不进任务栏、不进 Alt+Tab。
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint,
        )
        self._wallpaper_wanted = bool(wallpaper_mode) and wallpaper.supported()
        self._wallpaper_active = False
        self._layered = False           # 壁纸层里靠 UpdateLayeredWindow 推图
        self._rendering = False          # 正在离屏渲染（防止事件过滤器递归）
        self._push_dirty = False
        self._offset = QPoint(0, 0)
        # 壁纸层是往桌面窗口里 SetParent，子窗口不支持分层窗口——
        # 设了 WA_TranslucentBackground 的话挂上去会**完全不渲染**。
        if not self._wallpaper_wanted:
            self.setAttribute(Qt.WA_TranslucentBackground, True)

        width, height, icon, power, size_shows_summary = size_spec(size_key)
        self._size_key = size_key
        # 第五项是"这一档是否显示状态文字"：紧凑档塞不下，直接省掉
        self._show_summary = bool(show_summary) and size_shows_summary
        self._align = align if align in ALIGN_MODES else "free"
        self._glow = glow if glow in GLOW_LEVELS else DEFAULT_GLOW
        self._opacity = min(1.0, max(MIN_OPACITY, float(opacity)))
        self._dev_id = device.dev_id
        self._drag_offset: QPoint | None = None
        self.setCursor(Qt.PointingHandCursor)
        self._icon_size = icon
        self._power_diameter = power
        self.setFixedSize(width, height)

        self._temp_control: tuple[str, str, float, float, float] | None = None
        self._mode_control: tuple[str, str, str, list[tuple[str, str]]] | None = None
        self._build()
        self.update_device(device)
        if pos is not None:
            self.move(pos)

        self._watch = QTimer(self)
        self._watch.setInterval(WATCH_INTERVAL_MS)
        self._watch.timeout.connect(self._watch_wallpaper)
        if self._wallpaper_wanted:
            self._watch.start()

        # 分层窗口的内容全靠自己推，子控件重绘也要跟着推一次
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(16)
        self._push_timer.timeout.connect(self._flush_push)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    # ---- 构建 ----
    @property
    def is_card(self) -> bool:
        return self._size_key == CARD_SIZE

    @property
    def glow(self) -> str:
        return self._glow

    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(GLOW_PAD, GLOW_PAD, GLOW_PAD, GLOW_PAD)

        self._body = QFrame()
        self._body.setObjectName("TileBody")
        self._body.setAttribute(Qt.WA_TranslucentBackground, True)
        # 注意：这里**不能**设 WA_TransparentForMouseEvents。那个属性会连子控件
        # 一起禁用鼠标事件，电源键就点不动了（踩过）。不设也没事：QFrame 默认
        # 忽略鼠标按下事件，会自然冒泡到 DeviceTile 上开始拖动。
        shell.addWidget(self._body)

        box = QVBoxLayout(self._body)
        margin = 16 if self.is_card else (12 if self._size_key != "compact" else 10)
        box.setContentsMargins(margin, margin - 2, margin, margin - 2)
        box.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self._glyph = DeviceGlyph("generic", self._icon_size, True)
        self._glyph.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(self._glyph, 0, Qt.AlignTop if not self.is_card else Qt.AlignVCenter)

        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.setContentsMargins(0, 0, 0, 0)
        self._name = ElidedLabel("设备")
        self._name.setObjectName("TileName")
        self._name.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        texts.addWidget(self._name)

        self._summary = ElidedLabel("")
        self._summary.setObjectName("TileSubtitle" if self.is_card else "TileSummary")
        self._summary.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._summary.setVisible(self._show_summary)
        texts.addWidget(self._summary)
        top.addLayout(texts, 1)

        self._power = PowerButton("unknown", self._power_diameter)
        self._power.toggled_requested.connect(
            lambda _state: self.power_requested.emit(self._dev_id)
        )
        top.addWidget(self._power, 0, Qt.AlignTop if not self.is_card else Qt.AlignVCenter)
        box.addLayout(top)

        if not self.is_card:
            box.insertStretch(0, 1)

        self._stepper: _Stepper | None = None
        self._chips: _ChipRow | None = None
        if self.is_card:
            box.addStretch(1)
            self._stepper = _Stepper(self)
            self._stepper.changed.connect(self._on_temperature)
            self._stepper.hide()
            box.addWidget(self._stepper)

            self._chips = _ChipRow(self)
            self._chips.picked.connect(self._on_mode)
            self._chips.more_requested.connect(
                lambda: self.detail_requested.emit(self._dev_id)
            )
            box.addWidget(self._chips)
            box.addStretch(1)
        else:
            box.addStretch(1)

    # ---- 内容 ----
    def update_device(self, device: Device) -> None:
        self._dev_id = device.dev_id
        name = device.name or device.dev_id
        self._name.setText(name)
        self._name.setToolTip(name)
        self.setWindowTitle(f"桌面磁贴 · {name}")
        self._glyph.set_device(device_spec.device_kind(device), device.online is not False)

        offline = device.online is False
        self._summary.setText(self._summary_text(device))
        self._summary.setToolTip(self._summary.text())
        self._summary.setVisible(self._show_summary)

        found = device_spec.find_switch_service(device)
        if found is not None:
            self._power.set_state(device_spec.switch_state_of(found[2]))
            self._power.show()
        else:
            self._power.hide()

        # 离线标记：卡片底色会据此换成更暗的一层
        marker = "true" if offline else "false"
        if self._body.property("offline") != marker:
            self._body.setProperty("offline", marker)

        self._temp_control = None if offline else device_spec.find_temperature_control(device)
        self._mode_control = None if offline else device_spec.find_mode_control(device)
        self._sync_controls()
        self.update()

    def _summary_text(self, device: Device) -> str:
        if device.online is False:
            return "离线"
        if self.is_card:
            parts: list[str] = []
            mode = device_spec.find_mode_control(device)
            if mode is not None:
                label = dict(device_spec.MODE_LABELS).get(mode[2])
                if label:
                    parts.append(label)
            current = self._reported_temperature(device)
            if current is not None:
                parts.append(f"{current:g}°C")
            if device.room_name:
                parts.append(device.room_name)
            if parts:
                return " · ".join(parts)
        summary = device_spec.device_summary(device)
        return summary or ("在线" if device.online else "待同步")

    @staticmethod
    def _reported_temperature(device: Device) -> float | None:
        """设备回传的当前温度（不是设定值）。"""
        for svc in device.services.values():
            data = svc.data or {}
            for key, value in data.items():
                name = str(key).lower()
                if name in ("currenttemperature", "ambienttemp", "indoortemperature"):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        return float(value)
        return None

    def _sync_controls(self) -> None:
        """按设备能力显示/隐藏卡片上的控件。"""
        if self._stepper is None or self._chips is None:
            return
        temp = self._temp_control
        if temp is None:
            self._stepper.hide()
        else:
            _sid, _key, value, minimum, maximum, step = temp
            self._stepper.set_range(value, minimum, maximum, step)
            self._stepper.show()

        mode = self._mode_control
        if mode is None:
            self._chips.set_empty()
        else:
            _sid, _key, current, options = mode
            self._chips.set_modes(current, options)

    def _on_temperature(self, value: float) -> None:
        if self._temp_control is None:
            return
        sid, key, *_rest = self._temp_control
        self.command_requested.emit(self._dev_id, sid, {key: value})

    def _on_mode(self, mode_key: str) -> None:
        if self._mode_control is None:
            return
        sid, key, current, _options = self._mode_control
        if mode_key == current:
            return
        self.command_requested.emit(self._dev_id, sid, {key: mode_key})

    @property
    def dev_id(self) -> str:
        return self._dev_id

    # ---- 绘制 ----
    def _card_rect(self) -> QRect:
        return self.rect().adjusted(GLOW_PAD, GLOW_PAD, -GLOW_PAD, -GLOW_PAD)

    @property
    def glow_strength(self) -> float:
        return GLOW_LEVELS.get(self._glow, GLOW_LEVELS[DEFAULT_GLOW])

    def _card_color(self) -> QColor:
        pal = theme().palette
        base = QColor(pal.surface_2 if self._body.property("offline") == "true" else pal.surface)
        base.setAlphaF(self._opacity)
        return base

    def _border_color(self) -> QColor:
        pal = theme().palette
        if self.underMouse():
            return QColor(pal.accent)
        soft = QColor(pal.text)
        soft.setAlpha(30)
        return soft

    def _glow_blobs(self, card: QRectF) -> list[tuple[QPointF, float, QColor, float]]:
        """三条柔和光带，模拟"沉浸光感"里那层虹彩。"""
        pal = theme().palette
        warm = QColor(mix(pal.accent, "#ff8a5c", 0.35))
        cool = QColor(mix(pal.accent, "#4fd1ff", 0.62))
        violet = QColor(mix(pal.accent, "#b28cff", 0.66))
        return [
            (
                QPointF(card.left() + card.width() * 0.16, card.top() + card.height() * 0.04),
                card.width() * 0.74,
                warm,
                0.52,
            ),
            (
                QPointF(card.left() + card.width() * 0.50, card.top() - card.height() * 0.06),
                card.width() * 0.70,
                cool,
                0.46,
            ),
            (
                QPointF(card.left() + card.width() * 0.88, card.top() + card.height() * 0.14),
                card.width() * 0.72,
                violet,
                0.44,
            ),
        ]

    def _paint_halo(self, painter: QPainter, card: QRectF) -> None:
        """卡片外面那一圈柔光。

        做法是"一圈圈放大的圆角矩形"，但每一圈的浓度**不是各自独立给的**——
        后画的圈会盖住先画的圈，独立给值等于把透明度一层层累加，实测会叠成
        一圈刺眼的荧光描边。这里按目标曲线反推每一圈该补多少：

            a_i = (t_i - t_prev) / (1 - t_prev)

        这样叠完的最终浓度正好等于目标曲线：最外圈 0，贴到卡片边缘才到峰值。
        """
        if self.glow_strength <= 0:
            return
        pal = theme().palette
        dark = pal.mode == "dark"
        # 和卡片底色揉一下，但别揉太灰，否则在深色壁纸上基本看不见
        warm = QColor(mix(mix(pal.accent, pal.surface, 0.25), "#ff9a7a", 0.30))
        cool = QColor(mix(mix(pal.accent, pal.surface, 0.35), "#63c8ff", 0.35))

        peak = (0.42 if dark else 0.48) * self.glow_strength
        steps = GLOW_PAD
        achieved = 0.0
        for step in range(steps, 0, -1):
            ratio = step / steps                     # 1 = 最外圈
            target = peak * (1.0 - ratio) ** 2.0
            if target <= achieved + 1e-4:
                continue
            alpha = (target - achieved) / max(1e-3, 1.0 - achieved)
            achieved = target

            path = QPainterPath()
            path.addRoundedRect(
                card.adjusted(-step, -step, step, step),
                CARD_RADIUS + step,
                CARD_RADIUS + step,
            )
            gradient = QLinearGradient(card.topLeft(), card.bottomLeft())
            for stop, color in ((0.0, warm), (1.0, cool)):
                tint = QColor(color)
                tint.setAlphaF(min(1.0, alpha))
                gradient.setColorAt(stop, tint)
            painter.fillPath(path, gradient)

    def _paint_inner_glow(self, painter: QPainter, card: QRectF) -> None:
        """卡片里面那层"背后有光"的柔光。"""
        if self.glow_strength <= 0:
            return
        # 深色底上光更"抢眼"，同样是 0.4 的浓度会糊掉整张卡，所以按主题打折
        weight = 0.30 if theme().palette.mode == "light" else 0.18
        for center, radius, color, alpha in self._glow_blobs(card):
            peak = max(0.0, min(1.0, alpha * self.glow_strength * weight))
            gradient = QRadialGradient(center, radius)
            start = QColor(color)
            start.setAlphaF(peak)
            mid = QColor(color)
            mid.setAlphaF(peak * 0.55)
            fade = QColor(color)
            fade.setAlphaF(peak * 0.18)
            outer = QColor(color)
            outer.setAlphaF(0.0)
            gradient.setColorAt(0.0, start)
            gradient.setColorAt(0.55, mid)
            gradient.setColorAt(0.88, fade)
            gradient.setColorAt(1.0, outer)
            painter.setBrush(gradient)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(center, radius, radius)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        card = QRectF(self._card_rect())
        path = QPainterPath()
        path.addRoundedRect(card, CARD_RADIUS, CARD_RADIUS)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        self._paint_halo(painter, card)

        painter.fillPath(path, self._card_color())

        # 卡内的一层光：让玻璃看起来"背后有光"
        if self.glow_strength > 0:
            painter.save()
            painter.setClipPath(path)
            self._paint_inner_glow(painter, card)
            painter.restore()

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._border_color(), 1))
        painter.drawPath(path)
        painter.end()

        # 分层窗口要自己把内容推上去
        if self._layered and not self._rendering:
            self.schedule_push()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.update()
        super().leaveEvent(event)

    # ---- 壁纸层 ----
    @property
    def wallpaper_active(self) -> bool:
        return self._wallpaper_active

    @property
    def layered(self) -> bool:
        return self._layered

    def _apply_mask(self) -> None:
        """退路：推图失败时用圆角 region 硬切四角（边缘会有锯齿）。

        只在 ``UpdateLayeredWindow`` 不可用时才走这里，正常情况下走分层窗口，
        圆角是抗锯齿的。
        """
        if not self._wallpaper_active or self._layered:
            self.clearMask()
            return
        bitmap = QBitmap(self.size())
        bitmap.fill(Qt.color0)
        painter = QPainter(bitmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setBrush(Qt.color1)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self._card_rect(), CARD_RADIUS, CARD_RADIUS)
        painter.end()
        self.setMask(QRegion(bitmap))

    def _ensure_wallpaper(self) -> None:
        if not self._wallpaper_wanted or self._wallpaper_active:
            return
        screen_pos = self._screen_pos()
        hwnd = int(self.winId())
        offset = wallpaper.attach(hwnd)
        if offset is None:
            # 挂不上就老实当普通窗口：宁可少一个特性，也别变成一个看不见的窗口
            self._wallpaper_wanted = False
            self._watch.stop()
            return
        self._offset = QPoint(int(offset[0]), int(offset[1]))
        self._wallpaper_active = True
        self.move(self._to_parent(screen_pos))

        # 挂成子窗口之后，Qt 自己的绘制是被忽略的（分层窗口只认 UpdateLayeredWindow），
        # 所以要么整份内容自己推，要么什么都看不到
        self._layered = wallpaper.make_layered(hwnd)
        if self._layered:
            self.clearMask()
            self.schedule_push()
        else:
            self._apply_mask()
            self.update()

    def _watch_wallpaper(self) -> None:
        """Explorer 重启后父窗口会消失，定期检查并重挂。"""
        if not self._wallpaper_wanted or not self.isVisible():
            return
        if wallpaper.is_attached(int(self.winId())):
            if self._layered:
                return
            self._ensure_wallpaper()
            return
        self._wallpaper_active = False
        self._layered = False
        self.clearMask()
        self._ensure_wallpaper()

    def schedule_push(self) -> None:
        """安排一次内容推送（16ms 合并多次重绘）。"""
        if not self._layered or self._rendering:
            return
        self._push_dirty = True
        if not self._push_timer.isActive():
            self._push_timer.start()

    def _flush_push(self) -> None:
        if not self._layered or not self._wallpaper_active or not self._push_dirty:
            return
        self._push_dirty = False

        image = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        self._rendering = True
        try:
            # 连子控件一起渲染：分层窗口里按钮、文字都只能靠这张图
            self.render(
                image, QPoint(0, 0), QRegion(), QWidget.RenderFlags(QWidget.DrawChildren)
            )
        finally:
            self._rendering = False

        hwnd = int(self.winId())
        if wallpaper.push_layered(hwnd, image, self.pos().x(), self.pos().y()):
            return
        # 推不上去就别硬撑：退回不透明 + 圆角遮罩，至少还能看见
        self._layered = False
        self._apply_mask()
        self.update()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        if event.type() == QEvent.Paint and self._layered and not self._rendering:
            self.schedule_push()
        return super().eventFilter(obj, event)

    # ---- 显示 / 位置 ----
    def _screen_pos(self) -> QPoint:
        """当前实际所在的屏幕坐标（挂到桌面层后 Qt 坐标是相对的）。"""
        return self.pos() + self._offset

    def _to_parent(self, screen_pos: QPoint) -> QPoint:
        return screen_pos - self._offset

    def show_at(self, pos: QPoint | None = None) -> None:
        """显示，并把位置夹回屏幕内（换了分辨率 / 拔了副屏也不会跑丢）。"""
        area = _screen_area()
        target = pos if pos is not None else self._screen_pos()
        x = max(area.left(), min(target.x(), area.right() - self.width()))
        y = max(area.top(), min(target.y(), area.bottom() - self.height()))
        self.move(self._to_parent(QPoint(x, y)))
        self.show()
        self._ensure_wallpaper()

    def apply_align(self, align: str) -> None:
        """改对齐方式（不用重建窗口）。"""
        self._align = align if align in ALIGN_MODES else "free"

    def set_summary_visible(self, visible: bool) -> None:
        self._show_summary = bool(visible)
        self._summary.setVisible(self._show_summary and size_spec(self._size_key)[4])

    def set_opacity(self, value: float) -> None:
        """卡片不透明度（0.45~1）。壁纸层下这个值决定透出多少壁纸。"""
        value = min(1.0, max(MIN_OPACITY, float(value)))
        if abs(value - self._opacity) < 1e-3:
            return
        self._opacity = value
        self.update()

    def set_glow(self, level: str) -> None:
        if level not in GLOW_LEVELS or level == self._glow:
            return
        self._glow = level
        self.update()

    def grid_step(self) -> tuple[int, int]:
        """网格对齐的步长：卡片尺寸 + 间距（不含光晕余量）。"""
        card = self._card_rect()
        return (card.width() + TILE_GAP, card.height() + TILE_GAP)

    def _snapped(self, pos: QPoint) -> QPoint:
        """按当前对齐方式算最终落点（坐标为屏幕坐标）。"""
        if self._align == "grid":
            # 按"卡片尺寸 + 间距"步进，而不是死磕 16 像素。
            # 16px 只保证坐标是整数倍，可卡片宽高都不是 16 的倍数，两块挨在一起
            # 时反而会贴死（中间没有缝），看起来像粘成了一片。
            area = _screen_area()
            step_x, step_y = self.grid_step()
            col = max(0, round((pos.x() - GLOW_PAD - area.left()) / step_x))
            row = max(0, round((pos.y() - GLOW_PAD - area.top()) / step_y))
            return QPoint(
                area.left() + GLOW_PAD + col * step_x,
                area.top() + GLOW_PAD + row * step_y,
            )
        if self._align != "edge":
            return pos

        area = _screen_area()
        x, y = pos.x(), pos.y()
        # 四条边都试一遍：靠近就贴上去，"甩到边上"比手动对齐像素轻松得多
        if abs(x - area.left()) < SNAP_DISTANCE:
            x = area.left()
        if abs((x + self.width()) - (area.right() + 1)) < SNAP_DISTANCE:
            x = area.right() + 1 - self.width()
        if abs(y - area.top()) < SNAP_DISTANCE:
            y = area.top()
        if abs((y + self.height()) - (area.bottom() + 1)) < SNAP_DISTANCE:
            y = area.bottom() + 1 - self.height()
        return QPoint(x, y)

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._screen_pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_offset
            self.move(self._to_parent(target))
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

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton:
            self.detail_requested.emit(self._dev_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _remember_pos(self) -> None:
        snapped = self._snapped(self._screen_pos())
        if snapped != self._screen_pos():
            self.move(self._to_parent(snapped))
        self.moved.emit(self._dev_id, snapped.x(), snapped.y())

    def flush_pos(self) -> None:
        """退出前把当前位置补报一次（拖动中途退出的情况）。"""
        if self._drag_offset is not None:
            self._drag_offset = None
        self._remember_pos()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        menu = QMenu(self)
        menu.addAction("设备详情…", lambda: self.detail_requested.emit(self._dev_id))
        menu.addAction("选择设备…", self.settings_requested.emit)
        menu.addSeparator()
        menu.addAction("隐藏这块磁贴", lambda: self.dismissed.emit(self._dev_id))
        menu.addAction("隐藏全部磁贴", lambda: self.dismissed.emit(""))
        menu.exec(event.globalPos())


class TileBoard:
    """管理所有磁贴：谁显示、各自在哪、长什么样。

    不是 QWidget——磁贴是各自独立的顶层窗口，这里只负责持有和协调。
    """

    def __init__(
        self,
        positions: dict | None = None,
        *,
        size_key: str = DEFAULT_SIZE,
        align: str = "free",
        show_summary: bool = True,
        glow: str = DEFAULT_GLOW,
        wallpaper_mode: bool = False,
        opacity: float = DEFAULT_OPACITY,
    ) -> None:
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
        self._size_key = size_key if size_key in TILE_SIZES else DEFAULT_SIZE
        self._align = align if align in ALIGN_MODES else "free"
        self._show_summary = bool(show_summary)
        self._glow = glow if glow in GLOW_LEVELS else DEFAULT_GLOW
        self._wallpaper_mode = bool(wallpaper_mode)
        self._opacity = min(1.0, max(MIN_OPACITY, float(opacity)))
        self.on_power: Callable[[str], None] | None = None
        self.on_command: Callable[[str, str, dict], None] | None = None
        self.on_detail: Callable[[str], None] | None = None
        self.on_state_changed: Callable[[dict], None] | None = None
        self.on_dismissed: Callable[[str], None] | None = None
        self.on_settings: Callable[[], None] | None = None

    # ---- 外观选项 ----
    @property
    def size_key(self) -> str:
        return self._size_key

    @property
    def align(self) -> str:
        return self._align

    @property
    def show_summary(self) -> bool:
        return self._show_summary

    @property
    def glow(self) -> str:
        return self._glow

    @property
    def wallpaper_mode(self) -> bool:
        return self._wallpaper_mode

    @property
    def opacity(self) -> float:
        return self._opacity

    def set_options(
        self,
        *,
        size_key: str | None = None,
        align: str | None = None,
        show_summary: bool | None = None,
        glow: str | None = None,
        wallpaper_mode: bool | None = None,
        opacity: float | None = None,
    ) -> None:
        """改外观。

        尺寸和壁纸层变了必须重建窗口（固定尺寸控件没法只 resize；分层/不透明
        属性也只能在窗口创建时决定），位置会保留；对齐、摘要、光感可以就地更新。
        """
        rebuild = False
        if size_key is not None and size_key in TILE_SIZES and size_key != self._size_key:
            self._size_key = size_key
            rebuild = True
        if wallpaper_mode is not None and bool(wallpaper_mode) != self._wallpaper_mode:
            self._wallpaper_mode = bool(wallpaper_mode)
            rebuild = True
        if align is not None and align in ALIGN_MODES:
            self._align = align
        if show_summary is not None:
            self._show_summary = bool(show_summary)
        if glow is not None and glow in GLOW_LEVELS:
            self._glow = glow
        if opacity is not None:
            self._opacity = min(1.0, max(MIN_OPACITY, float(opacity)))

        if rebuild:
            self._rebuild()
            return
        for tile in self._tiles.values():
            tile.apply_align(self._align)
            tile.set_summary_visible(self._show_summary)
            tile.set_glow(self._glow)
            tile.set_opacity(self._opacity)

    def _rebuild(self) -> None:
        """按新尺寸 / 新窗口属性重建所有磁贴，位置原样保留。"""
        for dev_id, tile in list(self._tiles.items()):
            self._positions[dev_id] = tile._screen_pos()
            tile.hide()
            tile.close()
            tile.deleteLater()
        self._tiles.clear()
        self.set_devices(self._devices)

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
            tile = DeviceTile(
                device,
                pos,
                size_key=self._size_key,
                show_summary=self._show_summary,
                align=self._align,
                glow=self._glow,
                wallpaper_mode=self._wallpaper_mode,
                opacity=self._opacity,
            )
            tile.power_requested.connect(self._forward_power)
            tile.command_requested.connect(self._forward_command)
            tile.detail_requested.connect(self._forward_detail)
            tile.moved.connect(self._on_moved)
            tile.dismissed.connect(self._forward_dismissed)
            tile.settings_requested.connect(self._forward_settings)
            self._tiles[device.dev_id] = tile
            if self._visible:
                tile.show_at(pos)

    def _auto_pos(self, index: int) -> QPoint:
        """新磁贴从屏幕右上角往下排，排满一列往左挪一列。"""
        area = _screen_area()
        width, height = size_spec(self._size_key)[:2]
        per_column = max(1, (area.height() - EDGE_MARGIN * 2) // (height + TILE_GAP))
        column = index // per_column
        row = index % per_column
        x = area.right() - EDGE_MARGIN - width - column * (width + TILE_GAP)
        y = area.top() + EDGE_MARGIN + row * (height + TILE_GAP)
        return QPoint(x, y)

    # ---- 回调 ----
    def _forward_power(self, dev_id: str) -> None:
        if self.on_power is not None:
            self.on_power(dev_id)

    def _forward_command(self, dev_id: str, sid: str, data: dict) -> None:
        if self.on_command is not None:
            self.on_command(dev_id, sid, data)

    def _forward_detail(self, dev_id: str) -> None:
        if self.on_detail is not None:
            self.on_detail(dev_id)

    def _forward_dismissed(self, dev_id: str) -> None:
        if self.on_dismissed is not None:
            self.on_dismissed(dev_id)

    def _forward_settings(self) -> None:
        if self.on_settings is not None:
            self.on_settings()

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
            tile.show_at(self._positions.get(dev_id, tile._screen_pos()))

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
