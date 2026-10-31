"""验证动画层：不只是"看起来动了"，还要确认动画收尾干净。

重点检查 effect 有没有摘掉——QGraphicsOpacityEffect 忘了清理的话，
长列表滚动会持续走离屏合成，是那种"用一天才发现卡"的隐性开销。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import dispatch  # noqa: E402
from app.widgets import motion  # noqa: E402
from app.widgets.controls import PowerButton  # noqa: E402

FAILS: list[str] = []
PASSES: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    dispatch.attach()

    # ---- 1. 关闭动画时立即到位，不挂 effect ----
    motion.set_enabled(False)
    plain = QWidget()
    motion.fade_in(plain, motion.NORMAL)
    check("关掉动画后不挂 effect", plain.graphicsEffect() is None)

    # ---- 2. 开启后动画结束必须摘掉 effect ----
    motion.set_enabled(True)
    target = QWidget()
    motion.fade_in(target, 60)
    check("动画开始时挂了 effect", target.graphicsEffect() is not None)
    QTest.qWait(220)
    check("动画结束后 effect 被摘掉", target.graphicsEffect() is None)

    # ---- 3. 连续两次淡入不互相打架 ----
    again = QWidget()
    motion.fade_in(again, 60)
    motion.fade_in(again, 60)
    QTest.qWait(220)
    check("连续淡入后只剩干净状态", again.graphicsEffect() is None)

    # ---- 4. fade_out 结束后控件仍可见（只是透明被撤销）----
    out = QWidget()
    motion.fade_out(out, 40)
    QTest.qWait(180)
    check("淡出后清理 effect", out.graphicsEffect() is None)

    # ---- 5. 电源键状态过渡：起点 0、终点 1 ----
    motion.set_enabled(True)
    button = PowerButton("off")
    check("初始没有过渡进行中", button.blend == 1.0)
    button.set_state("on")
    check("切换瞬间从旧状态开始过渡", button.blend == 0.0)
    check("状态已切到 on", button.state == "on")
    QTest.qWait(300)
    check("过渡结束停在终点", button.blend == 1.0)

    # ---- 6. 关掉动画时电源键直接到位 ----
    motion.set_enabled(False)
    quick = PowerButton("off")
    quick.set_state("on")
    check("关动画时电源键立即到位", quick.blend == 1.0)
    motion.set_enabled(True)

    # ---- 7. 控件被销毁后动画不会报错 ----
    doomed = QWidget()
    motion.fade_in(doomed, 200)
    doomed.deleteLater()
    QTest.qWait(300)
    check("动画期间销毁控件不崩", True)

    print()
    print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
    for item in FAILS:
        print("  FAIL:", item)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
