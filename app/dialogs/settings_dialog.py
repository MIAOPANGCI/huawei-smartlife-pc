"""设置对话框：两列布局——左边日常（外观 / 启动 / 账号），右边进阶（挂件 / 诊断 / 关于）。

分两列不是为好看：设置项加到九项之后单列要 1100px 高，1366×768 的笔记本上
必须滚动才能看到「关闭」。两列把高度砍掉一半，同时按「常用 / 排查用」分组，
左列是天天会动的，右列基本只在出问题时打开。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import autostart, updater
from ..theme import ACCENT_PRESETS, theme
from ..workers import run_async
from huawei_smartlife.version import VERSION

DIALOG_WIDTH = 780


class _ColorSwatch(QPushButton):
    """主题色色块。"""

    def __init__(self, name: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setCheckable(True)
        self.setToolTip(name)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {color};
                border: 2px solid transparent;
                border-radius: 15px;
            }}
            QPushButton:checked {{
                border: 2px solid {theme().palette.text};
            }}
            """
        )


class SettingsDialog(QDialog):
    """外观、启动、挂件与账号设置。"""

    mode_changed = Signal(str)
    accent_changed = Signal(str)
    motion_changed = Signal(bool)
    startup_view_changed = Signal(str)
    capture_changed = Signal(bool)
    device_advanced_changed = Signal(bool)
    raw_protocol_changed = Signal(bool)
    camera_default_changed = Signal(str)
    board_restore_changed = Signal(bool)
    autostart_changed = Signal(bool)
    update_ready = Signal(str)          # 新版本目录，由主窗口接手替换
    logout_requested = Signal()

    def __init__(
        self,
        account: str,
        families: list[tuple[str, str]] | None = None,
        startup_view: str = "all",
        capture_enabled: bool = False,
        device_advanced: bool = False,
        raw_protocol: bool = False,
        motion_enabled: bool = True,
        cameras: list[tuple[str, str]] | None = None,
        camera_default: str = "",
        board_restore: bool = True,
        autostart_enabled: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        # 两列需要横向空间，但小屏（1024 宽以下）不能直接溢出屏幕
        screen = QGuiApplication.primaryScreen()
        limit = int(screen.availableGeometry().width() * 0.92) if screen else DIALOG_WIDTH
        self.setFixedWidth(min(DIALOG_WIDTH, limit))
        self._account = account
        self._families = families or []
        self._startup_view = startup_view
        self._capture_enabled = bool(capture_enabled)
        self._device_advanced = bool(device_advanced)
        self._raw_protocol = bool(raw_protocol)
        self._motion_enabled = bool(motion_enabled)
        # (显示名, key)：key 是设备 dev_id，手动接入的那台固定为 "manual"
        self._cameras = cameras or []
        self._camera_default = camera_default
        self._board_restore = bool(board_restore)
        # None = 问注册表要真实状态（设置项本身可能存在注册表里、不一定写过 settings）
        self._autostart_enabled = (
            autostart.is_enabled() if autostart_enabled is None else bool(autostart_enabled)
        )
        # 更新相关状态（下载进度由后台线程写、主线程定时读，见 _refresh_progress）
        self._latest: updater.Release | None = None
        self._last_check_at = 0.0
        self._update_busy = False
        self._staged_dir = ""
        self._progress: dict = {"done": 0, "total": 0, "state": "idle"}
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(300)
        self._progress_timer.timeout.connect(self._refresh_progress)
        self._build()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(24, 20, 24, 14)
        shell.setSpacing(10)

        # 两列之后高度已经降下来了，但小屏 + 系统缩放 150% 仍可能顶出去，
        # 所以保留滚动区兜底。
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        page = QVBoxLayout(content)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(14)
        self._scroll.setWidget(content)
        shell.addWidget(self._scroll, 1)

        title = QLabel("设置")
        title.setObjectName("PageTitle")
        page.addWidget(title)

        columns = QHBoxLayout()
        columns.setSpacing(30)
        left_host, self._left = self._column()
        right_host, self._right = self._column()
        columns.addWidget(left_host, 1)
        columns.addWidget(right_host, 1)
        page.addLayout(columns)

        # ---- 左列：日常 ----
        self._build_appearance(self._left)
        self._left.addWidget(self._divider())
        self._build_startup(self._left)
        self._left.addWidget(self._divider())
        self._build_update(self._left)
        self._left.addWidget(self._divider())
        self._build_account(self._left)
        self._left.addStretch(1)

        # ---- 右列：进阶 / 排查 ----
        self._build_board(self._right)
        self._right.addWidget(self._divider())
        self._build_diagnosis(self._right)
        self._right.addWidget(self._divider())
        self._build_about(self._right)
        self._right.addStretch(1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        shell.addLayout(close_row)

        self._fit_to_screen()

    def _column(self) -> tuple[QWidget, QVBoxLayout]:
        host = QWidget()
        box = QVBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        return host, box

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        return line

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Caption")
        label.setWordWrap(True)
        return label

    def _row(self, label_text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        label = QLabel(label_text)
        label.setObjectName("Body")
        row.addWidget(label, 0)
        row.addWidget(widget, 1)
        return row

    # ---- 外观 ----
    def _build_appearance(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("外观"))

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for mode, text in (("dark", "深色"), ("light", "浅色")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(theme().mode == mode)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)   # 避免 Fusion 焦点框盖住选中态
            button.clicked.connect(lambda _c=False, m=mode: self.mode_changed.emit(m))
            self._mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        accent_row = QHBoxLayout()
        accent_row.setSpacing(10)
        accent_label = QLabel("主题色")
        accent_label.setObjectName("Body")
        accent_row.addWidget(accent_label)
        self._accent_group = QButtonGroup(self)
        self._accent_group.setExclusive(True)
        for name, color in ACCENT_PRESETS.items():
            swatch = _ColorSwatch(name.strip(), color)
            swatch.setChecked(name == theme().accent_name)
            swatch.setFocusPolicy(Qt.NoFocus)
            swatch.clicked.connect(lambda _c=False, n=name: self.accent_changed.emit(n))
            self._accent_group.addButton(swatch)
            accent_row.addWidget(swatch)
        accent_row.addStretch(1)
        layout.addLayout(accent_row)

        self._motion_box = QCheckBox("界面动画（卡片淡入、窗口滑入）")
        self._motion_box.setChecked(self._motion_enabled)
        self._motion_box.setToolTip(
            "关掉后所有过渡立即完成。远程桌面、录屏或低配机器上更跟手。"
        )
        self._motion_box.toggled.connect(self._on_motion_toggled)
        layout.addWidget(self._motion_box)

    # ---- 启动 ----
    def _build_startup(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("启动"))

        self._startup_combo = QComboBox()
        self._startup_combo.setMinimumWidth(190)
        options: list[tuple[str, str]] = [
            ("全部设备", "all"),
            ("智能场景", "scenes"),
            ("运行日志", "logs"),
        ]
        for home_id, name in self._families:
            options.append((f"家庭 · {name}", f"home:{home_id}"))
        current_index = 0
        for index, (_label, key) in enumerate(options):
            self._startup_combo.addItem(_label, key)
            if key == self._startup_view:
                current_index = index
        self._startup_combo.setCurrentIndex(current_index)
        self._startup_combo.currentIndexChanged.connect(self._on_startup_changed)
        layout.addLayout(self._row("启动时默认打开", self._startup_combo))
        layout.addWidget(self._hint("下次启动登录后自动切换到这个分类。"))

        self._autostart_box = QCheckBox("开机自启动（登录 Windows 后自动运行）")
        self._autostart_box.setChecked(self._autostart_enabled)
        self._autostart_box.toggled.connect(self._on_autostart_toggled)
        if not autostart.supported():
            # 非 Windows 上这项没法实现，藏起来而不是留一个点了没反应的勾选框
            self._autostart_box.hide()
        layout.addWidget(self._autostart_box)

        if autostart.launcher_path() is not None:
            hint_text = (
                "在「启动」文件夹里放一个快捷方式（不需要管理员权限），开机后无窗口启动到托盘。"
                "任务管理器「启动应用」里显示为「华为智慧生活 PC 控制台」。"
            )
        else:
            # 没打包启动器时只能指向 pythonw，任务管理器按目标程序的产品名显示
            hint_text = (
                "在「启动」文件夹里放一个快捷方式（不需要管理员权限），开机后无窗口启动到托盘。"
                "未打包启动器（py -3 launcher/build.py）时，任务管理器里会显示成 Python。"
            )
        startup_hint = self._hint(hint_text)
        if autostart.supported():
            startup_hint.setToolTip(f"快捷方式：{autostart.shortcut_path()}")
        layout.addWidget(startup_hint)
        if not autostart.supported():
            startup_hint.hide()

    # ---- 更新 ----
    def _build_update(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("更新"))

        row = QHBoxLayout()
        row.setSpacing(8)
        self._version_label = QLabel(f"当前版本 v{VERSION}")
        self._version_label.setObjectName("Body")
        row.addWidget(self._version_label, 1)
        self._check_btn = QPushButton("检查更新")
        self._check_btn.setObjectName("Ghost")
        self._check_btn.setCursor(Qt.PointingHandCursor)
        self._check_btn.clicked.connect(self._on_check_update)
        row.addWidget(self._check_btn)
        layout.addLayout(row)

        self._update_status = self._hint(
            "从 GitHub 仓库拉取最新版本。源码运行时直接 git pull；"
            "打包版会下载新包、替换后自动重启。"
        )
        layout.addWidget(self._update_status)

        self._apply_btn = QPushButton("立即更新")
        self._apply_btn.setObjectName("Primary")
        self._apply_btn.setCursor(Qt.PointingHandCursor)
        self._apply_btn.clicked.connect(self._on_apply_update)
        self._apply_btn.hide()
        layout.addWidget(self._apply_btn, 0, Qt.AlignLeft)

        # 已经下载解压好，等用户点"重启并安装"
        self._install_btn = QPushButton("重启并安装")
        self._install_btn.setObjectName("Primary")
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(self._on_install)
        self._install_btn.hide()
        layout.addWidget(self._install_btn, 0, Qt.AlignLeft)

    def _set_update_status(self, text: str, level: str = "idle") -> None:
        palette = theme().palette
        colour = {
            "ok": palette.ok,
            "warn": palette.warn,
            "error": palette.danger,
        }.get(level, palette.text_3)
        self._update_status.setText(text)
        self._update_status.setStyleSheet(f"color: {colour};")

    def _on_check_update(self) -> None:
        if self._update_busy:
            return
        # 匿名接口每小时只有 60 次配额，连点几下就会被限流；60 秒内复用上次结果
        if self._latest is not None and time.monotonic() - self._last_check_at < 60:
            self._on_update_found(self._latest)
            return
        self._update_busy = True
        self._check_btn.setEnabled(False)
        self._apply_btn.hide()
        self._install_btn.hide()
        self._set_update_status("正在检查…")
        run_async(updater.fetch_latest, self._on_update_found, self._on_update_error)

    def _on_update_found(self, release: updater.Release) -> None:
        self._update_busy = False
        self._last_check_at = time.monotonic()
        self._check_btn.setEnabled(True)
        self._latest = release
        if updater.is_newer(release.version, VERSION):
            self._set_update_status(
                f"发现新版本 v{release.version}（当前 v{VERSION}）", "ok"
            )
            # 源码运行走 git pull，打包版走下载替换
            self._apply_btn.setText(
                "拉取更新" if updater.source_root() is not None else "下载并更新"
            )
            self._apply_btn.show()
        else:
            self._set_update_status(f"已是最新版本（v{VERSION}）", "ok")
            self._apply_btn.hide()

    def _on_update_error(self, error: BaseException) -> None:
        self._update_busy = False
        self._check_btn.setEnabled(True)
        self._apply_btn.hide()
        self._set_update_status(str(error), "error")

    def _on_apply_update(self) -> None:
        if self._update_busy or self._latest is None:
            return
        self._update_busy = True
        self._apply_btn.setEnabled(False)

        if updater.source_root() is not None:
            self._set_update_status("正在拉取代码…")
            run_async(updater.git_pull, self._on_git_pulled, self._on_apply_error)
            return

        dest = Path(tempfile.gettempdir()) / f"{updater.APP_NAME}-update.zip"
        self._progress.update(done=0, total=self._latest.zip_size, state="downloading")
        self._progress_timer.start()
        self._set_update_status("准备下载…")

        url = self._latest.zip_url

        def job() -> str:
            path = updater.download(url, dest, self._progress)
            return str(updater.stage(path))

        run_async(job, self._on_staged, self._on_apply_error)

    def _refresh_progress(self) -> None:
        done = int(self._progress.get("done") or 0)
        total = int(self._progress.get("total") or 0)
        if self._progress.get("state") == "done":
            self._progress_timer.stop()
            self._set_update_status("下载完成，正在解压…")
            return
        if total:
            percent = min(99, int(done * 100 / total))
            self._set_update_status(
                f"下载中 {percent}%（{done / 1048576:.0f}/{total / 1048576:.0f} MB）"
            )
        else:
            self._set_update_status(f"下载中 {done / 1048576:.1f} MB")

    def _on_staged(self, new_dir: str) -> None:
        self._progress_timer.stop()
        self._update_busy = False
        self._staged_dir = str(new_dir)
        self._set_update_status(
            f"v{self._latest.version if self._latest else ''} 已准备好，"
            "点「重启并安装」完成更新",
            "ok",
        )
        self._apply_btn.hide()
        self._install_btn.show()

    def _on_install(self) -> None:
        if self._staged_dir:
            self.update_ready.emit(self._staged_dir)

    def _on_git_pulled(self, message: str) -> None:
        self._update_busy = False
        self._apply_btn.setEnabled(True)
        self._set_update_status(message, "ok")
        if "重启" in message:
            self._set_update_status(f"{message}（关掉本程序重新打开即可）", "ok")

    def _on_apply_error(self, error: BaseException) -> None:
        self._progress_timer.stop()
        self._update_busy = False
        self._apply_btn.setEnabled(True)
        self._set_update_status(str(error), "error")

    # ---- 账号 ----
    def _build_account(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("账号"))
        account_label = QLabel(f"当前账号：{self._account or '未登录'}")
        account_label.setObjectName("Body")
        account_label.setWordWrap(True)
        layout.addWidget(account_label)

        layout.addWidget(
            self._hint(
                "登录凭据以明文保存在 ~/.huawei_smartlife/session.json，"
                "仅供本机免密登录使用；退出登录会自动删除该文件。"
            )
        )

        logout = QPushButton("退出登录并删除本地会话")
        logout.setObjectName("Danger")
        logout.setCursor(Qt.PointingHandCursor)
        logout.clicked.connect(self._on_logout)
        layout.addWidget(logout, 0, Qt.AlignLeft)

    # ---- 桌面挂件 ----
    def _build_board(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("桌面挂件"))

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(190)
        self._camera_combo.addItem("跟随上次查看的那台", "")
        for label, key in self._cameras:
            self._camera_combo.addItem(label, key)
        current_index = 0
        for index in range(self._camera_combo.count()):
            if str(self._camera_combo.itemData(index)) == self._camera_default:
                current_index = index
                break
        self._camera_combo.setCurrentIndex(current_index)
        self._camera_combo.currentIndexChanged.connect(self._on_camera_default_changed)
        if not self._cameras:
            self._camera_combo.setEnabled(False)

        layout.addLayout(self._row("相机按钮默认看", self._camera_combo))
        layout.addWidget(
            self._hint(
                "挂件标题栏的相机按钮点开就看这台；选「跟随上次查看」则用你最后切到的那台"
                "（在主界面点过某台的实时画面也算）。"
            )
        )

        self._board_restore_box = QCheckBox("启动时恢复上次打开的桌面挂件")
        self._board_restore_box.setChecked(self._board_restore)
        self._board_restore_box.setToolTip(
            "上次挂着的话，启动后在原来的位置自动显示；点过挂件的 × 关掉就不再弹回来。"
        )
        self._board_restore_box.toggled.connect(self._on_board_restore_toggled)
        layout.addWidget(self._board_restore_box)
        layout.addWidget(
            self._hint("挂件的位置、贴边状态、锁定与收起都会一并恢复。")
        )

    # ---- 诊断 ----
    def _build_diagnosis(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("诊断"))

        self._advanced_box = QCheckBox("设备详情显示高级信息")
        self._advanced_box.setChecked(self._device_advanced)
        self._advanced_box.toggled.connect(self._on_device_advanced_toggled)
        layout.addWidget(self._advanced_box)
        layout.addWidget(
            self._hint(
                "默认只显示能操作的选项（开关、滑块，以及温度、电量这类日常指标）。"
                "勾选后详情窗会展示只读诊断字段（信号强度、电压、原始数据）"
                "与协议命令下发面板。"
            )
        )

        self._raw_box = QCheckBox("运行日志显示原始协议报文")
        self._raw_box.setChecked(self._raw_protocol)
        self._raw_box.toggled.connect(self._on_raw_protocol_toggled)
        layout.addWidget(self._raw_box)
        layout.addWidget(
            self._hint(
                "开启后运行日志直接输出 MQTT 原始 JSON（含 header / body 全部字段），"
                "不再翻译成中文摘要。排查协议、对照官方 App 时用。"
            )
        )

        self._capture_box = QCheckBox("协议调试订阅（收到账号下所有 MQTT 消息）")
        self._capture_box.setChecked(self._capture_enabled)
        self._capture_box.toggled.connect(self._on_capture_toggled)
        layout.addWidget(self._capture_box)
        layout.addWidget(
            self._hint(
                "开启后会额外订阅 signaltrans 通配主题，连手机端官方 App 发出的查询"
                "也会进到运行日志里——排查协议时有用，日常会让日志明显变吵。"
                "切换后会重新连接实时通道。"
            )
        )

    # ---- 关于 ----
    def _build_about(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._section("关于"))
        about = QLabel(
            f"华为智慧生活 PC 控制台 v{VERSION} · 非官方客户端\n"
            "基于社区对智慧生活云端协议的逆向研究实现，华为未开放官方 API，"
            "接口可能随版本更新失效。仅供个人学习与研究使用。"
        )
        about.setObjectName("Caption")
        about.setWordWrap(True)
        about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(about)

    def _fit_to_screen(self) -> None:
        """高度不超过屏幕可用区域的 85%，超出的部分交给滚动区。

        不能用 ``self.sizeHint()``：滚动区给出的是它自己的默认高度，跟里面
        放了多少内容无关（会算出一个比实际矮很多的窗口）。
        """
        content = self._scroll.widget()
        body = content.sizeHint().height() if content is not None else 480
        margins = self.layout().contentsMargins()
        chrome = (
            margins.top()
            + margins.bottom()
            + self.layout().spacing()
            + 46  # 底部「关闭」按钮一行
        )
        natural = body + chrome
        height = natural
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            height = min(natural, max(380, int(screen.availableGeometry().height() * 0.85)))
        self.resize(self.width(), height)

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    # ==================================================================
    # 回调
    # ==================================================================
    def set_autostart(self, checked: bool) -> None:
        """外部（写注册表失败时）把勾选状态改回真实值，不再触发信号。"""
        self._autostart_enabled = bool(checked)
        blocker = self._autostart_box.blockSignals(True)
        self._autostart_box.setChecked(self._autostart_enabled)
        self._autostart_box.blockSignals(blocker)

    def _on_motion_toggled(self, checked: bool) -> None:
        self._motion_enabled = bool(checked)
        self.motion_changed.emit(self._motion_enabled)

    def _on_device_advanced_toggled(self, checked: bool) -> None:
        self._device_advanced = bool(checked)
        self.device_advanced_changed.emit(self._device_advanced)

    def _on_raw_protocol_toggled(self, checked: bool) -> None:
        self._raw_protocol = bool(checked)
        self.raw_protocol_changed.emit(self._raw_protocol)

    def _on_capture_toggled(self, checked: bool) -> None:
        self._capture_enabled = bool(checked)
        self.capture_changed.emit(self._capture_enabled)

    def _on_startup_changed(self, index: int) -> None:
        key = self._startup_combo.itemData(index)
        if isinstance(key, str):
            self.startup_view_changed.emit(key)

    def _on_camera_default_changed(self, index: int) -> None:
        # 空串 = 跟随上次查看，是有意义的值，所以不像启动项那样跳过空值
        key = self._camera_combo.itemData(index)
        self.camera_default_changed.emit("" if key is None else str(key))

    def _on_board_restore_toggled(self, checked: bool) -> None:
        self._board_restore = bool(checked)
        self.board_restore_changed.emit(self._board_restore)

    def _on_autostart_toggled(self, checked: bool) -> None:
        self._autostart_enabled = bool(checked)
        self.autostart_changed.emit(self._autostart_enabled)

    def _on_logout(self) -> None:
        self.logout_requested.emit()
        self.accept()
