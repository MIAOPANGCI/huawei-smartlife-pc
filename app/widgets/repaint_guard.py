"""尺寸骤变后补一次整窗重绘。

多路同屏和单台画面窗都是 ``WA_TranslucentBackground`` + ``QGraphicsDropShadowEffect``。
从全屏缩回时 Qt 只重画发生变化的局部区域，屏幕上残留的是全屏那一版内容——
看着像标题栏被吃掉、格子错位，实际上几何完全正确（查 geometry 全对）。
用户看到的就是"花屏/卡住"，只有拖一下窗口或切走再切回才恢复。

两个细节是踩出来的：

- **必须等窗口真正缩完**。``showNormal()`` 只是发请求，实际 resize 要走窗口
  系统消息；在同一轮事件循环里重绘，画到的还是旧尺寸的缓冲。实测 80ms 足够，
  而 0ms 和"紧跟 showNormal 之后"都无效。
- **用 repaint() 而不是 update()**。update() 只是排队，会并进窗口系统那次
  resize 重绘里，等于没做。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget


class RepaintGuard:
    """给半透明窗口挂一个延时整窗重绘。"""

    def __init__(self, widget: QWidget, delay_ms: int = 80) -> None:
        self._widget = widget
        self._timer = QTimer(widget)
        self._timer.setSingleShot(True)
        # 连续 resize 时不断重置，只在尺寸稳定后画这一次
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._repaint)

    def schedule(self) -> None:
        """安排在尺寸稳定后重绘一次（重复调用会合并）。"""
        self._timer.start()

    def _repaint(self) -> None:
        self._widget.repaint()
