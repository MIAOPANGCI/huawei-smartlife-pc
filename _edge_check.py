"""验证桌面挂件的贴边自动隐藏：吸附判定、缩进屏幕外、悬停滑出、状态落盘。

这几条里最容易错的是坐标：缩回时窗口大部分在屏幕外，如果把缩回坐标
当成「位置」存进设置，下次启动挂件就直接出现在屏幕外，用户会以为它坏了。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.widget_board as wb  # noqa: E402
from app.widgets import motion  # noqa: E402

FAILS: list[str] = []
PASSES: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


class FakeCursor:
    """替掉真实鼠标，免得测试去动用户的指针。"""

    point = QPoint(0, 0)

    @classmethod
    def pos(cls) -> QPoint:
        return cls.point


wb.QCursor = FakeCursor  # type: ignore[assignment]


def drive_until_peeked(board: wb.WidgetBoard) -> bool:
    for _ in range(60):
        board._poll_cursor()
        if board.peeked:
            return True
    return False


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    motion.set_enabled(False)   # 动画关掉，位置变化立刻可见，断言才稳定

    board = wb.WidgetBoard({"locked": False, "collapsed": False})
    board.show()
    app.processEvents()
    area = board._screen_area()
    width, height = board.width(), board.height()
    print(f"screen={area.x()},{area.y()} {area.width()}x{area.height()}  board={width}x{height}")

    # ---- 1. 右边缘吸附 ----
    board.move(area.right() - width + 5, area.top() + 120)
    board._maybe_dock()
    check("碰到右边缘会吸附", board.edge == "right")
    check("吸附后紧贴右边", board.x() == area.right() - width + 1)

    # ---- 2. 鼠标移开自动缩进 ----
    FakeCursor.point = QPoint(area.left() + 40, area.top() + 40)
    check("鼠标移开后缩进屏幕外", drive_until_peeked(board))
    check(
        "缩进后只留 6px 在屏幕内",
        board.x() == area.right() - wb.EDGE_PEEK + 1,
    )
    check("缩进后确实大部分在屏幕外", board.x() + width > area.right() + 2)

    # ---- 3. 悬停露出的窄边滑出 ----
    strip_x = area.right() - wb.EDGE_PEEK + 1
    FakeCursor.point = QPoint(strip_x + 2, board.y() + height // 2)
    board._poll_cursor()
    check("碰到窄边滑出", not board.peeked)
    check("滑出后回到紧贴右边", board.x() == area.right() - width + 1)

    # ---- 4. 落盘坐标必须是展开位置 ----
    FakeCursor.point = QPoint(area.left() + 40, area.top() + 40)
    drive_until_peeked(board)
    seen: list[dict] = []
    board.state_changed.connect(seen.append)
    board._emit_state()
    saved = seen[-1] if seen else {}
    check("状态里带 edge", saved.get("edge") == "right")
    check("缩进时仍记录展开坐标", saved.get("x") == area.right() - width + 1)

    # ---- 5. 左边缘 ----
    board.move(area.left() - 4, area.top() + 60)
    board._maybe_dock()
    check("碰到左边缘会吸附", board.edge == "left")
    check("吸附后紧贴左边", board.x() == area.left())
    FakeCursor.point = QPoint(area.right() - 30, area.top() + 300)
    check("左侧也能自动缩进", drive_until_peeked(board))
    check("左侧缩进后只留 6px", board.x() == area.left() - width + wb.EDGE_PEEK)

    # ---- 6. 顶部 ----
    board.move(area.left() + 40, area.top() + 3)
    board._maybe_dock()
    check("碰到顶部会吸附", board.edge == "top")
    check("吸附后紧贴顶部", board.y() == area.top())
    FakeCursor.point = QPoint(area.left() + 60, area.bottom() - 20)
    check("顶部也能自动缩进", drive_until_peeked(board))
    check("顶部缩进后只留 6px", board.y() == area.top() - height + wb.EDGE_PEEK)

    # ---- 7. 拖到中间就脱离 ----
    board.move(area.left() + 120, area.top() + 160)
    board._maybe_dock()
    check("离开边缘后不再自动隐藏", board.edge is None)
    check("脱离后位置保持", board.pos() == QPoint(area.left() + 120, area.top() + 160))

    # ---- 8. 锁定后仍可自动隐藏（锁定只禁拖动）----
    board._locked = True
    board.move(area.right() - width + 2, area.top() + 140)
    board._maybe_dock()
    check("锁定状态照样吸附", board.edge == "right")

    # ---- 9. 恢复保存的 edge ----
    board2 = wb.WidgetBoard({"locked": False, "collapsed": False, "edge": "left"})
    board2.show()
    app.processEvents()
    board2.show_near()
    check("重启后按保存的边贴回去", board2.edge == "left" and board2.x() == area.left())
    check("重启后先完整展开而不是直接隐藏", not board2.peeked)

    board.hide()
    board2.hide()

    # ---- 10. 位置记忆 ----
    saved = wb.WidgetBoard({"x": 320, "y": 240})
    saved.show()
    app.processEvents()
    check("构造时恢复上次位置", saved.pos() == QPoint(320, 240))

    saved.show_near()
    app.processEvents()
    check("显示时沿用保存位置（不跳回右下角）", saved.pos() == QPoint(320, 240))

    outside = wb.WidgetBoard({"x": 9999, "y": 9999})
    outside.show()
    app.processEvents()
    outside.show_near()
    app.processEvents()
    area = outside._screen_area()
    check(
        "越界坐标被夹回屏幕内",
        area.left() <= outside.x()
        and outside.x() + outside.width() <= area.right() + 1,
    )
    outside.hide()

    # ---- 11. 点 × 关掉 vs 退出时隐藏 ----
    dismissed: list[bool] = []
    saved.dismissed.connect(lambda: dismissed.append(True))
    saved.close_by_user()
    app.processEvents()
    check("点 × 会发出 dismissed", dismissed == [True])
    check("点 × 后窗口隐藏", not saved.isVisible())

    saved.show()
    app.processEvents()
    saved.hide()          # 模拟退出流程里的 board.hide()
    app.processEvents()
    check("退出时的隐藏不触发 dismissed", dismissed == [True])

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
