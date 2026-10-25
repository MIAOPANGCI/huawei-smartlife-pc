"""主线程投递：把「需要在主线程执行的事情」排队，由 QTimer 统一抽取执行。

为什么不用跨线程 Signal
----------------------
PySide6 里从工作线程 ``signal.emit()`` 到主线程控件的行为取决于连接类型推断，
在部分组合（Python 可调用对象、非槽方法）下会变成**同线程直接调用**，表现为
「界面不动 / 回调丢失 / 偶发崩溃」。更稳的做法是：工作线程只把闭包塞进线程安全
队列，主线程用定时器抽取执行——所有 GUI 操作都明确发生在主线程。
"""

from __future__ import annotations

import queue
from typing import Callable

_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
_timer = None

MAX_PER_TICK = 200


def post(callback: Callable[[], None]) -> None:
    """从任意线程投递一个回调，稍后在主线程执行。"""
    _queue.put(callback)


def _drain() -> None:
    for _ in range(MAX_PER_TICK):
        try:
            callback = _queue.get_nowait()
        except queue.Empty:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - 单个回调异常不应阻塞后续任务
            import logging

            logging.getLogger(__name__).exception("主线程回调执行出错")


def attach() -> None:
    """在主线程创建定时器开始抽取队列（QApplication 创建后调用一次）。"""
    global _timer
    if _timer is not None:
        return
    from PySide6.QtCore import QTimer

    _timer = QTimer()
    _timer.setInterval(20)
    _timer.timeout.connect(_drain)
    _timer.start()
