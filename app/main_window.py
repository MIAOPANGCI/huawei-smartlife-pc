"""主窗口：无边框外壳 + 页面切换 + 信号编排。

这里是把「控制器（业务）」与「页面（显示）」连起来的唯一地方：
控制器发信号 → 主窗口更新 UI；UI 发信号 → 主窗口调用控制器。
页面之间不互相引用，便于单独替换或测试。
"""

from __future__ import annotations

import json
import logging
import os

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QSizeGrip,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .controller import APP_DIR, UI_SETTINGS_PATH, AppController, friendly_error
from . import device_spec
from .dialogs.camera_dialog import CameraDialog
from .dialogs.device_dialog import DeviceDialog
from .dialogs.device_picker import DevicePickerDialog
from .dialogs.settings_dialog import SettingsDialog
from .tray import TrayController
from .tray_panel import TrayPanel
from .widget_board import WidgetBoard
from .widgets.glyph_icons import make_app_icon
from .pages.dashboard_page import DashboardPage
from .pages.login_page import LoginPage
from .theme import build_qss, theme
from .widgets import motion
from .widgets.title_bar import TitleBar
from .widgets.toast import ToastHost
from .workers import run_async

_LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self._device_dialog: DeviceDialog | None = None
        # 每台摄像头一个独立窗口：客厅和门口可以同时看，不互相顶掉
        self._camera_dialogs: dict[str, CameraDialog] = {}
        self._settings: dict = self._load_settings()
        self._startup_pending = False
        self._quit_requested = False

        self.setWindowTitle("华为智慧生活 · PC 控制台")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        # 无边框窗口要自绘圆角，必须先让窗口表面支持透明（否则四角是方的）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(1080, 700)
        self.resize(1280, 800)

        self._build()
        self._init_widgets()
        self._connect_controller()
        self._apply_theme()

        # 协议调试订阅默认关闭；注入一次，之后由设置对话框切换。
        # 放在信号连接之后，那条「已开启」提示才有人显示。
        # reconnect=False：此刻还没连实时通道，等登录后连接时自然带上。
        self.controller.set_capture_enabled(
            bool(self._settings.get("capture_telemetry")), reconnect=False
        )
        self.controller.set_raw_protocol(bool(self._settings.get("raw_protocol")))
        # 界面动画总开关，必须在任何过渡发生之前注入
        motion.set_enabled(bool(self._settings.get("motion", True)))

        # 启动后异步尝试恢复会话，避免卡住首屏
        QTimer.singleShot(120, self._bootstrap)

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("RootSurface")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_bar = TitleBar(self)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximize)
        self.title_bar.close_requested.connect(self.close)
        self.title_bar.theme_toggle_requested.connect(self._toggle_theme)
        self.title_bar.settings_requested.connect(self._open_settings)
        layout.addWidget(self.title_bar)

        self._stack = QStackedWidget()
        self.login_page = LoginPage()
        self.dashboard = DashboardPage(self.controller.store)
        self._stack.addWidget(self.login_page)
        self._stack.addWidget(self.dashboard)
        layout.addWidget(self._stack, 1)

        self.setCentralWidget(root)

        # 右下角缩放手柄（无边框窗口需要显式提供）
        self._grip = QSizeGrip(root)
        self._grip.setFixedSize(16, 16)
        self._grip.setStyleSheet("background: transparent;")

        self.toast = ToastHost(root)

        # 登录页交互
        self.login_page.login_requested.connect(self._on_login_requested)
        self.login_page.code_submitted.connect(self.controller.submit_challenge)
        self.login_page.channel_requested.connect(self.controller.select_challenge_channel)
        self.login_page.channel_switched.connect(self.controller.switch_challenge_channel)

        # 看板交互
        self.dashboard.refresh_requested.connect(self.controller.refresh)
        self.dashboard.mqtt_toggle_requested.connect(self._toggle_mqtt)
        self.dashboard.device_open_requested.connect(self._open_device)
        self.dashboard.device_power_requested.connect(self._on_power_requested)
        self.dashboard.settings_requested.connect(self._open_settings)
        self.dashboard.manual_camera_requested.connect(self._open_manual_camera)
        self.dashboard.scenes_requested.connect(self.controller.load_scenes)
        self.dashboard.scene_execute_requested.connect(self.controller.execute_scene)
        self.dashboard.logout_requested.connect(self._confirm_logout)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        if hasattr(self, "_grip"):
            self._grip.move(self.width() - 16, self.height() - 16)
        if hasattr(self, "toast"):
            self.toast._reposition()

    # ==================================================================
    # 托盘 / 托盘面板 / 桌面挂件
    # ==================================================================
    def _init_widgets(self) -> None:
        tray_layout = self._settings.get("tray_layout", "grid")
        board_state = self._settings.get("board", {})
        if not isinstance(board_state, dict):
            board_state = {}

        self.tray = TrayController(make_app_icon(theme().accent_color), self)
        # 面板与挂件都是独立顶层窗口（parent=None，见各自构造函数的说明），
        # 由主窗口持有引用，避免主窗口最小化时被连带隐藏。
        self.tray_panel = TrayPanel(tray_layout)
        self.board = WidgetBoard(board_state)

        self.tray.open_panel.connect(self._show_tray_panel)
        self.tray.open_main.connect(self._show_main_window)
        self.tray.open_board.connect(self._show_board)
        self.tray.quit_requested.connect(self._quit_app)

        self.tray_panel.power_requested.connect(self._on_power_requested)
        self.tray_panel.open_main.connect(self._show_main_window)
        self.tray_panel.layout_changed.connect(self._on_tray_layout_changed)
        self.tray_panel.devices_edit_requested.connect(self._edit_tray_devices)

        self.board.power_requested.connect(self._on_power_requested)
        self.board.state_changed.connect(self._on_board_state_changed)
        self.board.devices_edit_requested.connect(self._edit_board_devices)

        self.tray.show()

    # ---- 设备顺序：自选优先，未自选时按「在线优先 + 名称」自动排 ----
    def _default_order(self, require_switch: bool) -> list:
        devices = [
            device
            for device in self.controller.store.all()
            if not require_switch or device_spec.find_switch_service(device) is not None
        ]
        devices.sort(key=lambda d: (d.online is not True, d.name or ""))
        return devices

    def _ordered_devices(
        self, setting_key: str, require_switch: bool = False
    ) -> list:
        """按用户自选的 id 顺序取设备；列表为空则用默认规则。

        自选语义是"只显示我挑的这几个"，所以快照里新增的设备不会自动插进来，
        需要用户自己去选择对话框里加。
        """
        stored = self._settings.get(setting_key)
        if not isinstance(stored, list) or not stored:
            return self._default_order(require_switch)

        store = self.controller.store
        result = []
        for dev_id in stored:
            if not isinstance(dev_id, str):
                continue
            device = store.get(dev_id)
            if device is None:
                continue    # 设备已被删除 / 移出家庭
            if require_switch and device_spec.find_switch_service(device) is None:
                continue    # 后来失去开关能力的设备不硬塞进挂件
            result.append(device)
        return result or self._default_order(require_switch)

    def _sync_tray_devices(self) -> None:
        self.tray_panel.set_devices(self._ordered_devices("tray_devices"))

    def _sync_board_devices(self) -> None:
        self.board.set_devices(self._ordered_devices("board_devices", require_switch=True))

    def _show_tray_panel(self) -> None:
        from PySide6.QtGui import QCursor

        self._sync_tray_devices()
        self.tray_panel.popup_at(QCursor.pos())

    def _show_main_window(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_board(self) -> None:
        self._sync_board_devices()
        self.board.show_near()

    # ---- 自选设备与排序 ----
    def _edit_tray_devices(self) -> None:
        from .tray_panel import MAX_VISIBLE_DEVICES

        self._edit_device_selection(
            "tray_devices",
            "托盘项目 · 自选与排序",
            MAX_VISIBLE_DEVICES,
            require_switch=False,
            on_done=self._sync_tray_devices,
        )

    def _edit_board_devices(self) -> None:
        from .widget_board import MAX_CARDS

        self._edit_device_selection(
            "board_devices",
            "桌面挂件 · 自选与排序",
            MAX_CARDS,
            require_switch=True,
            on_done=self._sync_board_devices,
        )

    def _edit_device_selection(
        self,
        setting_key: str,
        title: str,
        max_count: int,
        require_switch: bool,
        on_done,
    ) -> None:
        all_devices = [
            device
            for device in self.controller.store.all()
            if not require_switch
            or device_spec.find_switch_service(device) is not None
        ]
        all_devices.sort(key=lambda d: (d.online is not True, d.name or ""))

        current = self._ordered_devices(setting_key, require_switch)
        selected = [device.dev_id for device in current][:max_count]

        # 对话框必须挂一个 parent（主窗口）：无 parent 的 QDialog 在 exec() 返回后
        # 没有任何引用兜底，PySide 析构 C++ 对象的时机和 Qt 内部状态（活动模态窗口 /
        # 焦点）对不上，这就是"点保存后 python has stopped working"的来源。
        # 定位不靠父窗口——exec 前手动搬到屏幕中央，所以主窗口最小化也不影响。
        dialog = DevicePickerDialog(title, all_devices, selected, max_count, self)
        dialog.adjustSize()
        self._center_dialog_on_screen(dialog)

        accepted = False
        try:
            accepted = dialog.exec() == DevicePickerDialog.Accepted
            picked = dialog.selected_ids() if accepted else []
        finally:
            # 显式交给事件循环销毁：即便 accept 之后还有 pending 的重绘 / 焦点事件，
            # 也不会访问已经析构的对象。
            dialog.hide()
            dialog.deleteLater()

        if not accepted:
            return

        if picked:
            self._settings[setting_key] = picked
            self._save_settings()
            on_done()
            self.toast.show_toast(f"已保存，共 {len(picked)} 个设备", "success")
        else:
            # 一个都不选等价于恢复默认：否则会留下一个空列表，
            # 用户下次打开面板看不到任何设备也不知道怎么改回来。
            self._settings.pop(setting_key, None)
            self._save_settings()
            on_done()
            self.toast.show_toast("已恢复默认排序", "info")

    @staticmethod
    def _center_dialog_on_screen(dialog) -> None:
        """把对话框摆到鼠标所在屏幕的中央（不依赖父窗口位置）。"""
        from PySide6.QtGui import QCursor

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        dialog.move(
            area.x() + max(0, (area.width() - dialog.width()) // 2),
            area.y() + max(0, (area.height() - dialog.height()) // 2),
        )

    def _on_tray_layout_changed(self, mode: str) -> None:
        self._settings["tray_layout"] = mode
        self._save_settings()

    def _on_board_state_changed(self, state: dict) -> None:
        self._settings["board"] = state
        self._save_settings()

    def _quit_app(self) -> None:
        self._quit_requested = True
        self.close()

    # ==================================================================
    # 控制器信号
    # ==================================================================
    def _connect_controller(self) -> None:
        ctrl = self.controller
        ctrl.login_started.connect(lambda: self.login_page.set_busy(True, "登录中…"))
        ctrl.login_succeeded.connect(self._on_login_succeeded)
        ctrl.login_failed.connect(self._on_login_failed)
        ctrl.challenge_required.connect(self.login_page.show_challenge)
        ctrl.logged_out.connect(self._on_logged_out)
        ctrl.snapshot_loaded.connect(self._on_snapshot_loaded)
        ctrl.devices_reloaded.connect(self._on_devices_reloaded)
        ctrl.device_changed.connect(self._on_device_changed)
        ctrl.scenes_loaded.connect(self.dashboard.show_scenes)
        ctrl.mqtt_state_changed.connect(self._on_mqtt_state)
        ctrl.busy_changed.connect(self._on_busy)
        ctrl.log.connect(self.dashboard.append_log)
        ctrl.notify.connect(self.toast.show_toast)

    # ---------- 登录 ----------
    def _on_login_requested(self, account: str, password: str, remember: bool) -> None:
        self._remember = remember
        # 重新点"登录"意味着上一轮挑战作废，把残留的验证码清掉免得误提交
        self.login_page.clear_challenge_input()
        self.controller.login(account, password, remember)

    def _on_login_succeeded(self, account: str) -> None:
        self.login_page.set_busy(False)
        self.login_page.clear_error()
        self._stack.setCurrentWidget(self.dashboard)
        # 登录→主界面跨页幅度最大，给一个稍慢的淡入，
        # 让"进来了"这件事有落点，而不是瞬间换一张脸
        motion.fade_in(self.dashboard, motion.SLOW)
        self.title_bar.set_controls_visible(True)
        self.title_bar.set_subtitle("已登录")
        self.dashboard.set_account(account)
        self.dashboard.set_title("全部设备", "正在加载…")
        self.title_bar.set_controls_visible(True)
        self._startup_pending = True
        self.controller.refresh()
        self.controller.connect_mqtt()

    def _on_login_failed(self, message: str) -> None:
        self.login_page.set_busy(False)
        self.login_page.show_error(message)
        # 验证码提交失败时把焦点放回输入框，省得用户再去点一下
        self.login_page.focus_code()

    def _on_logged_out(self, reason: str) -> None:
        self._close_device_dialog()
        self._stack.setCurrentWidget(self.login_page)
        self.login_page.reset()
        self.title_bar.set_subtitle("PC 控制台")
        self.title_bar.set_controls_visible(False)
        self.dashboard.set_mqtt_state(False)
        self.toast.show_toast(reason, "info")

    def _confirm_logout(self) -> None:
        answer = QMessageBox.question(
            self,
            "退出登录",
            "确定要退出登录并删除本地会话吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.controller.logout()

    # ---------- 数据 ----------
    def _on_snapshot_loaded(self, device_count: int, home_count: int) -> None:
        self.dashboard.reload_homes()
        self.dashboard.update_stats(self.controller.mqtt_connected)
        self.dashboard.set_title(
            "全部设备", f"共 {device_count} 个设备 · {home_count} 个家庭"
        )
        account = self.controller.account or ""
        masked = f"{account[:3]}****{account[-4:]}" if len(account) >= 8 else account
        self.title_bar.set_subtitle(masked or "未登录")

        self._sync_tray_devices()
        self._sync_board_devices()

        # 首次加载完成后应用「启动时默认分类」
        if self._startup_pending:
            self._startup_pending = False
            self.dashboard.show_view(self._settings.get("startup_view", "all"))

    def _on_devices_reloaded(self) -> None:
        # 首屏/刷新拉完设备后让卡片错峰浮出来，比"啪"地铺满一屏更容易看清
        self.dashboard.rebuild_cards(animate=True)
        self.dashboard.reload_homes()
        self.dashboard.update_stats(self.controller.mqtt_connected)
        self._sync_tray_devices()
        self._sync_board_devices()

    def _on_device_changed(self, dev_id: str) -> None:
        self.dashboard.update_device(dev_id)
        device = self.controller.store.get(dev_id)
        if device is not None:
            self.tray_panel.refresh_device(device)
            self.board.refresh_device(device)
        dialog = self._device_dialog
        if dialog is not None and dialog.isVisible() and dialog.dev_id == dev_id:
            device = self.controller.store.get(dev_id)
            if device is not None:
                dialog.reload(device)

    def _on_mqtt_state(self, connected: bool) -> None:
        self.dashboard.set_mqtt_state(connected)
        if connected:
            self.toast.show_toast("实时通道已连接", "success")

    def _on_busy(self, busy: bool, message: str) -> None:
        self.dashboard.set_busy(busy)
        if self._stack.currentWidget() is self.login_page:
            self.login_page.set_busy(busy, message)

    # ---------- 设备 ----------
    def _on_power_requested(self, dev_id: str) -> None:
        device = self.controller.store.get(dev_id)
        if device is None:
            return
        found = device_spec.find_switch_service(device)
        if found is None:
            self.toast.show_toast("该设备没有可用的开关服务", "warning")
            return
        sid, key, value = found
        self.controller.toggle_switch(dev_id, sid, key, value in (1, True))

    def _open_device(self, dev_id: str) -> None:
        device = self.controller.store.get(dev_id)
        if device is None:
            return
        if self._device_dialog is not None:
            if self._device_dialog.dev_id == dev_id and self._device_dialog.isVisible():
                self._device_dialog.raise_()
                self._device_dialog.activateWindow()
                return
            self._device_dialog.close()
            self._device_dialog = None

        dialog = DeviceDialog(
            device,
            self,
            show_advanced=bool(self._settings.get("device_advanced")),
        )
        dialog.command_requested.connect(self._on_device_command)
        dialog.query_requested.connect(self.controller.query_service)
        dialog.refresh_requested.connect(self.controller.refresh_device_state)
        dialog.advanced_changed.connect(self._set_device_advanced)
        dialog.camera_requested.connect(self._open_camera)
        dialog.finished.connect(self._on_dialog_finished)
        self._device_dialog = dialog
        dialog.show()
        self._center_dialog(dialog)
        self.controller.refresh_device_state(dev_id)

    def _set_device_advanced(self, enabled: bool) -> None:
        """记住「显示高级信息」的选择。

        设置对话框和详情窗底部都能改这一项，两处共用同一个键；
        详情窗开着的时候要跟着立即生效，否则用户会以为设置没作用。
        """
        self._settings["device_advanced"] = bool(enabled)
        self._save_settings()
        dialog = self._device_dialog
        if dialog is not None and dialog.isVisible():
            dialog.set_show_advanced(bool(enabled))

    def _center_dialog(self, dialog: DeviceDialog) -> None:
        """相对主窗口居中偏右，避免完全遮挡卡片。"""
        geo = self.geometry()
        dialog.move(
            geo.x() + max(0, (geo.width() - dialog.width()) // 2),
            geo.y() + max(0, (geo.height() - dialog.height()) // 2),
        )

    def _on_dialog_finished(self, _result: int) -> None:
        self._device_dialog = None

    def _on_device_command(self, dev_id: str, sid: str, data: dict, overrides: dict) -> None:
        self.controller.send_command(dev_id, sid, data, **overrides)

    # ---------- 摄像头实时画面 ----------
    def _open_camera(self, dev_id: str) -> None:
        """打开（或聚焦）某台摄像头的实时画面窗口。"""
        existing = self._camera_dialogs.get(dev_id)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        device = self.controller.store.get(dev_id)
        if device is None:
            return

        dialog = CameraDialog(device, self._camera_config(dev_id), self)
        dialog.config_changed.connect(self._save_camera_config)
        dialog.log.connect(self.dashboard.append_log)
        dialog.finished.connect(lambda _result, key=dev_id: self._camera_dialogs.pop(key, None))
        self._camera_dialogs[dev_id] = dialog
        dialog.show()
        self._center_dialog(dialog)

    def _open_manual_camera(self) -> None:
        """打开「手动添加的网络摄像头」窗口。

        这个入口不查华为设备列表——用来接任何开放 RTSP 的摄像头
        （电信天翼看家、海康、大华，或者同型号但没绑在华为账号上的）。
        配置按固定 key 存在 cameras["manual"] 下，与华为设备的 dev_id 不冲突。
        """
        key = "manual"
        existing = self._camera_dialogs.get(key)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        dialog = CameraDialog(
            None,
            self._camera_config(key),
            self,
            manual_name="网络摄像头",
            manual_key=key,
        )
        dialog.config_changed.connect(self._save_camera_config)
        dialog.log.connect(self.dashboard.append_log)
        dialog.finished.connect(
            lambda _result, name=key: self._camera_dialogs.pop(name, None)
        )
        self._camera_dialogs[key] = dialog
        dialog.show()
        self._center_dialog(dialog)

    def _camera_config(self, dev_id: str) -> dict:
        cameras = self._settings.get("cameras")
        if not isinstance(cameras, dict):
            return {}
        entry = cameras.get(dev_id)
        return dict(entry) if isinstance(entry, dict) else {}

    def _save_camera_config(self, dev_id: str, config: dict) -> None:
        """记住这台摄像头的 IP/账号/密码/码流。

        凭据只写在本机 ``ui_settings.json`` 里，不上传、不进日志；
        日志与界面里展示的地址都经过 ``masked_url`` 打码。
        """
        cameras = self._settings.get("cameras")
        if not isinstance(cameras, dict):
            cameras = {}
        cameras[dev_id] = dict(config)
        self._settings["cameras"] = cameras
        self._save_settings()

    def _close_camera_dialogs(self) -> None:
        for dialog in list(self._camera_dialogs.values()):
            dialog.close()
        self._camera_dialogs.clear()

    def _close_device_dialog(self) -> None:
        if self._device_dialog is not None:
            self._device_dialog.close()
            self._device_dialog = None

    # ---------- MQTT ----------
    def _toggle_mqtt(self) -> None:
        if self.controller.mqtt_connected:
            self.controller.disconnect_mqtt()
        else:
            self.controller.connect_mqtt()

    # ==================================================================
    # 主题与设置
    # ==================================================================
    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.setStyleSheet(build_qss(theme().palette))
        self.title_bar.set_theme_icon(theme().mode == "dark")
        self.title_bar.refresh_icons()
        self.dashboard.refresh_icons()
        if hasattr(self, "tray_panel"):
            self.tray_panel.refresh_icons()
            self.board.refresh_icons()
        self.login_page.apply_theme()
        # 自绘控件需要主动重绘
        for widget in self.findChildren(QWidget):
            widget.update()

    def _toggle_theme(self) -> None:
        mode = theme().toggle_mode()
        self._settings["mode"] = mode
        self._save_settings()
        self._apply_theme()
        if self._device_dialog is not None:
            self._device_dialog.update()

    def _set_accent(self, name: str) -> None:
        theme().set_accent(name)
        self._settings["accent"] = name
        self._save_settings()
        self._apply_theme()

    def _open_settings(self) -> None:
        families = [
            (home.home_id, home.name)
            for home, _count in self.controller.store.homes_with_counts()
        ]
        dialog = SettingsDialog(
            self.controller.account,
            families,
            self._settings.get("startup_view", "all"),
            capture_enabled=bool(self._settings.get("capture_telemetry")),
            device_advanced=bool(self._settings.get("device_advanced")),
            raw_protocol=bool(self._settings.get("raw_protocol")),
            motion_enabled=bool(self._settings.get("motion", True)),
            parent=self,
        )
        dialog.mode_changed.connect(self._set_mode)
        dialog.accent_changed.connect(self._set_accent)
        dialog.motion_changed.connect(self._set_motion)
        dialog.startup_view_changed.connect(self._set_startup_view)
        dialog.capture_changed.connect(self._set_capture)
        dialog.device_advanced_changed.connect(self._set_device_advanced)
        dialog.raw_protocol_changed.connect(self._set_raw_protocol)
        dialog.logout_requested.connect(self.controller.logout)
        dialog.exec()

    def _set_motion(self, enabled: bool) -> None:
        """界面动画总开关。关掉后所有过渡立即完成（远程桌面/低配机更跟手）。"""
        self._settings["motion"] = bool(enabled)
        self._save_settings()
        motion.set_enabled(bool(enabled))
        self.toast.show_toast(
            "已开启界面动画" if enabled else "已关闭界面动画", "info"
        )

    def _set_raw_protocol(self, enabled: bool) -> None:
        """日志显示原始报文：存盘、通知控制器，并把遥测展开。

        不展开的话，原始报文都记在「遥测」档里，用户开了开关却还是看不到。
        """
        self._settings["raw_protocol"] = bool(enabled)
        self._save_settings()
        self.controller.set_raw_protocol(bool(enabled))
        if enabled:
            self.dashboard.set_log_verbose(True)
        self.toast.show_toast(
            "日志已改为输出原始报文" if enabled else "日志已恢复中文摘要",
            "warning" if enabled else "info",
        )

    def _set_capture(self, enabled: bool) -> None:
        """协议调试开关：存盘并让控制器重连以应用新的订阅范围。"""
        self._settings["capture_telemetry"] = bool(enabled)
        self._save_settings()
        self.controller.set_capture_enabled(bool(enabled))
        if enabled:
            self.toast.show_toast("已开启协议调试订阅，日志会变详细", "warning")
        else:
            self.toast.show_toast("已关闭协议调试订阅", "info")

    def _set_startup_view(self, key: str) -> None:
        """记住「启动时默认打开」的分类，下次登录后自动跳转。"""
        self._settings["startup_view"] = key
        self._save_settings()
        label = {
            "all": "全部设备",
            "scenes": "智能场景",
            "logs": "运行日志",
            "other": "其他设备",
        }.get(key)
        if label is None and key.startswith("home:"):
            label = f"家庭 · {self.controller.store.home_name(key.split(':', 1)[1])}"
        self.toast.show_toast(f"下次启动将打开：{label or '全部设备'}", "info")

    def _set_mode(self, mode: str) -> None:
        theme().set_mode(mode)
        self._settings["mode"] = mode
        self._save_settings()
        self._apply_theme()

    # ==================================================================
    # 设置持久化
    # ==================================================================
    def _load_settings(self) -> dict:
        try:
            with open(UI_SETTINGS_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(UI_SETTINGS_PATH, "w", encoding="utf-8") as handle:
                json.dump(self._settings, handle, ensure_ascii=False, indent=2)
        except OSError as error:
            _LOGGER.warning("保存界面设置失败: %s", error)

    # ==================================================================
    # 生命周期
    # ==================================================================
    def _bootstrap(self) -> None:
        """启动时尝试免密恢复会话。"""
        self.title_bar.set_controls_visible(False)
        self._remember = True

        def done(restored: bool) -> None:
            if not restored:
                self._stack.setCurrentWidget(self.login_page)
                self.title_bar.set_controls_visible(False)
                if not self.login_page.isVisible():
                    self._stack.setCurrentWidget(self.login_page)

        def failed(error: BaseException) -> None:
            self.dashboard.append_log(f"恢复登录状态失败：{friendly_error(error)}", "warning")
            self._stack.setCurrentWidget(self.login_page)

        run_async(self.controller.restore_session, done, failed)

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.title_bar.set_maximized(False)
        else:
            self.showMaximized()
            self.title_bar.set_maximized(True)
        self._sync_window_corners(self.isMaximized())
        if self._device_dialog is not None:
            self._center_dialog(self._device_dialog)

    def _sync_window_corners(self, maximized: bool | None = None) -> None:
        """最大化时去掉圆角，否则屏幕边缘会留出四道缝。

        显式传入状态而不是内部查询：某些平台（如无头/远程会话）里
        ``isMaximized()`` 会慢一拍，导致切完状态后圆角没跟上。
        """
        if maximized is None:
            maximized = self.isMaximized()
        value = "true" if maximized else "false"
        for widget in (self.centralWidget(), self.title_bar):
            if widget is None:
                continue
            # 属性名不能叫 maximized：那是 QWidget 的只读内建属性，写不进去
            widget.setProperty("winMaximized", value)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_window_corners()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self._save_settings()
        self._close_device_dialog()
        self._close_camera_dialogs()

        # 托盘可用时，关闭窗口 = 收进托盘（真正退出走托盘菜单）
        if not self._quit_requested and self.tray.available:
            event.ignore()
            self.hide()
            self.tray.show_message(
                "华为智慧生活",
                "已最小化到托盘，右键托盘图标可退出。",
            )
            return

        try:
            self.controller.disconnect_mqtt()
        except Exception:  # noqa: BLE001 - 退出阶段忽略断开异常
            pass
        self.board.hide()
        self.tray_panel.hide()
        self.tray.hide()
        super().closeEvent(event)
