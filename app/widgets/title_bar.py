"""无边框窗口的自绘标题栏（参考 MiHome-Windows 的标题栏交互）。

功能：拖动移动、双击最大化/还原、最小化、最大化、关闭。
图标全部用 QPainter 自绘生成（见 glyph_icons），不依赖系统图标字体。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..theme import theme
from .glyph_icons import make_icon


class _Logo(QWidget):
    """品牌标记：圆角方块 + 强调色。"""

    def __init__(self, size: int = 26, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = theme().palette
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(
            float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height()), 8, 8
        )
        painter.fillPath(path, QColor(palette.accent))

        painter.setPen(QColor(palette.on_accent))
        font = QFont(self.font())
        font.setPointSizeF(self._size * 0.44)
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, "H")
        painter.end()


class TitleBar(QWidget):
    """窗口标题栏。宿主窗口负责实际的最小化/最大化/关闭。"""

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()
    theme_toggle_requested = Signal()
    settings_requested = Signal()

    def __init__(
        self, parent: QWidget, title: str = "华为智慧生活", subtitle: str = "PC 控制台"
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(46)
        self._drag_offset: QPoint | None = None
        self._icon_targets: list[tuple[QPushButton, str]] = []
        self._build(title, subtitle)
        self.refresh_icons()

    def _build(self, title: str, subtitle: str) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(10)

        layout.addWidget(_Logo(26), 0, Qt.AlignVCenter)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(0)
        self._title = QLabel(title)
        self._title.setObjectName("AppTitle")
        self._subtitle = QLabel(subtitle)
        self._subtitle.setObjectName("AppSubTitle")
        text_box.addWidget(self._title)
        text_box.addWidget(self._subtitle)
        layout.addLayout(text_box)
        layout.addStretch(1)

        self._theme_btn = self._icon_button("moon", "切换深色 / 浅色主题")
        self._theme_btn.clicked.connect(self.theme_toggle_requested.emit)
        layout.addWidget(self._theme_btn, 0, Qt.AlignVCenter)

        layout.addSpacing(6)
        self._min_btn = self._icon_button("minimize", "最小化")
        self._min_btn.clicked.connect(self.minimize_requested.emit)
        layout.addWidget(self._min_btn, 0, Qt.AlignVCenter)

        self._max_btn = self._icon_button("maximize", "最大化")
        self._max_btn.clicked.connect(self.maximize_requested.emit)
        layout.addWidget(self._max_btn, 0, Qt.AlignVCenter)

        self._close_btn = self._icon_button("close", "关闭", close=True)
        self._close_btn.clicked.connect(self.close_requested.emit)
        layout.addWidget(self._close_btn, 0, Qt.AlignVCenter)

    def _icon_button(self, name: str, tooltip: str, close: bool = False) -> QPushButton:
        button = QPushButton()
        button.setObjectName("TitleButtonClose" if close else "TitleButton")
        button.setToolTip(tooltip)
        button.setFixedSize(34, 30)
        button.setIconSize(QSize(16, 16))
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        self._icon_targets.append((button, name))
        return button

    # ---------- 图标 ----------
    def refresh_icons(self) -> None:
        color = theme().palette.text_2
        for button, name in self._icon_targets:
            button.setIcon(make_icon(name, color, 16, width=1.4))

    def _set_icon(self, button: QPushButton, name: str) -> None:
        for index, (target, _old) in enumerate(self._icon_targets):
            if target is button:
                self._icon_targets[index] = (button, name)
                break
        button.setIcon(make_icon(name, theme().palette.text_2, 16, width=1.4))

    # ---------- 状态 ----------
    def set_theme_icon(self, dark: bool) -> None:
        self._set_icon(self._theme_btn, "sun" if dark else "moon")
        self._theme_btn.setToolTip("切换到浅色主题" if dark else "切换到深色主题")

    def set_maximized(self, maximized: bool) -> None:
        self._set_icon(self._max_btn, "restore" if maximized else "maximize")
        self._max_btn.setToolTip("还原" if maximized else "最大化")

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)

    def set_controls_visible(self, visible: bool) -> None:
        """保留接口：主题切换在登录页/主界面都可用，其余窗口按钮始终显示。"""
        self._theme_btn.setVisible(True)

    # ---------- 拖动 ----------
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton:
            window = self.window()
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            window = self.window()
            if window.isMaximized():
                ratio = event.globalPosition().x() / max(1, window.width())
                window.showNormal()
                self._drag_offset = QPoint(int(window.width() * ratio), 20)
                self.set_maximized(False)
            window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
