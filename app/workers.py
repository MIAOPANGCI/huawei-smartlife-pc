"""后台任务：把阻塞的 HTTP / MQTT 操作放到工作线程，结果回主线程。

工作线程只做计算与网络调用，**绝不触碰任何 Qt 控件**；结果通过
``app.dispatch.post`` 投递到主线程执行（见该模块的说明）。
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Callable

from .dispatch import post

_LOGGER = logging.getLogger(__name__)

Callback = Callable[[object], None]


class TaskRunner:
    """轻量线程池：限制并发数，回调一律在主线程执行。"""

    def __init__(self, max_threads: int = 6) -> None:
        self._semaphore = threading.Semaphore(max_threads)
        self._callbacks: dict[int, tuple[Callback | None, Callback | None]] = {}
        self._lock = threading.Lock()
        self._tokens = itertools.count(1)
        self._pending = 0

    # ---------- 主线程侧 ----------
    def _deliver_done(self, token: int, result: object) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            pair = self._callbacks.pop(token, None)
        if pair is None:
            return
        on_done, _ = pair
        if on_done is None:
            return
        try:
            on_done(result)
        except Exception:  # noqa: BLE001 - 单个回调异常不影响其它任务
            _LOGGER.exception("任务完成回调执行出错")

    def _deliver_failed(self, token: int, error: BaseException) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            pair = self._callbacks.pop(token, None)
        if pair is None:
            return
        _, on_error = pair
        if on_error is None:
            _LOGGER.error("后台任务异常: %s", error)
            return
        try:
            on_error(error)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("任务失败回调执行出错")

    # ---------- 对外 ----------
    def submit(self, fn, on_done=None, on_error=None) -> None:
        """提交任务；on_done(result) / on_error(exception) 在主线程执行。"""
        with self._lock:
            token = next(self._tokens)
            self._callbacks[token] = (on_done, on_error)
            self._pending += 1

        def job() -> None:
            with self._semaphore:
                try:
                    result = fn()
                except Exception as error:  # noqa: BLE001
                    _LOGGER.debug("后台任务失败: %s", error, exc_info=True)
                    # 用默认参数捕获异常对象：except 结束后 Python 会删除 error
                    post(lambda err=error: self._deliver_failed(token, err))
                else:
                    post(lambda res=result: self._deliver_done(token, res))

        threading.Thread(target=job, daemon=True, name=f"task-{token}").start()

    def pending(self) -> int:
        with self._lock:
            return self._pending

    def wait(self, msecs: int = 3000) -> bool:
        """退出时给未完成任务一点收敛时间（daemon 线程不会被强杀）。"""
        import time

        deadline = time.monotonic() + msecs / 1000.0
        while time.monotonic() < deadline:
            if self.pending() == 0:
                return True
            time.sleep(0.05)
        return False


# 全局线程池（应用生命周期内复用）
_runner: TaskRunner | None = None


def runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner


def run_async(fn, on_done=None, on_error=None) -> None:
    """便捷函数：``run_async(lambda: client.get_snapshot(), on_ok, on_fail)``。"""
    runner().submit(fn, on_done, on_error)
