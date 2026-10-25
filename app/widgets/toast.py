"""轻量通知（Toast）：右下角淡入淡出，不打断操作。

主窗口持有一个 ToastHost，任何地方调用 ``toast.show("已开启", "success")``。
同屏只保留最近 3 条，自动向上堆叠，超时后淡出移除。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import theme
from .labels import ElidedLabel

_KIND_COLORS = {
    "info": "accent",
    "success": "ok",
    "warning": "warn",
    "danger": "danger",
}


class _ToastCard(QFrame):
    """单条通知卡片。"""

    def __init__(self, text: str, kind: str, host: "ToastHost") -> None:
        super().__init__(host)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._host = host

        palette = theme().palette
        color_key = _KIND_COLORS.get(kind, "accent")
        color = getattr(palette, color_key)

        self.setStyleSheet(
            f"""
            QFrame#Panel {{
                background: {palette.surface_3};
                border: 1px solid {palette.border};
                border-left: 3px solid {color};
                border-radius: 10px;
            }}
            QLabel {{
                color: {palette.text};
                font-size: 12px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 14, 9)
        # 单行 + 自动省略：Toast 是短提示，换行会让高度算错、文字被裁。
        # 水平策略用 Ignored：长度不参与 sizeHint，避免把宿主撑开或压扁。
        label = ElidedLabel(text)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        label.setMinimumHeight(18)
        layout.addWidget(label, 1)

        # 一个控件只能挂一个 graphics effect：这里要淡入淡出，就不叠阴影
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)
        self._removed = False

    def fade_in(self) -> None:
        self._fade(0.0, 1.0, 160)

    def fade_out(self) -> None:
        if self._removed:
            return
        self._fade(1.0, 0.0, 200)

    def _fade(self, start: float, end: float, duration: int) -> None:
        animation = QPropertyAnimation(self._opacity, b"opacity", self)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda: self._on_fade_done(end))
        animation.start()
        self._animation = animation  # 保持引用，避免被 GC

    def _on_fade_done(self, end: float) -> None:
        if end <= 0.0 and not self._removed:
            self._removed = True
            self._host.remove(self)


class ToastHost(QWidget):
    """Toast 容器：贴在父窗口右下角，透明、不吃鼠标事件。"""

    MAX_VISIBLE = 3

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self._cards: list[_ToastCard] = []
        self.hide()

    def show_toast(self, text: str, kind: str = "info", duration: int = 2800) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        card = _ToastCard(text, kind, self)
        self._layout.addWidget(card)
        self._cards.append(card)
        while len(self._cards) > self.MAX_VISIBLE:
            oldest = self._cards[0]
            self._cards.remove(oldest)
            oldest.fade_out()
        self.show()
        self.raise_()
        self._reposition()
        # 新卡片的 sizeHint 要等这一轮布局结束后才准确，下一轮事件循环再定一次位
        QTimer.singleShot(0, self._reposition)
        card.fade_in()
        QTimer.singleShot(max(600, duration), card.fade_out)

    def remove(self, card: _ToastCard) -> None:
        if card in self._cards:
            self._cards.remove(card)
        # 从布局里摘掉再销毁；setParent(None) 会临时把它变成顶层窗口
        self._layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        if not self._cards:
            self.hide()
        else:
            self._reposition()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(380, max(240, parent.width() - 80))
        self.setFixedWidth(width)
        self._layout.activate()
        height = max(40, self._layout.sizeHint().height())
        self.setGeometry(
            parent.width() - width - 22,
            parent.height() - height - 22,
            width,
            height,
        )
        self.raise_()
