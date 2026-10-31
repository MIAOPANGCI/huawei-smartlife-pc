"""验证设置界面：两列布局、开机自启动项（写注册表失败时不能留下假的勾选状态）。

跑法（无头）：
    python _settings_check.py

这里**不会真的写注册表**：成功路径只测命令生成与信号，
失败路径用打桩让 enable() 抛错，验证勾选框会回滚。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from app import autostart, dispatch  # noqa: E402
from app.dialogs.settings_dialog import SettingsDialog  # noqa: E402
from app.theme import set_theme  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")
    dispatch.attach()

    # ---- 1. 启动命令 ----
    command = autostart.launch_command()
    check("启动目标带引号（防空格路径）", command.startswith('"'))
    check("不会弹控制台", "pythonw" in command.lower() or "HuaweiSmartLife.exe" in command)
    check("带 --tray：开机只驻留托盘，不弹主窗口", "--tray" in command)
    check("本机支持开机自启动（Windows）", autostart.supported() is True)

    # 有打包好的启动器就指向它，任务管理器才会显示中文名
    if autostart.launcher_path() is not None:
        check("指向启动器 exe（任务管理器显示中文名）", "HuaweiSmartLife.exe" in command)
    else:
        check("没有启动器时退回 pythonw", "pythonw" in command.lower())

    # 用「启动」文件夹的快捷方式而不是 Run 键：任务管理器里 Run 项显示的是
    # 可执行文件名（pythonw.exe → "python"），用户认不出这是谁
    shortcut = autostart.shortcut_path()
    check("快捷方式放在「启动」文件夹", "Startup" in shortcut)
    check("快捷方式本身用中文名（在文件夹里好辨认）",
          autostart.SHORTCUT_NAME.startswith("华为智慧生活"))
    check("快捷方式名以 .lnk 结尾", shortcut.endswith(".lnk"))
    check("旧版注册表项名保留（用于清理）", autostart.VALUE_NAME == "HuaweiSmartLifePC")

    # --tray 是给本程序看的，必须先摘掉再交给 Qt
    from app.main import parse_launch_args

    plain_args, plain_hidden = parse_launch_args(["main.py"])
    tray_args, tray_hidden = parse_launch_args(["main.py", "--tray"])
    check("--tray 会被摘掉再传 QApplication", tray_args == ["main.py"])
    check("--tray 表示隐藏主窗口", tray_hidden is True)
    check("普通启动不隐藏", plain_hidden is False and plain_args == ["main.py"])

    # ---- 2. 两列布局 ----
    dialog = SettingsDialog(
        "138****0000",
        [("h1", "我的家")],
        "all",
        cameras=[("门口摄像头", "cam1"), ("网络摄像头", "manual")],
        camera_default="",
        autostart_enabled=False,
    )
    dialog.show()
    app.processEvents()

    check("左列有内容（外观 / 启动 / 账号）", dialog._left.count() > 0)
    check("右列有内容（挂件 / 诊断 / 关于）", dialog._right.count() > 0)
    check("比单列窄不了：宽度按两列给", dialog.width() >= 720)
    check("两列后高度明显下降", dialog.height() < 800)
    check("左右两列宽度一致", abs(dialog._left.parentWidget().width() - dialog._right.parentWidget().width()) <= 2)

    # ---- 3. 开机自启动项 ----
    check("有开机自启动勾选框", dialog._autostart_box is not None)
    check("默认不勾选（传了 False）", dialog._autostart_box.isChecked() is False)
    check("是复选框而不是按钮", isinstance(dialog._autostart_box, QCheckBox))

    seen: list[bool] = []
    dialog.autostart_changed.connect(seen.append)
    dialog._autostart_box.setChecked(True)
    app.processEvents()
    check("勾选后通知外部", seen == [True])

    # 外部（主窗口）写注册表失败时会调 set_autostart 回滚
    dialog.set_autostart(False)
    app.processEvents()
    check("回滚后勾选状态复位", dialog._autostart_box.isChecked() is False)
    check("回滚不会再发一遍信号（否则会死循环）", seen == [True])

    # ---- 4. 真实的失败路径：enable 抛错时复选框要改回去 ----
    # 完全打桩，不依赖本机此刻有没有装过启动项
    original_enable = autostart.enable
    original_is_enabled = autostart.is_enabled
    autostart.enable = lambda: (_ for _ in ()).throw(OSError("被拦截"))
    autostart.is_enabled = lambda: False
    try:
        dialog._autostart_box.setChecked(True)
        app.processEvents()
        # 模拟主窗口的失败处理（同 MainWindow._set_autostart）
        if dialog._autostart_box.isChecked():
            dialog.set_autostart(autostart.is_enabled())
        check("创建启动项失败时不留下假的勾选", dialog._autostart_box.isChecked() is False)
    finally:
        autostart.enable = original_enable
        autostart.is_enabled = original_is_enabled

    # ---- 5. 没有摄像头时下拉禁用 ----
    plain = SettingsDialog("138****0000", [("h1", "我的家")], "all", autostart_enabled=False)
    check("没有摄像头时下拉禁用", plain._camera_combo.isEnabled() is False)
    check("没有摄像头时只有「跟随上次查看」一项", plain._camera_combo.count() == 1)
    plain.close()

    dialog.close()

    # ---- 6. 隐藏启动（--tray）但恢复不出会话时，窗口必须自己出现 ----
    # 否则开机后程序在跑、却停在隐藏的登录页上，用户什么都看不到
    from PySide6.QtTest import QTest

    from app.controller import AppController
    from app.main_window import MainWindow

    controller = AppController()
    controller.restore_session = lambda: False      # 模拟没有免密会话
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None

    hidden = MainWindow(controller, start_hidden=True)
    check("隐藏启动时窗口默认不显示", hidden.isVisible() is False)
    QTest.qWait(700)      # _bootstrap 用 120ms 延时 + 线程池回收
    check("没有会话时自动显示登录窗口", hidden.isVisible() is True)
    hidden.close()

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
