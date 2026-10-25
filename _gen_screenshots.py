"""重新生成 docs/screenshots 下的全部截图。

**只用假数据**（build_fake_snapshot + 本文件的 demo 补充），账号一律用打码串
（138****0000）。截图绝不能从真实登录态截取：那样会把家庭设备清单、房间结构、
场景名（往往就是账号昵称）一起带出去，而且一旦提交进 git 就很难彻底清除。

用法：python _gen_screenshots.py dark|light
"""

from __future__ import annotations

import os
import sys

# _smoke_test 在 import 时会 setdefault("QT_QPA_PLATFORM", "offscreen")，
# 离屏平台没有系统字体，中文会变成方块。必须抢在它之前钉死平台。
os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import dispatch  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.theme import set_theme  # noqa: E402
from huawei_smartlife.models import (  # noqa: E402
    Device,
    DiscoverySnapshot,
    ServiceState,
)
from _smoke_test import build_fake_snapshot  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "screenshots")
# 截图里出现的账号一律是打码串，别改成真实号码
DEMO_ACCOUNT = "138****0000"


def _switch(on: int = 0) -> dict:
    return {"switch": ServiceState(sid="switch", data={"on": on})}


def demo_snapshot() -> DiscoverySnapshot:
    """在自检用的 5 台设备之外再补 5 台，让看板截图不至于只有一行。

    全部是虚构的通用设备（扫地机器人、台灯……），
    不涉及任何真实家庭。自检那份数据不动，保持 5 台。
    """
    base = build_fake_snapshot()
    extra = (
        Device(
            home_id="h1",
            dev_id="demo-robot",
            name="扫地机器人",
            room_name="客厅",
            online=True,
            model="RB-200",
            services={
                **_switch(1),
                "battery": ServiceState(sid="battery", data={"battery": 76}),
            },
        ),
        Device(
            home_id="h1",
            dev_id="demo-lamp",
            name="书桌台灯",
            room_name="书房",
            online=True,
            model="LD-30",
            services={
                **_switch(1),
                "brightness": ServiceState(
                    sid="brightness", data={"brightness": 45, "min": 1, "max": 100}
                ),
            },
        ),
        Device(
            home_id="h1",
            dev_id="demo-purifier",
            name="空气净化器",
            room_name="客厅",
            online=True,
            model="AP-50",
            services={
                **_switch(1),
                # sid / 键名都取 device_spec 词表里已有中文的词，
                # 用生造的名字会显示成英文（Air Purifier / Filter Left）
                "purifier": ServiceState(
                    sid="purifier",
                    data={
                        "mode": 2,
                        "speed": 3,
                        "temperature": 24,
                        "humidity": 46,
                        "level": 1,
                        "battery": 82,
                        "voltage": 220,
                        "rssi": -52,
                    },
                ),
            },
        ),
        Device(
            home_id="h2",
            dev_id="demo-tv",
            name="智慧屏",
            room_name="客厅",
            online=True,
            model="TV-65",
            services=_switch(0),
        ),
        Device(
            home_id="h2",
            dev_id="demo-lock",
            name="智能门锁",
            room_name="玄关",
            online=False,
            model="LK-10",
            services={
                "battery": ServiceState(sid="battery", data={"battery": 91}),
            },
        ),
    )
    return DiscoverySnapshot(homes=base.homes, devices=base.devices + extra)


def snap(widget, name: str, pad: int = 0, bg: str = "#c9d3dc"):
    """保存控件截图；pad > 0 时贴到纯色背景上（无边框窗口是透明的）。"""
    shot = widget.grab().toImage()
    if pad:
        canvas = QImage(shot.width() + pad * 2, shot.height() + pad * 2, QImage.Format_ARGB32)
        canvas.fill(QColor(bg))
        painter = QPainter(canvas)
        painter.drawImage(pad, pad, shot)
        painter.end()
        canvas.save(os.path.join(OUT, name))
    else:
        shot.save(os.path.join(OUT, name))
    print("saved", name, flush=True)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dark"
    accent = "华为红"

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    set_theme(mode, accent)
    dispatch.attach()

    controller = AppController()
    controller.restore_session = lambda: False
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None

    window = MainWindow(controller)
    # 产品里最小尺寸是 1080x700（避免缩到布局散架）。截图时按内容临时放开，
    # 否则一页只有两行卡片的看板会留一大半空白。
    window.setMinimumSize(0, 0)
    window.resize(1120, 700)
    window.show()
    QTest.qWait(1200)

    if mode == "dark":
        # 登录页用完整高度截：收紧到 430 会把底部免责说明裁掉
        snap(window, "login.png")
        window.resize(1120, 430)
        QTest.qWait(400)

    # 假数据：10 台设备、2 个家庭（我的家 / 父母家）
    controller.store.replace_snapshot(demo_snapshot())
    window._on_login_succeeded(DEMO_ACCOUNT)
    window.dashboard.rebuild_cards()
    window.dashboard.reload_homes()
    QTest.qWait(700)
    snap(window, f"main-{mode}.png")

    if mode != "dark":
        window._quit_app()
        return 0

    devices = list(controller.store.all())

    # ---- 设备详情：挑净化器（开关 + 5 个属性，界面最完整）----
    target = next(item for item in devices if item.dev_id == "demo-purifier")
    window._open_device(target.dev_id)
    QTest.qWait(600)
    dialog = window._device_dialog
    if dialog is not None:
        snap(dialog, "device-dialog.png", pad=18)
        dialog.close()

    # ---- 自选设备与排序 ----
    from app.dialogs.device_picker import DevicePickerDialog

    picker = DevicePickerDialog(
        "托盘项目 · 自选与排序",
        devices,
        ["dev-light", "dev-heater", "dev-socket"],
        max_count=12,
        parent=window,
    )
    picker.show()
    QTest.qWait(500)
    snap(picker, "device-picker.png", pad=18)
    picker.close()

    # ---- 设置 ----
    from app.dialogs.settings_dialog import SettingsDialog

    settings = SettingsDialog(
        DEMO_ACCOUNT,
        families=[("h1", "我的家"), ("h2", "父母家")],
        startup_view="all",
        parent=window,
    )
    settings.show()
    QTest.qWait(500)
    snap(settings, "settings.png", pad=18)
    settings.close()

    # ---- 托盘面板：双列 / 单列 ----
    window._sync_tray_devices()
    panel = window.tray_panel
    panel._layout_mode = "grid"
    panel.set_devices(devices)
    panel.show()
    QTest.qWait(400)
    snap(panel, "tray-panel.png", pad=26)
    panel._toggle_layout()
    QTest.qWait(400)
    snap(panel, "tray-panel-list.png", pad=26)
    panel.hide()

    # ---- 桌面挂件 ----
    window._show_board()
    QTest.qWait(600)
    snap(window.board, "desktop-board.png", pad=26)
    window.board.hide()

    window._quit_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
