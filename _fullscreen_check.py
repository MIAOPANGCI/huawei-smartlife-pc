"""验证全屏：单台画面窗和多路同屏都能进/出全屏，且退出时周边控件都回来。

全屏靠 showFullScreen()，offscreen 平台也支持；这里重点是"隐藏了什么、恢复了什么"。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import app.dialogs.camera_wall as wall_module  # noqa: E402
from app.dialogs.camera_dialog import CameraDialog  # noqa: E402
from app.dialogs.camera_wall import CameraWall  # noqa: E402
from app.theme import set_theme  # noqa: E402
from app.widgets.repaint_guard import RepaintGuard  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


class FakeGrabber(QObject):
    frame_ready = Signal(QImage)
    opened = Signal(int, int)
    failed = Signal(str)

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self.url = url
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def wait(self, msecs: int = 0) -> bool:
        return True

    def terminate(self) -> None:
        pass


def margins_of(widget) -> tuple[int, int, int, int]:
    m = widget._shell.contentsMargins()
    return (m.left(), m.top(), m.right(), m.bottom())


def test_single_dialog(app: QApplication) -> None:
    dialog = CameraDialog(None, {}, manual_name="测试摄像头", manual_key="probe")
    dialog.show()
    app.processEvents()

    check("初始不是全屏", dialog.isFullScreen() is False)
    check("初始标题栏可见", dialog._titlebar.isVisible())
    check("初始表单区可见", dialog._chrome_bottom.isVisible())

    dialog._toggle_fullscreen()
    app.processEvents()
    check("进入全屏", dialog.isFullScreen() is True)
    check("全屏后收起标题栏", not dialog._titlebar.isVisible())
    check("全屏后收起提示文字", not dialog._tip.isVisible())
    check("全屏后收起表单与按钮", not dialog._chrome_bottom.isVisible())
    check("全屏后去掉外边距（画面贴边）", margins_of(dialog) == (0, 0, 0, 0))
    check("全屏后关掉阴影（否则边缘发虚）", not dialog._shadow.isEnabled())
    check("画面本身还在", dialog._view.isVisible())

    # ESC 退出
    dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    app.processEvents()
    check("ESC 退出全屏", dialog.isFullScreen() is False)
    check("退出后标题栏回来", dialog._titlebar.isVisible())
    check("退出后表单回来", dialog._chrome_bottom.isVisible())
    check("退出后外边距恢复", margins_of(dialog) == (14, 14, 14, 14))

    # 非全屏时按 ESC 不应有任何副作用（默认 QDialog 会关窗，那样误触就丢了画面）
    dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    app.processEvents()
    check("非全屏时 ESC 不误触", dialog.isFullScreen() is False and dialog.isVisible())

    dialog.close()


def test_wall(app: QApplication) -> None:
    wall_module.FrameGrabber = FakeGrabber  # type: ignore[assignment]
    targets = [
        {"key": "a", "name": "门口", "config": {"host": "192.0.2.11"}},
        {"key": "b", "name": "院子", "config": {"host": "192.0.2.12"}},
    ]
    wall = CameraWall(targets, ["a", "b"])
    wall.show()
    app.processEvents()

    check("同屏初始不是全屏", wall.isFullScreen() is False)
    check("同屏初始标题栏可见", wall._titlebar.isVisible())

    wall._toggle_fullscreen()
    app.processEvents()
    check("同屏进入全屏", wall.isFullScreen() is True)
    check("同屏全屏后收起标题栏", not wall._titlebar.isVisible())
    check("同屏全屏后去掉外边距", margins_of(wall) == (0, 0, 0, 0))
    check("同屏全屏后关掉阴影", not wall._shadow.isEnabled())
    check("格子还在", len(wall._tiles) == 2 and all(t.isVisible() for t in wall._tiles.values()))

    wall.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    app.processEvents()
    check("同屏 ESC 退出全屏", wall.isFullScreen() is False)
    check("同屏退出后标题栏回来", wall._titlebar.isVisible())
    check("同屏退出后外边距恢复", margins_of(wall) == (14, 14, 14, 14))

    # 双击任一格 = 切全屏（格子上装了事件过滤器）
    tile = wall._tiles["a"]
    dbl = QEvent(QEvent.MouseButtonDblClick)
    handled = tile.eventFilter(tile._view, dbl)
    app.processEvents()
    check("双击画面能进全屏", handled and wall.isFullScreen() is True)
    wall.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    app.processEvents()
    check("再按 ESC 退回", wall.isFullScreen() is False)

    # ---- 全屏状态下删光所有格子 ----
    # 曾经这样删完只剩一句"点右上角 + 添加"，而标题栏还在全屏的隐藏状态里，
    # 按钮根本不存在，等于卡死在一个空窗口
    wall._toggle_fullscreen()
    app.processEvents()
    check("先进入全屏", wall.isFullScreen() is True)
    for key in list(wall.selection()):
        wall._remove(key)
    app.processEvents()
    check("全屏下删光会自动退出全屏", wall.isFullScreen() is False)
    check("删光后标题栏回来了（能点 + 添加）", wall._titlebar.isVisible())
    check("删光后显示空态提示", wall._placeholder is not None)
    check("空态下窗口收成小尺寸", wall.width() < 700)

    # ---- 全屏下增删格子 ----
    # resize 会让 Qt 悄悄退出全屏，而标题栏的显隐是我们自己维护的；
    # 不同步就会出现"没有标题栏、也没在全屏"的怪窗口（删到只剩一路时最容易撞上）
    wall._add("a")
    app.processEvents()
    check("空态加回一路后标题栏仍在", wall._titlebar.isVisible())
    check("单路时窗口按大画面铺开", wall.width() > 700)

    wall._set_fullscreen(True)
    app.processEvents()
    check("进入全屏", wall.isFullScreen())
    wall._add("b")
    app.processEvents()
    check("全屏下加一路不影响全屏状态", wall.isFullScreen())
    wall._remove("b")
    app.processEvents()
    check("全屏下删回一路仍是全屏", wall.isFullScreen())
    check("全屏时标题栏本来就该藏着", not wall._titlebar.isVisible())

    wall._set_fullscreen(False)
    app.processEvents()
    check("退出全屏后标题栏回来", wall._titlebar.isVisible())

    wall.close()


class _RepaintSpy(QWidget):
    """记下 repaint 被真正调用了几次。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def repaint(self) -> None:  # noqa: N802 - Qt 接口
        self.calls += 1


def test_repaint_guard(app: QApplication) -> None:
    """退出全屏后要补一次整窗重绘。

    这窗口是半透明 + 阴影特效，从全屏缩回时 Qt 只重画变化的局部区域，屏幕上
    留着全屏那一版（标题栏像"消失"、格子错位），而各项 geometry 全是对的。
    只有真在屏幕上抓图才看得见，offscreen 抓不到——所以这里退一步验证机制：
    确实安排了重绘、到点真的画、连续 resize 会合并。
    """
    spy = _RepaintSpy()
    guard = RepaintGuard(spy, delay_ms=30)
    guard.schedule()
    check("schedule 后定时器在跑", guard._timer.isActive())
    QTest.qWait(150)
    check("到点真的调用 repaint", spy.calls == 1)

    spy.calls = 0
    guard.schedule()
    guard.schedule()
    guard.schedule()
    QTest.qWait(150)
    check("连续 schedule 合并成一次", spy.calls == 1)

    # 两个画面窗都挂了兜底，且退出全屏会真的安排重绘
    dialog = CameraDialog(None, {"host": "192.0.2.11"}, manual_name="门口", manual_key="p")
    check("单台画面窗挂了重绘兜底", isinstance(getattr(dialog, "_repaint_guard", None), RepaintGuard))
    dialog._repaint_guard._timer.stop()
    dialog._toggle_fullscreen()
    QTest.qWait(50)
    dialog._toggle_fullscreen()
    QTest.qWait(20)
    check("单台画面窗退出全屏会安排重绘", dialog._repaint_guard._timer.isActive())
    dialog.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")

    test_single_dialog(app)
    test_wall(app)
    test_repaint_guard(app)

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
