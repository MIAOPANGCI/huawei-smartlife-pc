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
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.dialogs.camera_wall as wall_module  # noqa: E402
from app.dialogs.camera_dialog import CameraDialog  # noqa: E402
from app.dialogs.camera_wall import CameraWall  # noqa: E402
from app.theme import set_theme  # noqa: E402

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

    wall.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")

    test_single_dialog(app)
    test_wall(app)

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
