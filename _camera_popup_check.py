"""验证桌面挂件的快捷实时画面：入口显隐、目标排序、浮层定位、连接与释放。

跑法（无头，不联网）：
    python _camera_popup_check.py

端口探测被替换成一概不通，所以这里不会真的去连任何摄像头；
要验证真实画面得在本机配好一台再手动点开。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.camera_popup as camera_popup  # noqa: E402
from app import dispatch  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.theme import set_theme  # noqa: E402
from huawei_smartlife.models import (  # noqa: E402
    Device,
    DiscoverySnapshot,
    Home,
    ServiceState,
)

# 不发真包：测试环境里 192.0.2.x 本来也不可达
camera_popup.port_open = lambda host, port, timeout=1.2: False  # type: ignore[assignment]

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def build_snapshot() -> DiscoverySnapshot:
    return DiscoverySnapshot(
        homes=(Home(home_id="h1", name="我的家"),),
        devices=(
            Device(
                home_id="h1",
                dev_id="dev-light",
                name="客厅灯",
                room_name="客厅",
                online=True,
                model="HL-100",
                services={"switch": ServiceState(sid="switch", data={"on": 1})},
            ),
            Device(
                home_id="h1",
                dev_id="dev-cam-door",
                name="门口摄像头",
                room_name="玄关",
                online=True,
                model="小豚当家室外摄像头3K版",
            ),
            Device(
                home_id="h1",
                dev_id="dev-cam-yard",
                name="院子摄像头",
                room_name="院子",
                online=True,
                model="小豚当家室外摄像头5 4K版",
            ),
        ),
    )


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")
    dispatch.attach()

    controller = AppController()
    controller.restore_session = lambda: False
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None
    controller.store.replace_snapshot(build_snapshot())

    window = MainWindow(controller)
    window.resize(1280, 860)
    window.show()
    app.processEvents()

    # 不碰本机真实配置（ui_settings.json 里可能存着用户自己的摄像头）
    window._settings.pop("cameras", None)
    window._settings.pop("board_devices", None)
    window._settings.pop("camera_default", None)
    window._settings.pop("board_camera", None)
    # 后面会走到 _save_settings（记忆 / 设置项），测试一律不落盘
    window._save_settings = lambda: None
    window._sync_board_devices()
    app.processEvents()

    # ---- 1. 入口显隐 ----
    check("有摄像头设备时挂件显示相机按钮", window.board._camera_btn.isVisibleTo(window.board))

    window.controller.store.remove_device = None   # 别让人误用
    targets = window._camera_targets()
    check("两台摄像头都被识别", len(targets) == 2)
    check("未配置时也能列出（点开即引导配置）", all(not t["config"] for t in targets))

    # ---- 2. 已配置的排在前面 ----
    window._settings["cameras"] = {
        "dev-cam-yard": {"host": "192.0.2.50", "port": 554, "username": "u",
                         "password": "p", "stream": "sub", "paths": {}}
    }
    targets = window._camera_targets()
    check("已填地址的摄像头排第一", targets[0]["key"] == "dev-cam-yard")

    # ---- 3. 手动添加的摄像头只在填过地址后才出现 ----
    window._settings["cameras"]["manual"] = {"host": "", "port": 554}
    check("空地址的手动摄像头不进列表", len(window._camera_targets()) == 2)
    window._settings["cameras"]["manual"] = {"host": "192.0.2.60", "port": 554,
                                             "username": "u", "password": "p",
                                             "stream": "sub", "paths": {}}
    check("填过地址的手动摄像头进列表", len(window._camera_targets()) == 3)
    check(
        "已配置的华为设备排在手动接入前面",
        window._camera_targets()[0]["key"] == "dev-cam-yard",
    )

    # ---- 4. 打开浮层 ----
    window.board.move(200, 300)
    window._toggle_camera_popup()
    app.processEvents()
    popup = window._camera_popup
    check("点相机按钮弹出浮层", popup is not None and popup.isVisible())
    check("浮层贴着挂件弹出", popup.x() != 0 or popup.y() != 0)
    check("画面浮层开着时挂件不自动缩回", window.board._edge_paused is True)

    # ---- 5. 未配置的目标给引导，不启动读帧线程 ----
    window._settings["cameras"].pop("dev-cam-yard", None)
    popup.set_targets(window._camera_targets())
    # 显式挑那台没配的：浮层会停在原来的目标上，不会自动跳到未配置的
    for index, target in enumerate(popup._targets):
        if target["key"] == "dev-cam-yard":
            popup._index = index
            break
    popup.start()
    app.processEvents()
    check("未配置时给配置引导", "还没填地址" in popup._view._hint)
    check("未配置时不启动读帧", popup._grabber is None)
    check("未配置时不显示重试", not popup._retry.isVisible())

    # ---- 6. 配置好但端口不通：给出准确结论和重试入口 ----
    window._settings["cameras"] = {
        "dev-cam-yard": {"host": "192.0.2.50", "port": 554, "username": "u",
                         "password": "p", "stream": "sub", "paths": {}}
    }
    popup.set_targets(window._camera_targets())
    popup.start()
    QTest.qWait(2500)          # 等端口探测线程回来
    check("端口不通时提示检查 IP 与局域网", "端口不通" in popup._status.text())
    check("端口不通时露出重试", popup._retry.isVisible())
    check("探测失败后不留读帧线程", popup._grabber is None)

    # ---- 7. 多台之间切换 ----
    before = popup.current()["key"]
    popup._step(1)
    app.processEvents()
    check("可以切到下一台", popup.current()["key"] != before)
    check("切换后标题跟着变", popup.current()["name"] in popup._name_label.text())
    popup._step(-1)
    check("能切回上一台", popup.current()["key"] == before)

    # ---- 8. 配置改了就重连（不会还拿着旧地址）----
    popup.reload_config("dev-cam-yard", {"host": "192.0.2.77", "port": 554,
                                         "username": "u", "password": "p",
                                         "stream": "main", "paths": {}})
    check("保存配置后目标跟着更新",
          any(t["config"].get("host") == "192.0.2.77" for t in popup._targets))

    # ---- 9. 关闭后释放 ----
    popup.hide()
    app.processEvents()
    check("关闭浮层后挂件恢复自动缩回", window.board._edge_paused is False)
    check("关闭浮层后没有残留的读帧线程", popup._grabber is None)

    # ---- 9.5 默认看哪台：设置在先，记忆在后 ----
    from app.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(
        "138****0000",
        [],
        cameras=[(t["name"], str(t["key"])) for t in window._camera_targets()],
        camera_default="",
    )
    check("设置下拉含「跟随上次查看」+ 每台摄像头",
          dialog._camera_combo.count() == len(window._camera_targets()) + 1)
    check("默认选中「跟随上次查看」", dialog._camera_combo.currentIndex() == 0)

    emitted: list[str] = []
    dialog.camera_default_changed.connect(emitted.append)
    index = dialog._camera_combo.findData("dev-cam-door")
    dialog._camera_combo.setCurrentIndex(index)
    check("选中某台会通知主窗口", emitted == ["dev-cam-door"])

    # 设置里点明固定某台 → 打开浮层就是那台，跟记忆无关
    window._settings["board_camera"] = "dev-cam-yard"
    window._set_camera_default("dev-cam-door")
    check("设置优先于上次查看", window._camera_last_key() == "dev-cam-door")
    window._toggle_camera_popup()
    app.processEvents()
    check("浮层按设置打开指定那台", window._camera_popup.current()["key"] == "dev-cam-door")
    window._camera_popup.hide()

    # 切回「跟随上次查看」→ 用记忆
    window._set_camera_default("")
    check("清空设置后回到跟随上次", window._camera_last_key() == "dev-cam-yard")
    window._toggle_camera_popup()
    app.processEvents()
    check("浮层跟着上次看的那台", window._camera_popup.current()["key"] == "dev-cam-yard")

    # 用户切台后立刻记住，下次还是它
    window._camera_popup._step(1)
    app.processEvents()
    switched = window._camera_popup.current()["key"]
    check("切台后写入记忆", window._settings.get("board_camera") == switched)
    window._camera_popup.hide()
    window._toggle_camera_popup()
    app.processEvents()
    check("重开浮层停在刚切的那台", window._camera_popup.current()["key"] == switched)
    window._camera_popup.hide()

    # 设置里指定的那台被删掉时不能白屏：退回第一台
    window._settings["camera_default"] = "已删除的摄像头"
    window._toggle_camera_popup()
    app.processEvents()
    check("指定的摄像头不存在时退回第一台",
          window._camera_popup.current()["key"] == window._camera_popup._targets[0]["key"])
    window._camera_popup.hide()
    window._settings.pop("camera_default", None)

    # ---- 10. 浮层始终在屏幕内 ----
    area = popup._screen_area() if hasattr(popup, "_screen_area") else None
    from PySide6.QtGui import QGuiApplication

    area = (QGuiApplication.primaryScreen().availableGeometry())
    popup.show_for(window.board.frameGeometry())
    app.processEvents()
    check(
        "浮层不越出屏幕",
        area.left() <= popup.x() and popup.x() + popup.width() <= area.right() + 1,
    )
    check(
        "浮层不越出屏幕上下",
        area.top() <= popup.y() and popup.y() + popup.height() <= area.bottom() + 1,
    )
    popup.hide()

    # ---- 11. 收起挂件时，挂在它上面的画面一起收 ----
    window.board.show()
    popup.show_for(window.board.frameGeometry())
    app.processEvents()
    check("浮层能重新打开", popup.isVisible())
    window.board.hide()
    app.processEvents()
    check("收起挂件时画面一起消失", not popup.isVisible())
    check("画面消失后挂件恢复自动缩回", window.board._edge_paused is False)

    # ---- 12. 一台摄像头都没有时藏起入口 ----
    window.controller.store.replace_snapshot(
        DiscoverySnapshot(
            homes=(Home(home_id="h1", name="我的家"),),
            devices=(
                Device(
                    home_id="h1",
                    dev_id="dev-light",
                    name="客厅灯",
                    online=True,
                    model="HL-100",
                    services={"switch": ServiceState(sid="switch", data={"on": 1})},
                ),
            ),
        )
    )
    window._settings.pop("cameras", None)
    window._sync_board_devices()
    app.processEvents()
    check("没有摄像头时藏起相机按钮", not window.board._camera_btn.isVisibleTo(window.board))

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
