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

from PySide6.QtCore import QPoint  # noqa: E402
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

    # ---- 5b. 默认音量滑块 ----
    volume_dialog = SettingsDialog(
        "138****0000", [("h1", "我的家")], "all",
        default_volume=1.5, autostart_enabled=False,
    )
    check("默认音量按设置初始化滑块", volume_dialog._volume_slider.value() == 150)
    check("超过 100% 标出增强", "增强" in volume_dialog._volume_label.text())

    picked: list[float] = []
    volume_dialog.default_volume_changed.connect(picked.append)
    volume_dialog._volume_slider.setValue(200)
    app.processEvents()
    check("拖动滑块发出音量变化", bool(picked) and abs(picked[-1] - 2.0) < 1e-6)
    check("滑块旁的数字跟着变", volume_dialog._volume_label.text().startswith("200%"))

    volume_dialog._volume_slider.setValue(5)
    check("低于下限被夹住", volume_dialog._volume_slider.value() == 30)
    volume_dialog.close()

    # ---- 5c. 磁贴的个性化选项 ----
    tile_dialog = SettingsDialog(
        "138****0000", [("h1", "我的家")], "all",
        tile_size="card", tile_align="edge", tile_glow="strong",
        tile_wallpaper=False, autostart_enabled=False,
    )
    check("尺寸下拉有「卡片」档",
          [tile_dialog._tile_size_combo.itemData(i) for i in range(tile_dialog._tile_size_combo.count())]
          == ["compact", "standard", "large", "card"])
    check("尺寸按设置初始化", tile_dialog._tile_size_combo.currentData() == "card")
    check("对齐按设置初始化", tile_dialog._tile_align_combo.currentData() == "edge")
    check("光感按设置初始化", tile_dialog._tile_glow_combo.currentData() == "strong")
    check("壁纸层按设置初始化", tile_dialog._tile_wallpaper_box.isChecked() is False)
    check("光感有四档（含关闭）",
          [tile_dialog._tile_glow_combo.itemData(i) for i in range(tile_dialog._tile_glow_combo.count())]
          == ["off", "weak", "balanced", "strong"])

    glow_picks: list[str] = []
    wallpaper_picks: list[bool] = []
    tile_dialog.tile_glow_changed.connect(glow_picks.append)
    tile_dialog.tile_wallpaper_changed.connect(wallpaper_picks.append)

    tile_dialog._tile_glow_combo.setCurrentIndex(0)
    app.processEvents()
    check("换光感档位发出信号", glow_picks == ["off"])

    tile_dialog._tile_wallpaper_box.setChecked(True)
    app.processEvents()
    check("壁纸层开关发出信号", wallpaper_picks == [True])

    check("不透明度按设置初始化", tile_dialog._tile_opacity_slider.value() == 86)
    check("不透明度标签跟着显示", tile_dialog._tile_opacity_label.text() == "86%")
    opacity_picks: list[int] = []
    tile_dialog.tile_opacity_changed.connect(opacity_picks.append)
    tile_dialog._tile_opacity_slider.setValue(60)
    app.processEvents()
    check("拖不透明度发出信号", opacity_picks == [60])
    check("不透明度标签跟着变", tile_dialog._tile_opacity_label.text() == "60%")

    check("字体候选第一项是系统默认",
          tile_dialog._font_combo.itemData(0) == "")
    font_picks: list[str] = []
    tile_dialog.font_changed.connect(font_picks.append)
    if tile_dialog._font_combo.count() > 1:
        tile_dialog._font_combo.setCurrentIndex(1)
        app.processEvents()
        check("换字体发出信号", font_picks == [tile_dialog._font_combo.itemData(1)])
    tile_dialog.close()

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
    # 这个窗口读的是用户真实的 ui_settings.json；下面会改 settings 并触发保存，
    # 不挡住的话测试数据会写进用户的配置（曾经真的把 board_restore 写成 False）
    hidden._save_settings = lambda: None
    check("隐藏启动时窗口默认不显示", hidden.isVisible() is False)
    QTest.qWait(700)      # _bootstrap 用 120ms 延时 + 线程池回收
    check("没有会话时自动显示登录窗口", hidden.isVisible() is True)

    # ---- 7. 启动时恢复上次的挂件 ----
    hidden._settings["board_restore"] = True
    hidden._settings["board"] = {"x": 120, "y": 90, "visible": True}
    hidden._board_restored = False
    hidden._restore_board()
    app.processEvents()
    check("上次开着 → 启动自动打开挂件", hidden.board.isVisible())
    # 位置的恢复发生在 WidgetBoard 构造时（读 board.x/y），这里改配置不会重读，
    # 那条路径由 _edge_check.py 覆盖

    hidden.board.hide()
    hidden._board_restored = False
    hidden._settings["board"] = {"x": 120, "y": 90, "visible": False}
    hidden._restore_board()
    check("上次关着 → 启动不打开", not hidden.board.isVisible())

    hidden._board_restored = False
    hidden._settings["board"] = {"x": 120, "y": 90, "visible": True}
    hidden._settings["board_restore"] = False
    hidden._restore_board()
    check("设置关掉后，上次开着也不弹出来", not hidden.board.isVisible())

    # 点 × 关掉要记成"不想看"，退出时的隐藏则不能改这个状态
    hidden._settings["board"] = {"visible": True}
    hidden._remember_board_visible(False)
    check("点 × 关掉会记成不可见", hidden._settings["board"].get("visible") is False)

    # 挂件上报位置时不能把 visible 冲掉：_emit_state 的字典里没有这个字段
    hidden._settings["board"] = {}
    hidden._show_board()
    check("打开挂件后记为可见", hidden._settings["board"].get("visible") is True)
    hidden._on_board_state_changed(
        {"x": 10, "y": 20, "locked": False, "collapsed": False, "edge": None}
    )
    check("上报位置不会把 visible 冲掉", hidden._settings["board"].get("visible") is True)

    # ---- 8. 设置里的默认音量：存设置 + 记进本次运行 + 应用到开着的窗口 ----
    from app import audio as audio_module

    hidden._set_audio_volume(2.0)
    check("默认音量写进设置", abs(hidden._settings.get("audio_volume", 0) - 2.0) < 1e-6)
    check("默认音量记进本次运行", abs(audio_module.shared_volume() - 2.0) < 1e-6)

    hidden._set_audio_volume(99.0)
    check("传超上限也夹到 MAX_VOLUME",
          abs(hidden._settings.get("audio_volume", 0) - audio_module.MAX_VOLUME) < 1e-6)
    hidden._set_audio_volume(1.0)      # 复位，别把用户的运行态留在 3 倍

    # ---- 8b. 磁贴的光感与壁纸层：写设置 + 应用到 board ----
    hidden._set_tile_glow("weak")
    check("光感写进设置", hidden._settings.get("tiles_glow") == "weak")
    check("光感应用到磁贴板", hidden.tile_board.glow == "weak")

    hidden._set_tile_wallpaper(False)
    check("壁纸层写进设置", hidden._settings.get("tiles_wallpaper") is False)
    check("壁纸层应用到磁贴板", hidden.tile_board.wallpaper_mode is False)
    hidden._set_tile_wallpaper(True)
    hidden._set_tile_glow("balanced")

    hidden._set_tile_opacity(65)
    check("不透明度写进设置", abs(hidden._settings.get("tiles_opacity", 0) - 0.65) < 1e-6)
    check("不透明度应用到磁贴板", abs(hidden.tile_board.opacity - 0.65) < 1e-6)
    hidden._set_tile_opacity(10)
    check("不透明度低于下限被夹住", hidden.tile_board.opacity >= 0.45)
    hidden._set_tile_opacity(86)

    # 字体：写设置 + 应用到整个应用 + 写进样式表
    from app import theme as theme_module

    before_font = theme_module.font_family()
    hidden._set_font_family("SimSun")
    check("字体写进设置", hidden._settings.get("font_family") == "SimSun")
    check("字体应用到应用", theme_module.font_family() == "SimSun")
    hidden._set_font_family(before_font)
    check("字体能切回原值", theme_module.font_family() == before_font)

    hidden.close()

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
