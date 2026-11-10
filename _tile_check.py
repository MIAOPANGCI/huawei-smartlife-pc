"""桌面磁贴自检：按设备增删、显示隐藏、位置记忆与夹取、信号转发。

磁贴是每台设备一个**独立顶层窗口**，最容易出问题的是"重复同步时重建窗口"
（会闪一下并丢位置）和"退出时不回收"（窗口留在桌面上）。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.theme import set_theme  # noqa: E402
from app.widget_tile import (  # noqa: E402
    GLOW_PAD,
    TILE_GAP,
    TILE_HEIGHT,
    TILE_SIZES,
    TILE_WIDTH,
    DeviceTile,
    TileBoard,
    _screen_area,
)
from huawei_smartlife.models import Device, ServiceState  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def make_device(
    dev_id: str,
    name: str,
    *,
    online: bool | None = True,
    on: bool | None = None,
    temp: float | None = None,
    with_switch: bool = True,
) -> Device:
    if not with_switch:
        return Device(home_id="h", dev_id=dev_id, name=name, online=online, services={})
    data: dict = {}
    if on is not None:
        data["on"] = on
    if temp is not None:
        data["temperature"] = temp
    return Device(
        home_id="h",
        dev_id=dev_id,
        name=name,
        room_name="客厅",
        online=online,
        services={
            "switch": ServiceState(sid="switch", service_type="switch", data=data)
        },
    )


def test_build(app: QApplication) -> None:
    board = TileBoard()
    board.set_devices(
        [
            make_device("a", "客厅热水器", on=True, temp=48),
            make_device("b", "书房台灯", on=False),
            make_device("c", "阳台插座", online=False, on=False),
        ]
    )
    check("按设备建出磁贴", board.count == 3)
    tile = board._tiles["a"]
    check("磁贴尺寸固定", (tile.width(), tile.height()) == (TILE_WIDTH, TILE_HEIGHT))
    check("显示设备名", tile._name.text() == "客厅热水器")
    check("摘要带温度", "48" in tile._summary.full_text)
    check("离线设备显示离线", board._tiles["c"]._summary.full_text == "离线")
    check("离线设备打了标记", board._tiles["c"]._body.property("offline") == "true")

    plain = DeviceTile(make_device("x", "无开关设备", with_switch=False))
    check("没有开关能力时不显示电源键", plain._power.isHidden() is True)
    plain.setParent(None)
    plain.deleteLater()


def test_reuse(app: QApplication) -> None:
    """重复同步必须复用窗口：重建会闪一下，而且位置全丢。"""
    board = TileBoard({"a": {"x": 300, "y": 200}})
    board.set_devices([make_device("a", "旧名字", on=True)])
    first = board._tiles["a"]
    check("记住上次位置", (first.x(), first.y()) == (300, 200))

    board.set_devices([make_device("a", "新名字", on=True)])
    check("重复同步复用同一个窗口", board._tiles["a"] is first)
    check("内容跟着更新", board._tiles["a"]._name.text() == "新名字")

    board.set_devices([])
    check("设备消失后磁贴移除", board.count == 0)


def test_show_hide(app: QApplication) -> None:
    board = TileBoard()
    board.set_devices([make_device("a", "A", on=True), make_device("b", "B", on=True)])
    check("默认整体不显示", board.visible is False)
    check("默认磁贴也不可见", board._tiles["a"].isVisible() is False)

    board.show_all()
    app.processEvents()
    check("show_all 后可见", board.visible is True)
    check("每块都显示出来", all(t.isVisible() for t in board._tiles.values()))

    board.hide_all()
    app.processEvents()
    check("hide_all 后隐藏", board.visible is False)
    check("每块都藏起来", all(not t.isVisible() for t in board._tiles.values()))


def test_hide_one(app: QApplication) -> None:
    board = TileBoard()
    board.set_devices([make_device("a", "A", on=True), make_device("b", "B", on=True)])
    board.show_all()
    app.processEvents()

    board.hide_one("a")
    app.processEvents()
    check("隐藏单块只影响那一块", board._tiles["a"].isVisible() is False)
    check("另一块还在", board._tiles["b"].isVisible() is True)
    check("还有可见的，整体仍算显示", board.visible is True)

    board.hide_one("b")
    app.processEvents()
    check("全藏完整体归为不显示", board.visible is False)


def test_position(app: QApplication) -> None:
    saved: dict = {}
    board = TileBoard()
    board.on_state_changed = lambda state: saved.update(state)
    board.set_devices([make_device("a", "A", on=True)])
    tile = board._tiles["a"]

    tile.move(320, 240)
    tile._remember_pos()
    check("拖动后位置进快照", saved.get("a") == {"x": 320, "y": 240})
    check("snapshot 与上报一致", board.snapshot().get("a") == {"x": 320, "y": 240})

    # 换分辨率 / 拔掉副屏后，存下来的坐标可能在屏幕外
    area = _screen_area()
    tile.show_at(QPoint(area.right() + 800, area.bottom() + 800))
    app.processEvents()
    check("越界位置被夹回屏幕", tile.x() <= area.right() - TILE_WIDTH + 1)
    check("越界位置纵向也被夹回", tile.y() <= area.bottom() - TILE_HEIGHT + 1)
    tile.hide()


def test_drag(app: QApplication) -> None:
    """按住拖动要真的移动磁贴，松手才落盘（拖到一半别每次都写设置）。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    saved: dict = {}
    board = TileBoard()
    board.on_state_changed = lambda state: saved.update(state)
    board.set_devices([make_device("a", "A", on=True)])
    tile = board._tiles["a"]
    tile.move(200, 200)

    press = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(10, 10), QPointF(210, 210),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    )
    tile.mousePressEvent(press)
    check("按下时还没动", (tile.x(), tile.y()) == (200, 200))

    move = QMouseEvent(
        QEvent.MouseMove, QPointF(30, 30), QPointF(260, 260),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
    )
    tile.mouseMoveEvent(move)
    check("拖动会跟着鼠标移动", (tile.x(), tile.y()) == (250, 250))
    check("拖动过程中不写设置", saved == {})

    release = QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(30, 30), QPointF(260, 260),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
    )
    tile.mouseReleaseEvent(release)
    check("松手后位置落盘", saved.get("a") == {"x": 250, "y": 250})
    tile.hide()


def test_signals(app: QApplication) -> None:
    board = TileBoard()
    powered: list[str] = []
    dismissed: list[str] = []
    board.on_power = powered.append
    board.on_dismissed = dismissed.append
    board.set_devices([make_device("a", "A", on=True)])
    tile = board._tiles["a"]

    tile.power_requested.emit("a")
    check("电源请求转发到主窗口", powered == ["a"])

    tile.dismissed.emit("a")
    check("单块隐藏转发", dismissed == ["a"])
    tile.dismissed.emit("")
    check("全部隐藏转发（空字符串）", dismissed[-1] == "")


def test_close(app: QApplication) -> None:
    board = TileBoard()
    board.set_devices([make_device("a", "A", on=True), make_device("b", "B", on=True)])
    board.show_all()
    app.processEvents()
    board.close_all()
    check("退出后磁贴全部回收", board.count == 0)
    check("退出后不再标记显示", board.visible is False)


class _FakeStore:
    """替身设备仓库：只需要 all() 和 get()。"""

    def __init__(self, devices: list[Device]) -> None:
        self._items = {device.dev_id: device for device in devices}

    def all(self) -> list[Device]:
        return list(self._items.values())

    def get(self, dev_id: str) -> Device | None:
        return self._items.get(dev_id)


def test_integration(app: QApplication) -> None:
    """接进主窗口后：托盘切换、设置落盘、设备同步都要对。"""
    from app.controller import AppController
    from app.main_window import MainWindow

    controller = AppController()
    controller.restore_session = lambda: False
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None
    controller.store = _FakeStore(
        [make_device("a", "客厅热水器", on=True), make_device("b", "台灯", on=False)]
    )

    window = MainWindow(controller, start_hidden=True)
    # 读的是用户真实的 ui_settings.json，挡掉写盘
    window._save_settings = lambda: None
    window._settings.pop("tiles_visible", None)
    window._settings.pop("tiles_devices", None)

    window._sync_board_devices()
    check("主窗口把设备同步进磁贴", window.tile_board.count == 2)

    window._show_tiles()
    app.processEvents()
    check("托盘开关能显示磁贴", window.tile_board.visible is True)
    check("显示状态记进设置", window._settings.get("tiles_visible") is True)

    window._show_tiles()
    app.processEvents()
    check("再点一次隐藏", window.tile_board.visible is False)
    check("隐藏状态记进设置", window._settings.get("tiles_visible") is False)

    window._show_tiles()
    app.processEvents()
    window._on_tile_dismissed("")
    app.processEvents()
    check("右键「隐藏全部」生效", window.tile_board.visible is False)

    window._on_tiles_state_changed({"a": {"x": 111, "y": 222}})
    check("位置写进设置", window._settings.get("tiles_positions") == {"a": {"x": 111, "y": 222}})

    window.tile_board.close_all()
    window.close()


def make_ac(dev_id: str = "ac", name: str = "二楼空调") -> Device:
    """空调：带可写的设定温度 + 字符串模式枚举，卡片上会出控件。"""
    return Device(
        home_id="h",
        dev_id=dev_id,
        name=name,
        room_name="卧室",
        online=True,
        services={
            "ac": ServiceState(
                sid="ac",
                service_type="ac",
                data={
                    "on": True,
                    "targettemp": 27,
                    "mode": "auto",
                    "currenttemperature": 29,
                },
            )
        },
    )


def test_card_controls(app: QApplication) -> None:
    """卡片尺寸上的控件：温度加减、模式切换，都是发绝对命令。"""
    tile = DeviceTile(make_ac(), size_key="card")
    check("卡片上出现温度条", tile._stepper is not None and not tile._stepper.isHidden())
    check("温度条显示当前设定值", tile._stepper._value.text() == "27 °C")
    check("到上限时加号禁用", tile._stepper._plus.isEnabled() is True)

    commands: list[tuple[str, str, dict]] = []
    tile.command_requested.connect(lambda *args: commands.append(args))
    tile._stepper._plus.click()
    app.processEvents()
    check("点加号发出 +1 的命令", commands and commands[-1][2] == {"targettemp": 28.0})
    check("卡片上的数字同步变化", tile._stepper._value.text() == "28 °C")

    tile._stepper._minus.click()
    app.processEvents()
    check("点减号回到 27", commands[-1][2] == {"targettemp": 27.0})

    # 模式：当前模式排第一并高亮，其余是其它模式 + 更多
    chips = tile._chips._buttons
    labels = [button.text() for button in chips]
    check("模式块按当前模式排在前面", labels[0] == "自动")
    check("当前模式高亮", chips[0].isChecked() is True)
    check("最后一个是「更多」", labels[-1] == "更多")

    chips[1].click()
    app.processEvents()
    check("点模式块发出模式命令", commands[-1][2] == {"mode": "cool"})

    # 不支持的设备：只留「更多」，不硬塞控件
    plain = DeviceTile(make_device("p", "台灯", on=True), size_key="card")
    check("没有温度就不显示温度条", plain._stepper.isHidden() is True)
    check("没有模式时只剩「更多」", [b.text() for b in plain._chips._buttons] == ["更多"])

    # 离线时控件藏起来，避免对着离线设备发命令
    offline = DeviceTile(
        Device(home_id="h", dev_id="o", name="空调", online=False,
               services=make_ac().services),
        size_key="card",
    )
    check("离线设备不显示温度条", offline._stepper.isHidden() is True)
    tile.close(); plain.close(); offline.close()


def test_glow(app: QApplication) -> None:
    """沉浸光感：四档都在，改档不重建窗口。"""
    board = TileBoard()
    board.set_devices([make_device("a", "A", on=True)])
    tile = board._tiles["a"]
    check("默认是均衡", tile.glow == "balanced")
    check("默认光感强度在 0~1 之间", 0 < tile.glow_strength <= 1)

    board.set_options(glow="off")
    check("改档不重建窗口", board._tiles["a"] is tile)
    check("关闭后强度为 0", tile.glow_strength == 0.0)

    board.set_options(glow="strong")
    check("强档强度为 1", tile.glow_strength == 1.0)

    board.set_options(glow="乱填")
    check("非法档位被忽略", board.glow == "strong")
    board.close_all()


def test_wallpaper(app: QApplication) -> None:
    """壁纸层：挂不上要退化成普通窗口，不能变成一个看不见的窗口。"""
    from PySide6.QtCore import Qt as _Qt

    import app.widget_tile as tile_module

    original = tile_module.wallpaper.attach
    attempts: list[int] = []

    def fake_fail(hwnd: int):
        attempts.append(hwnd)
        return None

    def fake_ok(hwnd: int):
        attempts.append(hwnd)
        return (0, 0)

    try:
        tile_module.wallpaper.attach = fake_fail
        tile = DeviceTile(make_device("a", "A", on=True), wallpaper_mode=True)
        check("壁纸模式不开半透明（子窗口不支持分层）",
              tile.testAttribute(_Qt.WA_TranslucentBackground) is False)
        tile.show_at(QPoint(300, 300))
        app.processEvents()
        check("挂不上时不认为自己在壁纸层", tile.wallpaper_active is False)
        check("挂不上后关掉重试（不反复折腾自己）", tile._wallpaper_wanted is False)
        tile.hide()

        tile_module.wallpaper.attach = fake_ok
        ok_tile = DeviceTile(make_device("b", "B", on=True), wallpaper_mode=True)
        ok_tile.show_at(QPoint(300, 300))
        app.processEvents()
        check("挂上后标记为壁纸层", ok_tile.wallpaper_active is True)
        check("挂上后套上圆角遮罩", ok_tile.mask().isEmpty() is False)
        check("挂上后坐标换算用父窗口偏移", ok_tile._screen_pos() == ok_tile.pos() + ok_tile._offset)
        ok_tile.hide()

        # 非壁纸模式：保持半透明、不留遮罩
        normal = DeviceTile(make_device("c", "C", on=True), wallpaper_mode=False)
        check("普通模式仍是半透明窗口",
              normal.testAttribute(_Qt.WA_TranslucentBackground) is True)
        check("普通模式没有遮罩", normal.mask().isEmpty() is True)
        normal.close()
    finally:
        tile_module.wallpaper.attach = original


def test_window_behaviour(app: QApplication) -> None:
    """磁贴是"桌面上的方块"：不置顶、不进任务栏，但内容区不能吞掉子控件的点击。"""
    board = TileBoard()
    board.set_devices([make_device("a", "热水器", on=True)])
    tile = board._tiles["a"]
    flags = tile.windowFlags()
    check("磁贴不置顶（不该压住别的窗口）", not (flags & Qt.WindowStaysOnTopHint))
    check("磁贴是 Tool 窗口（不进任务栏 / Alt-Tab）", bool(flags & Qt.Tool))
    check("磁贴无边框", bool(flags & Qt.FramelessWindowHint))

    tile.show()
    app.processEvents()
    center = tile._power.mapTo(tile, tile._power.rect().center())
    check("电源键能被鼠标命中", tile.childAt(center) is tile._power)

    fired: list[str] = []
    tile.power_requested.connect(fired.append)
    QTest.mouseClick(tile._power, Qt.LeftButton)
    app.processEvents()
    check("点电源键会发出开关请求", fired == ["a"])

    # 这一条是回归防护：内容区一旦设了 WA_TransparentForMouseEvents，
    # 子控件（电源键）会跟着收不到鼠标事件，表现就是"开关点不了"
    check(
        "内容区没有禁用鼠标事件",
        not tile._body.testAttribute(Qt.WA_TransparentForMouseEvents),
    )
    board.close_all()


def test_sizes(app: QApplication) -> None:
    """三档尺寸都要真的换了尺寸，紧凑档还要自动省掉状态文字。"""
    for key, (width, height, _icon, _power, shows_summary) in TILE_SIZES.items():
        tile = DeviceTile(make_device("a", "A", on=True, temp=40), size_key=key)
        check(f"尺寸档 {key} 生效", (tile.width(), tile.height()) == (width, height))
        check(
            f"尺寸档 {key} 的状态文字可见性",
            tile._summary.isVisibleTo(tile) is shows_summary,
        )
        tile.close()
        tile.deleteLater()


def test_set_options(app: QApplication) -> None:
    board = TileBoard()
    board.set_devices([make_device("a", "A", on=True)])
    first = board._tiles["a"]
    check("默认是标准尺寸", (first.width(), first.height()) == (TILE_WIDTH, TILE_HEIGHT))

    board.set_options(align="grid")
    check("改对齐不重建窗口", board._tiles["a"] is first)
    check("对齐选项已更新", board.align == "grid")

    board.set_options(show_summary=False)
    check("关掉状态文字", board._tiles["a"]._summary.isHidden() is True)
    board.set_options(show_summary=True)
    check("再打开状态文字", board._tiles["a"]._summary.isHidden() is False)

    board.set_options(size_key="large")
    check("改尺寸会重建窗口", board._tiles["a"] is not first)
    check("新尺寸生效", board._tiles["a"].width() == TILE_SIZES["large"][0])
    check("重建后设备还在", board.count == 1)

    board.set_options(size_key="紧凑")     # 非法值应被忽略，不能把磁贴搞成 0 尺寸
    check("非法尺寸被忽略", board.size_key == "large")


def test_snap(app: QApplication) -> None:
    """拖动松手时按对齐方式吸附。"""
    board = TileBoard(align="grid")
    board.set_devices([make_device("a", "A", on=True)])
    tile = board._tiles["a"]

    area = _screen_area()
    step_x, step_y = tile.grid_step()
    tile.move(area.left() + 203, area.top() + 117)
    tile._remember_pos()
    offset = GLOW_PAD
    check(
        "网格对齐落在网格线上",
        (
            (tile.x() - area.left() - offset) % step_x,
            (tile.y() - area.top() - offset) % step_y,
        ) == (0, 0),
    )
    check("吸附后仍在原处附近", abs(tile.x() - area.left() - 203) <= step_x // 2)

    board.set_options(align="edge")
    tile = board._tiles["a"]
    tile.move(area.left() + 5, area.top() + 7)
    tile._remember_pos()
    check("贴边吸附到左上角", (tile.x(), tile.y()) == (area.left(), area.top()))

    board.set_options(align="free")
    tile.move(area.left() + 5, area.top() + 7)
    tile._remember_pos()
    check("自由摆放不吸附", (tile.x(), tile.y()) == (area.left() + 5, area.top() + 7))


def test_selection(app: QApplication) -> None:
    """自选设备：只放挑中的那几台。"""
    from app.controller import AppController
    from app.main_window import MainWindow

    controller = AppController()
    controller.restore_session = lambda: False
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None
    controller.store = _FakeStore(
        [make_device("a", "客厅热水器", on=True), make_device("b", "台灯", on=False)]
    )

    window = MainWindow(controller, start_hidden=True)
    window._save_settings = lambda: None
    window._settings["tiles_devices"] = ["b"]
    window._sync_board_devices()
    check("自选后只放挑中的设备", window.tile_board.count == 1)
    check("挑中的确实是那台", "b" in window.tile_board._tiles)

    window._settings.pop("tiles_devices", None)
    window._sync_board_devices()
    check("清空自选后恢复默认", window.tile_board.count == 2)

    window.tile_board.close_all()
    window.close()


def test_auto_pos(app: QApplication) -> None:
    """没存过位置的设备要自动排在屏幕内，不能堆到屏幕外。"""
    board = TileBoard()
    board.set_devices([make_device(str(i), f"设备{i}", on=True) for i in range(6)])
    area = _screen_area()
    positions = [(t.x(), t.y()) for t in board._tiles.values()]
    inside = all(
        area.left() <= x <= area.right() and area.top() <= y <= area.bottom()
        for x, y in positions
    )
    check("自动排布全部落在屏幕内", inside)
    check("自动排布不重叠", len(set(positions)) == len(positions))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")

    test_build(app)
    test_reuse(app)
    test_show_hide(app)
    test_hide_one(app)
    test_position(app)
    test_drag(app)
    test_signals(app)
    test_window_behaviour(app)
    test_card_controls(app)
    test_glow(app)
    test_wallpaper(app)
    test_sizes(app)
    test_set_options(app)
    test_snap(app)
    test_auto_pos(app)
    test_selection(app)
    test_integration(app)
    test_close(app)

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
