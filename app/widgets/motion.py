"""动画工具箱：统一的时长、缓动与收尾清理。

为什么收口
----------
1. ``QGraphicsOpacityEffect`` 每用一个就多一次离屏合成，**淡入结束后必须摘掉**，
   否则长列表滚动会明显掉帧。这个"忘了清理"的坑一旦散落在各处迟早会踩，
   所以统一在这里用 ``fade_in`` 处理，调用方不需要记住。
2. 动画只是装饰，不该影响可用性：设置里关掉之后这里全部退化成"直接显示"，
   各个页面不用各自判断。

时长都控制在 120~220ms 之间：再长会挡住操作，再短看不出过渡。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

# 全局开关：设置里「界面动画」关掉后置 False，所有过渡退化为瞬时
ENABLED = True

# 常用时长（毫秒）
FAST = 120
NORMAL = 180
SLOW = 240

_EXIT_CURVE = QEasingCurve.OutCubic


def enabled() -> bool:
    return ENABLED


def set_enabled(value: bool) -> None:
    global ENABLED
    ENABLED = bool(value)


def fade_in(
    widget: QWidget,
    duration: int = NORMAL,
    delay: int = 0,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """让控件从透明淡入到不透明。

    delay 用于列表错峰出现；动画结束后会摘掉 effect 并释放动画对象，
    因此可以放心地在列表重建时反复调用。
    """
    if widget is None:
        return None
    if not ENABLED or duration <= 0:
        if on_finished is not None:
            QTimer.singleShot(0, on_finished)
        return None

    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)

    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(_EXIT_CURVE)

    def _finish() -> None:
        # 先摘 effect 再放动画：反过来会在动画仍持有 effect 时把它删掉
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)
        if on_finished is not None:
            on_finished()
        animation.deleteLater()

    animation.finished.connect(_finish)

    if delay > 0:
        QTimer.singleShot(delay, animation.start)
    else:
        animation.start()
    return animation


def fade_out(
    widget: QWidget,
    duration: int = FAST,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """淡出到全透明，结束后摘掉 effect（控件本身仍可见，只是透明）。"""
    if widget is None:
        return None
    if not ENABLED or duration <= 0:
        if on_finished is not None:
            QTimer.singleShot(0, on_finished)
        return None

    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)

    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.setEasingCurve(QEasingCurve.InCubic)

    def _finish() -> None:
        if widget.graphicsEffect() is effect:
            effect.setOpacity(1.0)
            widget.setGraphicsEffect(None)
        if on_finished is not None:
            on_finished()
        animation.deleteLater()

    animation.finished.connect(_finish)
    animation.start()
    return animation


def slide_in(widget: QWidget, offset: int = 10, duration: int = NORMAL) -> None:
    """让顶层窗口从下方 offset 像素滑到当前位置（对话框弹出用）。

    用位置而不是透明度：无边框 + 半透明窗口上 ``windowOpacity`` 在部分
    平台不生效，位置动画则到处都稳。
    """
    if widget is None or not ENABLED or duration <= 0:
        return
    target = widget.pos()
    widget.move(QPoint(target.x(), target.y() + offset))
    animation = QPropertyAnimation(widget, b"pos", widget)
    animation.setDuration(duration)
    animation.setStartValue(widget.pos())
    animation.setEndValue(target)
    animation.setEasingCurve(_EXIT_CURVE)
    animation.finished.connect(animation.deleteLater)
    animation.start()
    # 挂到控件上，防被 GC 掉（QPropertyAnimation 的 parent 已经是 widget，
    # 这里再持一份引用，避免 Python 侧提前回收）
    widget._slide_animation = animation  # noqa: SLF001 - 生命周期兜底
