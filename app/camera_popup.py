"""桌面挂件旁的摄像头实时画面浮层。

为什么单独做一个浮层，而不是直接弹 CameraDialog：
CameraDialog 是「配置 + 播放」的完整窗口（760×600，带 IP / 账号 / 扫描局域网 /
码流识别），从桌面挂件点开只为瞄一眼画面，把这些表单一起端出来太重。这个浮层
只负责看画面——要改配置点它的滑杆按钮回大窗口，两边各司其职。

读帧与绘制直接复用 CameraDialog 的 ``FrameGrabber`` / ``VideoView``：同一路流的
解码参数（缓冲区只留最新帧、断流阈值）必须一致，各写一份迟早跑偏。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.camera import RtspCamera, port_open

from .dialogs.camera_dialog import FrameGrabber, VideoView, have_opencv
from .theme import theme
from .widgets.glyph_icons import make_icon
from .widgets.labels import ElidedLabel
from .widgets.rounded_surface import RoundedSurface

VIDEO_WIDTH = 480
VIDEO_HEIGHT = 270
PADDING = 10
BAR_HEIGHT = 30


class _PortProbe(QThread):
    """开流前先探端口。

    cv2 在地址不通时会死等内部 30 秒上限才报错（``CAP_PROP_OPEN_TIMEOUT_MSEC``
    实测无效），在浮层里干等半分钟等于「点了没反应」，所以先花 1 秒问端口。
    """

    done = Signal(bool)

    def __init__(self, host: str, port: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port

    def run(self) -> None:  # noqa: D401 - QThread 接口
        try:
            self.done.emit(port_open(self._host, self._port, timeout=1.2))
        except Exception:  # noqa: BLE001 - 探测失败等同于连不上，不能把界面带崩
            self.done.emit(False)


class CameraPopup(QWidget):
    """桌面挂件旁的实时画面小窗。"""

    configure_requested = Signal(str)   # key：回完整窗口改配置
    target_changed = Signal(str)        # 用户切到了另一台（外面据此记住它）
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        # 独立顶层窗口（parent=None）：主窗口收进托盘、挂件缩回，画面都不该跟着没
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("华为智慧生活 · 实时画面")
        self._targets: list[dict] = []
        self._index = 0
        self._camera: RtspCamera | None = None
        self._grabber: FrameGrabber | None = None
        self._probe: _PortProbe | None = None
        self._token = 0                      # 连接代号：迟到的回调一律作废
        self._drag_offset: QPoint | None = None
        self._frame_count = 0
        self._fps_from = time.monotonic()
        self._size_text = "—"
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        self._build()
        self.refresh_icons()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)

        surface = RoundedSurface(radius=14)
        shell.addWidget(surface)

        layout = QVBoxLayout(surface)
        layout.setContentsMargins(PADDING, 8, PADDING, 8)
        layout.setSpacing(6)

        layout.addWidget(self._build_topbar())

        self._view = VideoView(minimum=(1, 1))
        self._view.setFixedSize(VIDEO_WIDTH, VIDEO_HEIGHT)
        self._view.set_hint("准备中…")
        layout.addWidget(self._view)

        layout.addLayout(self._build_status())

        self.setFixedWidth(VIDEO_WIDTH + PADDING * 2)

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 0, 0)
        layout.setSpacing(2)

        self._name_label = ElidedLabel("摄像头")
        self._name_label.setObjectName("PopoverTitle")
        layout.addWidget(self._name_label, 1)

        self._prev_btn = self._nav_button("‹", "上一台")
        self._prev_btn.clicked.connect(lambda: self._step(-1))
        layout.addWidget(self._prev_btn)

        self._next_btn = self._nav_button("›", "下一台")
        self._next_btn.clicked.connect(lambda: self._step(1))
        layout.addWidget(self._next_btn)

        self._config_btn = self._icon_button("sliders", "配置这台摄像头")
        self._config_btn.clicked.connect(self._on_configure)
        layout.addWidget(self._config_btn)

        close_btn = self._icon_button("close", "关闭画面")
        close_btn.clicked.connect(self.hide)
        layout.addWidget(close_btn)

        bar.mousePressEvent = self._bar_press      # type: ignore[method-assign]
        bar.mouseMoveEvent = self._bar_move        # type: ignore[method-assign]
        bar.mouseReleaseEvent = self._bar_release  # type: ignore[method-assign]
        self._topbar = bar
        return bar

    def _nav_button(self, text: str, tooltip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("PopoverButton")
        button.setFixedSize(22, 22)
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        button.setStyleSheet("font-size: 15px; padding-bottom: 4px;")
        return button

    def _icon_button(self, name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("PopoverButton")
        button.setFixedSize(24, 24)
        button.setIconSize(QSize(14, 14))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFlat(True)
        self._icon_buttons.append((button, name))
        return button

    def _build_status(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {theme().palette.text_3}; font-size: 10px;")
        row.addWidget(self._dot)

        self._status = QLabel("")
        self._status.setObjectName("Caption")
        row.addWidget(self._status, 1)

        self._retry = QPushButton("重试")
        self._retry.setObjectName("LinkButton")
        self._retry.setCursor(Qt.PointingHandCursor)
        self._retry.clicked.connect(lambda: self.start())
        self._retry.hide()
        row.addWidget(self._retry)
        return row

    def refresh_icons(self) -> None:
        color = theme().palette.text_2
        for button, name in self._icon_buttons:
            button.setIcon(make_icon(name, color, 14, width=1.4))

    # ==================================================================
    # 数据
    # ==================================================================
    def set_targets(self, targets: list[dict], preferred: str = "") -> None:
        """targets: ``[{key, name, config}]``，顺序由调用方排好。

        注意这个接口**不负责猜**该看哪台：``preferred`` 是调用方给的
        「上次看的那台」，没有才退回当前这台、再退回第一个。拿名字或顺序
        去猜"用户大概想看哪台"是猜不准的——三台摄像头时猜错就是每次都要
        多按两下箭头。

        刷新时也尽量停在原来那台：设备列表每次刷新都会重传一遍，
        要是每次都跳回第一台，用户看到一半的画面就被切走了。
        """
        current = self.current()
        keep_key = preferred or (str(current.get("key")) if current else "")
        self._targets = list(targets)
        self._index = 0
        if keep_key:
            for index, target in enumerate(self._targets):
                if str(target.get("key")) == keep_key:
                    self._index = index
                    break
        self._sync_nav()

    def current(self) -> dict | None:
        if not self._targets:
            return None
        return self._targets[max(0, min(self._index, len(self._targets) - 1))]

    def _sync_nav(self) -> None:
        multi = len(self._targets) > 1
        self._prev_btn.setVisible(multi)
        self._next_btn.setVisible(multi)

    def reload_config(self, key: str, config: dict) -> None:
        """完整窗口保存配置后同步过来；正在看这台就重连一次。"""
        for target in self._targets:
            if str(target.get("key")) == key:
                target["config"] = dict(config)
                if self.current() is target and self.isVisible():
                    self.start()
                return

    # ==================================================================
    # 定位
    # ==================================================================
    def show_for(self, anchor: QRect) -> None:
        """贴着挂件弹出：优先放它左边（挂件常贴屏幕右侧），放不下改右边。"""
        self.adjustSize()
        width, height = self.width(), self.height()
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)

        gap = 10
        if anchor.left() - gap - width >= area.left():
            x = anchor.left() - gap - width
        elif anchor.right() + gap + width <= area.right():
            x = anchor.right() + gap
        else:
            x = area.right() - width

        # 底部对齐挂件，避免盖住用户刚点的那个按钮
        y = anchor.bottom() - height
        x = max(area.left(), min(x, area.right() - width))
        y = max(area.top(), min(y, area.bottom() - height))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.start()

    # ==================================================================
    # 播放
    # ==================================================================
    def start(self) -> None:
        self.stop()
        target = self.current()
        if target is None:
            self._name_label.setText("摄像头")
            self._view.set_hint("没有可看的摄像头")
            self._set_status("先在主界面添加摄像头", "warn")
            return

        name = str(target.get("name") or "摄像头")
        self._name_label.setText(name)
        self._sync_nav()
        cfg = target.get("config") or {}
        host = str(cfg.get("host") or "").strip()

        if not host:
            self._view.set_hint(
                f"「{name}」还没填地址\n点右上角的滑杆按钮去配置 IP 与账号"
            )
            self._set_status("未配置", "warn")
            self._show_retry(False)
            return

        if not have_opencv():
            self._view.set_hint("未安装 opencv-python，无法内嵌播放")
            self._set_status("缺少 opencv-python：pip install opencv-python", "error")
            self._show_retry(False)
            return

        stream = str(cfg.get("stream") or "sub")
        paths = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
        camera = RtspCamera(
            name=name,
            host=host,
            username=str(cfg.get("username") or ""),
            password=str(cfg.get("password") or ""),
            port=int(cfg.get("port") or 554),
            stream=stream,
            path=str(paths.get(stream)) if paths.get(stream) else None,
        )
        self._camera = camera
        self._size_text = "—"
        self._view.set_hint("正在连接…\n首帧通常需要 1–3 秒")
        self._set_status(f"正在连接 {camera.masked_url}", "idle")
        self._show_retry(False)

        token = self._token
        probe = _PortProbe(camera.host, camera.port, self)
        probe.done.connect(lambda ok, tk=token: self._on_port_checked(tk, ok))
        probe.finished.connect(probe.deleteLater)
        self._probe = probe
        probe.start()

    def stop(self) -> None:
        self._token += 1        # 在途的端口探测结果一律作废
        self._probe = None
        grabber = self._grabber
        self._grabber = None
        if grabber is None:
            return
        grabber.stop()
        # cv2 的 read() 可能正卡在网络等待里，等不到就放弃，别给关闭动作拖后腿
        if not grabber.wait(2000):
            grabber.terminate()
            grabber.wait(500)
        grabber.deleteLater()

    def _on_port_checked(self, token: int, ok: bool) -> None:
        if token != self._token:
            return
        self._probe = None
        camera = self._camera
        if camera is None:
            return
        if not ok:
            self._view.set_hint(
                f"连不上 {camera.host}:{camera.port}\n"
                "确认 IP 没写错、摄像头与电脑接在同一路由器"
            )
            self._set_status("端口不通，检查 IP 与局域网", "error")
            self._show_retry(True)
            return

        grabber = FrameGrabber(camera.rtsp_url, self)
        grabber.frame_ready.connect(self._on_frame)
        grabber.opened.connect(self._on_opened)
        grabber.failed.connect(self._on_failed)
        self._grabber = grabber
        grabber.start()

    def _on_opened(self, width: int, height: int) -> None:
        self._size_text = f"{width}×{height}" if width and height else "未知分辨率"
        self._set_status(f"在线 · {self._size_text}", "ok")
        self._fps_from = time.monotonic()
        self._frame_count = 0

    def _on_failed(self, message: str) -> None:
        self._view.set_hint(f"{message}\n\n检查后点「重试」")
        self._set_status(message, "error")
        self._show_retry(True)

    def _on_frame(self, image: QImage) -> None:
        self._view.set_image(image)
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_from
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_from = now
            self._set_status(f"在线 · {fps:.0f} fps · {self._size_text}", "ok")

    # ==================================================================
    # 交互
    # ==================================================================
    def _step(self, delta: int) -> None:
        if len(self._targets) < 2:
            return
        self._index = (self._index + delta) % len(self._targets)
        self.start()
        target = self.current()
        if target is not None:
            # 切台就记住：下次点开挂件的相机按钮应该还是这台
            self.target_changed.emit(str(target.get("key")))

    def _on_configure(self) -> None:
        target = self.current()
        if target is not None:
            self.configure_requested.emit(str(target.get("key") or ""))

    def _show_retry(self, visible: bool) -> None:
        self._retry.setVisible(bool(visible) and self.current() is not None)

    def _set_status(self, text: str, level: str = "idle") -> None:
        palette = theme().palette
        colour = {
            "ok": palette.ok,
            "warn": palette.warn,
            "error": palette.danger,
            "idle": palette.text_3,
        }.get(level, palette.text_3)
        self._dot.setStyleSheet(f"color: {colour}; font-size: 10px;")
        self._status.setText(text)

    def _bar_press(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        event.accept()

    def _bar_move(self, event) -> None:
        if self._drag_offset is None or not (event.buttons() & Qt.LeftButton):
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def _bar_release(self, event) -> None:
        self._drag_offset = None
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.key() == Qt.Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        # 双击画面 = 要看细节 / 要调参数，交给完整窗口
        self._on_configure()
        event.accept()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().hideEvent(event)
        self.stop()
        self.closed.emit()
