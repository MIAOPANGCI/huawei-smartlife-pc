"""验证多路同屏：格数排布、增删、选择持久化、线程释放。

不联网：读帧线程换成假的，只验证窗口怎么组织和调度。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.dialogs.camera_wall as wall_module  # noqa: E402
from app.dialogs.camera_wall import MAX_TILES, CameraWall  # noqa: E402
from app.theme import set_theme  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


class FakeGrabber(QObject):
    """替身：记录被启动/停止，不真的解码。"""

    frame_ready = Signal(QImage)
    opened = Signal(int, int)
    failed = Signal(str)

    instances: list["FakeGrabber"] = []

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self.url = url
        self.running = False
        FakeGrabber.instances.append(self)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def wait(self, msecs: int = 0) -> bool:
        return True

    def terminate(self) -> None:
        self.running = False


def target(key: str, name: str, host: str = "") -> dict:
    return {"key": key, "name": name, "config": {"host": host} if host else {}}


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")

    wall_module.FrameGrabber = FakeGrabber  # type: ignore[assignment]

    targets = [
        target("cam-door", "门口摄像头", "192.0.2.11"),
        target("cam-yard", "院子摄像头", "192.0.2.12"),
        target("cam-living", "客厅摄像头"),          # 未配置
        target("cam-garage", "车库摄像头", "192.0.2.13"),
    ]

    # ---- 1. 默认选择 ----
    wall = CameraWall(targets, None)
    check("默认挑已配置的那几台", set(wall.selection()) == {"cam-door", "cam-yard", "cam-garage"})
    check("未配置的不进默认选择", "cam-living" not in wall.selection())
    check("每路都起了读帧线程", len(FakeGrabber.instances) == 3)
    check("线程都处于运行态", all(item.running for item in FakeGrabber.instances))

    # ---- 2. 格数排布 ----
    positions = sorted(wall._grid.getItemPosition(i)[:2] for i in range(wall._grid.count()))
    check("3 路按 2 列排（2×2）", positions == [(0, 0), (0, 1), (1, 0)])

    # ---- 3. 增删：只动变动的那一格 ----
    added: list[list[str]] = []
    wall.selection_changed.connect(added.append)

    wall._remove("cam-garage")                  # 3 路 → 2 路
    check("删到 2 路后空行不再抢高度", wall._grid.rowStretch(1) == 0)
    check("2 路时窗口高度跟着收", wall.height() < 500)
    positions = sorted(wall._grid.getItemPosition(i)[:2] for i in range(wall._grid.count()))
    check("2 路横向并排", positions == [(0, 0), (0, 1)])

    total_before = len(FakeGrabber.instances)
    before = [item for item in FakeGrabber.instances if item.running]
    wall._add("cam-garage")                     # 2 路 → 3 路（已配置的，会真连）
    check("加一路只新增一个读帧线程", len(FakeGrabber.instances) == total_before + 1)
    check("原有几路没有被重连", all(item.running for item in before))
    check("加一路会通知外部（用于持久化）", added and added[-1][-1] == "cam-garage")

    last = FakeGrabber.instances[-1]
    wall._remove("cam-garage")
    check("移除只停掉被删那一格", not last.running)
    check("其余几路还在跑", all(item.running for item in before))
    check("移除后通知外部", added[-1] == ["cam-door", "cam-yard"])

    # ---- 3b. 未配置的那台只占格不连流 ----
    count_before = len(FakeGrabber.instances)
    wall._add("cam-living")
    check("未配置的格子不会起读帧线程", len(FakeGrabber.instances) == count_before)
    check("未配置的格子给出提示而不是崩", "地址" in wall._tiles["cam-living"]._view._hint)
    wall._remove("cam-living")

    # ---- 5. 上限 ----
    many = [target(f"c{i}", f"摄像头{i}", "192.0.2.20") for i in range(12)]
    big = CameraWall(many, [f"c{i}" for i in range(MAX_TILES)])
    check(f"最多 {MAX_TILES} 路", len(big.selection()) == MAX_TILES)
    before = len(big.selection())
    big._add("c11")
    check("超出上限时拒绝添加", len(big.selection()) == before)
    big.close()

    # ---- 6. 失效的选择 ----
    stale = CameraWall(targets, ["cam-door", "已经删掉的摄像头"])
    check("上次选中的摄像头被删了就跳过", stale.selection() == ["cam-door"])
    stale.close()

    # ---- 7. 关闭释放线程 ----
    FakeGrabber.instances.clear()
    closing = CameraWall(targets, ["cam-door", "cam-yard"])
    check("关闭前线程在跑", all(item.running for item in FakeGrabber.instances))
    closing.close()
    check("关闭后所有线程已停", all(not item.running for item in FakeGrabber.instances))
    check(
        "关闭后格子不再持有读帧对象",
        all(tile._grabber is None for tile in closing._tiles.values()),
    )

    wall.close()

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
