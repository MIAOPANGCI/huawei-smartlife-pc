"""摄像头实时画面窗口：RTSP 拉流 → Qt 画面（OpenCV 解码）。

为什么单独开一个窗口，而不是塞进设备详情窗：
  - 视频是持续刷新的，塞进详情窗会让那个原本「一眼看完」的卡片变成大块面积；
  - 非模态窗口可以边看边操作设备，也能同时开多路（客厅 + 门口）。

限制写在明处：只有开放 RTSP 的型号能拉流（小豚当家室外 3K / 5 4K 系列），
其余型号的流走华为云私有加密通道，第三方无法还原——窗口里会直接说明原因。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

# 必须在 cv2 打开流之前设置：OpenCV 默认走 UDP，家庭 Wi-Fi 上极易花屏、丢包；
# 超时设 5 秒，避免断网时 read() 无限阻塞导致关窗口卡死。
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000"
)

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import theme
from ..widgets import motion
from ..widgets.glyph_icons import make_icon
from ..workers import run_async
from huawei_smartlife.camera import (
    CANDIDATE_PATHS,
    NvrSupport,
    RtspCamera,
    find_player,
    nvr_support,
    port_open,
    scan_lan,
)

_STAGE_HINT = {
    True: (
        "该型号开放 RTSP。请先在手机「华为智慧生活」App 里进入这台摄像头 → "
        "右上角 ∷ → 设置 →「硬盘录像机」→ 打开「连接外部硬盘录像机」，"
        "设一个独立账号密码（不是华为账号），再把下面的 IP / 账号 / 密码填进来。"
    ),
    False: (
        "该型号的实时画面走华为云私有加密通道，第三方无法在电脑上还原，"
        "这台只能继续用手机 App 观看。下面仍可手填——如果你确认它支持 RTSP。"
    ),
    None: (
        "无法从型号判断是否开放 RTSP。请在 App 里看摄像头设置中有没有"
        "「硬盘录像机」这一项：有就照着填，没有就是取不到流。"
    ),
}

_MANUAL_HINT = (
    "手动添加的网络摄像头：不按型号判断，只要对方开放 RTSP 就能看——"
    "华为、电信天翼看家、海康、大华都行。填 IP、账号、密码后点「连接」；"
    "不知道码流路径就点「识别码流」自动试。"
)


def _have_opencv() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺依赖只影响本窗口，不能让主程序崩
        return False
    return True


class _FrameGrabber(QThread):
    """后台读帧线程：唯一职责是把 BGR 帧转成 QImage 抛给界面。"""

    frame_ready = Signal(QImage)
    opened = Signal(int, int)  # 宽, 高
    failed = Signal(str)

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 - QThread 接口
        try:
            import cv2
        except Exception:  # noqa: BLE001
            self.failed.emit("未安装 opencv-python，无法解码视频流")
            return

        capture = None
        try:
            capture = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
            # 只要最新帧：不设的话内部排队会越积越多，画面延迟越拖越长
            try:
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:  # noqa: BLE001 - 部分后端不支持该属性
                pass
            if not capture.isOpened():
                self.failed.emit(
                    "打不开视频流：检查 IP 是否正确、是否同一局域网、"
                    "App 里「硬盘录像机」是否已开启且账号密码一致"
                )
                return

            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self.opened.emit(width, height)

            misses = 0
            while not self._stop:
                ok, frame = capture.read()
                if self._stop:
                    break
                if not ok or frame is None:
                    misses += 1
                    # 断流时 read 会快速返回 False，累计到阈值再判死
                    if misses > 60:
                        self.failed.emit("视频流中断：网络不稳或摄像头已断开")
                        return
                    self.msleep(60)
                    continue
                misses = 0
                height_now, width_now = frame.shape[:2]
                # QImage 不拷贝时指向 cv2 的缓冲区，下一帧就被覆盖 → 必花屏
                image = QImage(
                    frame.data,
                    width_now,
                    height_now,
                    frame.strides[0],
                    QImage.Format_BGR888,
                ).copy()
                self.frame_ready.emit(image)
        except Exception as error:  # noqa: BLE001 - 任何异常都只影响本窗口
            self.failed.emit(f"解码异常：{error}")
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:  # noqa: BLE001
                    pass


class FrameGrabber(_FrameGrabber):
    """给外部（挂件的快捷预览浮层）用的公开名字。

    两个窗口看的是同一路流，读帧参数（缓冲区只留最新帧、断流阈值）必须一致，
    所以只导出而不是各写一份——复制出去的版本迟早会跟这边跑偏。
    """


def have_opencv() -> bool:
    """公开别名：外部判断能不能内嵌播放，不必自己 import cv2 试一遍。"""
    return _have_opencv()


def _read_first_frame(url: str) -> tuple[int, int] | None:
    """打开流读一帧，返回 (宽, 高)；失败或没画面返回 None。"""
    import cv2

    capture = None
    try:
        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if capture.isOpened():
            ok, frame = capture.read()
            if ok and frame is not None:
                height, width = frame.shape[:2]
                return int(width), int(height)
    except Exception:  # noqa: BLE001 - 探测场景，任何异常都等同于"这条不通"
        pass
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:  # noqa: BLE001
                pass
    return None


def _grab_size(url: str, timeout: float = 8.0) -> tuple[int, int] | None:
    """带超时地试开一条流。

    OpenCV 打开不通的地址会一直等到内部 30 秒上限
    （``CAP_PROP_OPEN_TIMEOUT_MSEC`` 在 5.0 上不生效，实测无效），
    逐条探测时不能忍这个延迟，所以放到子线程里限时等待。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_read_first_frame, url)
    try:
        return future.result(timeout=timeout)
    except (FutureTimeout, Exception):  # noqa: BLE001
        return None
    finally:
        # wait=False：底层线程可能还卡在网络等待里，不能让它拖住调用方
        pool.shutdown(wait=False, cancel_futures=True)


class _StreamProbe(QThread):
    """依次试开候选通道，按分辨率判断哪条是主码流。

    这里刻意用「打开 + 读一帧」而不是自己拼 RTSP DESCRIBE：这台设备的鉴权是
    ``Digest realm="HIipCamera"``，手写认证头很容易被判 400（实测如此），
    而 cv2 走的 FFmpeg 已经处理好认证。用与实际播放完全相同的代码路径去探测，
    结论才不会和播放结果打架。
    """

    found = Signal(list)  # [(path, 宽, 高), ...] 只含真正出帧的通道
    progress = Signal(str)

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 554,
        paths: tuple[str, ...] = CANDIDATE_PATHS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._paths = tuple(paths)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 - QThread 接口
        try:
            import cv2  # noqa: F401
        except Exception:  # noqa: BLE001
            self.found.emit([])
            return

        # 端口不通就没必要逐条试：每条都要等满超时，纯浪费
        if not port_open(self._host, self._port, timeout=2.0):
            self.found.emit([])
            return

        results: list[tuple[str, int, int]] = []
        for path in self._paths:
            if self._stop:
                break
            self.progress.emit(f"正在试通道 {path} …")
            camera = RtspCamera(
                name="probe",
                host=self._host,
                username=self._username,
                password=self._password,
                port=self._port,
                path=path,
            )
            size = _grab_size(camera.rtsp_url)
            if size is not None:
                results.append((path, size[0], size[1]))
                # 主/子各一条就够判断了，继续试只会让用户干等
                if len(results) >= 2:
                    break

        if not self._stop:
            self.found.emit(results)


class VideoView(QWidget):
    """等比居中绘制一帧画面，四周留黑边。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        minimum: tuple[int, int] = (600, 338),
    ) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._hint = "尚未连接"
        # 完整窗口需要大画面；挂件的快捷预览是小浮层，尺寸得由调用方定
        self.setMinimumSize(*minimum)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.ArrowCursor)

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_hint(self, text: str) -> None:
        self._hint = text
        self._image = None
        self.update()

    def clear(self) -> None:
        self.set_hint("尚未连接")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0a0c10"))
        if self._image is not None and not self._image.isNull():
            scaled = self._image.size().scaled(
                self.size(), Qt.KeepAspectRatio
            )
            left = (self.width() - scaled.width()) // 2
            top = (self.height() - scaled.height()) // 2
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(QRect(left, top, scaled.width(), scaled.height()), self._image)
            return

        painter.setPen(QPen(QColor("#5a6272")))
        font = painter.font()
        font.setPointSize(10)
        painter.setFont(font)
        flags = int(Qt.AlignCenter) | int(Qt.TextWordWrap)
        painter.drawText(self.rect().adjusted(24, 24, -24, -24), flags, self._hint)


class CameraDialog(QDialog):
    """某台摄像头的实时画面窗口（非模态）。"""

    config_changed = Signal(str, dict)  # dev_id, config
    log = Signal(str, str)  # 文本, 等级

    def __init__(
        self,
        device=None,
        config: dict | None = None,
        parent: QWidget | None = None,
        *,
        manual_name: str = "",
        manual_key: str = "",
    ) -> None:
        """device 为 None 表示「手动添加的网络摄像头」。

        不传 device 就不做型号判定——这类摄像头可能压根不是华为生态的
        （电信天翼看家、海康、大华……），只要对方开放 RTSP 就能看，
        按华为型号去判断没有意义还会误导。
        """
        super().__init__(parent)
        self._device = device
        self._manual = device is None
        self._name = (device.name if device is not None else manual_name) or "网络摄像头"
        # 配置持久化用的 key：华为设备用 dev_id，手动添加用固定键
        self._key = (device.dev_id if device is not None else manual_key) or "manual"
        self._config = dict(config or {})
        self._grabber: _FrameGrabber | None = None
        self._drag_offset: QPoint | None = None
        self._frame_count = 0
        self._fps_from = time.monotonic()
        self._fps = 0.0
        self._size_text = "—"
        self._scanned = False
        self._probe: _StreamProbe | None = None
        # 识别出的通道：{"main": "onvif1", "sub": "onvif2"}，空表示按默认映射
        self._paths: dict[str, str] = {}
        self._slid_in = False

        self._support: NvrSupport = (
            nvr_support(device)
            if device is not None
            else NvrSupport(True, "手动添加：不按型号判断，填对地址与凭据即可")
        )

        self.setWindowTitle(f"实时画面 · {self._name}")
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowSystemMenuHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(760, 600)
        self._build()
        self._apply_config(self._config)
        self._refresh_tip()
        self._sync_external_button()
        if not _have_opencv():
            # 两种缺法要说清差别：能外挂播放器就指过去，两个都没有才提装包
            hint = (
                "未安装 opencv-python，无法在本窗口内播放。可点「外部播放器」用 "
                f"{os.path.basename(find_player() or 'VLC')} 观看。"
                if find_player()
                else "未安装 opencv-python，本窗口无法内嵌播放；也没有找到 VLC / ffplay / mpv。"
                "请执行 pip install opencv-python 后再试。"
            )
            self._set_status(hint, "warn")

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(14, 14, 14, 14)

        surface = QFrame()
        surface.setObjectName("DialogSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(Qt.black)
        surface.setGraphicsEffect(shadow)
        shell.addWidget(surface)

        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_titlebar())

        body = QVBoxLayout()
        body.setContentsMargins(18, 10, 18, 14)
        body.setSpacing(10)

        self._tip = QLabel("")
        self._tip.setObjectName("Caption")
        self._tip.setWordWrap(True)
        self._tip.setMinimumHeight(32)
        body.addWidget(self._tip)

        self._view = VideoView()
        self._view.set_hint("填好下面四项，点「连接」开始拉流")
        body.addWidget(self._view, 1)

        body.addLayout(self._build_status_row())
        body.addLayout(self._build_form())
        body.addLayout(self._build_actions())
        layout.addLayout(body, 1)

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("DialogTitleBar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 10, 0)

        title = QLabel("实时画面")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        close = QPushButton()
        close.setObjectName("TitleButtonClose")
        close.setFixedSize(32, 28)
        close.setFlat(True)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("关闭")
        close.setIconSize(QSize(15, 15))
        close.setIcon(make_icon("close", theme().palette.text_2, 15))
        close.clicked.connect(self.close)
        layout.addWidget(close)

        bar.mousePressEvent = self._bar_press  # type: ignore[method-assign]
        bar.mouseMoveEvent = self._bar_move  # type: ignore[method-assign]
        bar.mouseReleaseEvent = self._bar_release  # type: ignore[method-assign]
        return bar

    def _bar_press(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _bar_move(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _bar_release(self, event) -> None:
        self._drag_offset = None

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {theme().palette.text_3}; font-size: 11px;")
        row.addWidget(self._dot)

        self._status = QLabel("未连接")
        self._status.setObjectName("Caption")
        row.addWidget(self._status)
        row.addStretch(1)

        self._metrics = QLabel("")
        self._metrics.setObjectName("Caption")
        row.addWidget(self._metrics)
        return row

    def _build_form(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        self._host = QComboBox()
        self._host.setEditable(True)
        self._host.setMinimumWidth(180)
        self._host.lineEdit().setPlaceholderText("摄像头内网 IP，如 192.168.1.100")
        grid.addWidget(self._mini_label("IP"), 0, 0)
        grid.addWidget(self._host, 0, 1, 1, 3)

        self._user = QLineEdit()
        self._user.setPlaceholderText("硬盘录像机账号")
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText("硬盘录像机密码")

        self._stream = QComboBox()
        self._stream.addItem("子码流（省带宽）", "sub")
        self._stream.addItem("主码流（高清）", "main")

        grid.addWidget(self._mini_label("账号"), 1, 0)
        grid.addWidget(self._user, 1, 1)
        grid.addWidget(self._mini_label("密码"), 1, 2)
        grid.addWidget(self._password, 1, 3)
        grid.addWidget(self._mini_label("码流"), 2, 0)
        grid.addWidget(self._stream, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return grid

    def _sync_external_button(self) -> None:
        """按本机实际情况决定「外部播放器」按钮的去留。

        没装任何播放器时这个按钮点下去只有一个结果——报错。功能不可用就不要
        摆在界面上诱导点击；内嵌播放已经能看画面，不需要用户额外装东西。
        """
        exe = find_player()
        self._external.setVisible(bool(exe))
        if exe:
            self._external.setToolTip(f"用 {os.path.basename(exe)} 打开同一条流")
            self._external.setEnabled(True)

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._scan_button = QPushButton("扫描局域网")
        self._scan_button.setObjectName("Ghost")
        self._scan_button.setCursor(Qt.PointingHandCursor)
        self._scan_button.setToolTip("扫描本网段 554 端口，找出开放 RTSP 的摄像头")
        self._scan_button.clicked.connect(self._on_scan)
        row.addWidget(self._scan_button)

        self._probe_button = QPushButton("识别码流")
        self._probe_button.setObjectName("Ghost")
        self._probe_button.setCursor(Qt.PointingHandCursor)
        self._probe_button.setToolTip(
            "实测这台摄像头哪条通道能出画面，自动区分主码流与子码流"
            "（不同型号的通道编号不一样，猜不得）"
        )
        self._probe_button.clicked.connect(self._on_probe_streams)
        row.addWidget(self._probe_button)

        self._external = QPushButton("外部播放器")
        self._external.setObjectName("Ghost")
        self._external.setCursor(Qt.PointingHandCursor)
        self._external.setToolTip("用 VLC / ffplay / mpv 打开同一条流")
        self._external.clicked.connect(self._on_external)
        row.addWidget(self._external)

        self._snap = QPushButton("截图")
        self._snap.setObjectName("Ghost")
        self._snap.setCursor(Qt.PointingHandCursor)
        self._snap.setToolTip("把当前画面存成 PNG")
        self._snap.clicked.connect(self._on_snapshot)
        row.addWidget(self._snap)

        row.addStretch(1)

        self._stop_button = QPushButton("断开")
        self._stop_button.setObjectName("Ghost")
        self._stop_button.setCursor(Qt.PointingHandCursor)
        self._stop_button.clicked.connect(self.stop)
        row.addWidget(self._stop_button)

        self._connect_button = QPushButton("连接")
        self._connect_button.setCursor(Qt.PointingHandCursor)
        self._connect_button.clicked.connect(self._on_connect)
        row.addWidget(self._connect_button)
        return row

    def _mini_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Caption")
        label.setMinimumWidth(30)
        return label

    # ==================================================================
    # 配置
    # ==================================================================
    def _apply_config(self, config: dict) -> None:
        host = str(config.get("host") or "")
        if host:
            self._host.setCurrentText(host)
        self._user.setText(str(config.get("username") or ""))
        self._password.setText(str(config.get("password") or ""))
        stream = str(config.get("stream") or "sub")
        index = self._stream.findData(stream)
        self._stream.setCurrentIndex(index if index >= 0 else 0)
        # 上次「识别码流」的结果：不同型号通道编号不同，记住了才不用每次重扫
        paths = config.get("paths")
        if isinstance(paths, dict):
            self._paths = {
                str(key): str(value)
                for key, value in paths.items()
                if key in {"main", "sub"} and value
            }

    def config(self) -> dict:
        return {
            "host": self._host.currentText().strip(),
            "port": 554,
            "username": self._user.text().strip(),
            "password": self._password.text(),
            "stream": self._stream.currentData(),
            "paths": dict(self._paths),
        }

    def _refresh_tip(self) -> None:
        if self._manual:
            # 这类摄像头可能压根不是华为生态的，套用型号判定只会误导
            self._tip.setText(_MANUAL_HINT)
            self._tip.setStyleSheet(f"color: {theme().palette.text_2};")
            return
        self._tip.setText(_STAGE_HINT.get(self._support.ok, _STAGE_HINT[None]))
        colour = {
            True: theme().palette.text_2,
            False: theme().palette.warn,
            None: theme().palette.text_3,
        }.get(self._support.ok, theme().palette.text_3)
        self._tip.setStyleSheet(f"color: {colour};")

    def _set_status(self, text: str, level: str = "idle") -> None:
        palette = theme().palette
        colour = {
            "ok": palette.ok,
            "warn": palette.warn,
            "error": palette.danger,
            "idle": palette.text_3,
        }.get(level, palette.text_3)
        self._dot.setStyleSheet(f"color: {colour}; font-size: 11px;")
        self._status.setText(text)
        if level in {"warn", "error"}:
            self.log.emit(f"摄像头 {self._name}：{text}", level)

    # ==================================================================
    # 播放控制
    # ==================================================================
    def _current_camera(self) -> RtspCamera | None:
        cfg = self.config()
        if not cfg["host"]:
            self._set_status("先填摄像头 IP；不知道就点「扫描局域网」", "warn")
            return None
        return RtspCamera(
            name=self._name,
            host=cfg["host"],
            username=cfg["username"],
            password=cfg["password"],
            port=int(cfg["port"]),
            stream=cfg["stream"],
            path=self._paths.get(str(cfg["stream"])),
            device_id=self._device.dev_id if self._device is not None else None,
        )

    def _on_connect(self) -> None:
        camera = self._current_camera()
        if camera is None:
            return
        if not _have_opencv():
            if find_player():
                self._set_status(
                    "缺少 opencv-python，无法内嵌播放；可点「外部播放器」观看", "warn"
                )
            else:
                self._set_status(
                    "缺少 opencv-python，无法内嵌播放（本机也没有 VLC / ffplay / mpv）。"
                    "请执行 pip install opencv-python",
                    "warn",
                )
            return

        # 先问端口再开流：IP 写错或不同网段时，这一步 1.5 秒就能给出结论，
        # 而 cv2 要死等 30 秒才肯说"打不开"
        if not port_open(camera.host, camera.port, timeout=1.5):
            self._set_status(
                f"连不上 {camera.host}:{camera.port}：确认 IP 没写错、"
                "摄像头与电脑接在同一路由器（不同网段扫不到也连不通）",
                "error",
            )
            return

        self.stop()
        self._config = self.config()
        self.config_changed.emit(self._key, dict(self._config))

        self._view.set_hint("正在连接…\n首帧通常需要 1–3 秒")
        self._set_status(f"正在连接 {camera.masked_url}", "idle")
        self._metrics.setText("")
        self._frame_count = 0
        self._fps_from = time.monotonic()
        self._size_text = "—"

        self._grabber = _FrameGrabber(camera.rtsp_url, self)
        self._grabber.frame_ready.connect(self._on_frame)
        self._grabber.opened.connect(self._on_opened)
        self._grabber.failed.connect(self._on_failed)
        self._grabber.start()

    def stop(self) -> None:
        grabber = self._grabber
        self._grabber = None
        if grabber is None:
            return
        grabber.stop()
        # cv2 的 read() 可能正卡在网络等待里，等不到就只能放弃（daemon 线程不阻塞退出）
        if not grabber.wait(2000):
            grabber.terminate()
            grabber.wait(500)
        self._set_status("已断开", "idle")
        self._metrics.setText("")
        self._view.set_hint("已断开。点「连接」重新拉流")

    def _on_opened(self, width: int, height: int) -> None:
        self._size_text = f"{width}×{height}" if width and height else "未知分辨率"
        self._set_status("播放中", "ok")

    def _on_failed(self, message: str) -> None:
        self._set_status(message, "error")
        self._view.set_hint(f"{message}\n\n检查后点「连接」重试")
        self._metrics.setText("")

    def _on_frame(self, image: QImage) -> None:
        self._view.set_image(image)
        self._frame_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_from
        if elapsed >= 1.0:
            self._fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_from = now
            self._metrics.setText(f"{self._fps:.0f} fps · {self._size_text}")

    # ==================================================================
    # 辅助操作
    # ==================================================================
    def _on_scan(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        self._scan_button.setEnabled(False)
        self._scan_button.setText("扫描中…")
        self._set_status("正在扫描本网段 554 端口，约 2 秒…", "idle")

        def work():
            return scan_lan()

        def done(hosts) -> None:
            self._scanned = False
            self._scan_button.setEnabled(True)
            self._scan_button.setText("扫描局域网")
            hosts = list(hosts or [])
            if not hosts:
                self._set_status(
                    "没扫到开放 554 的地址：确认摄像头与电脑在同一路由器、同一网段",
                    "warn",
                )
                return
            current = self._host.currentText().strip()
            self._host.clear()
            self._host.addItems(hosts)
            if current in hosts:
                self._host.setCurrentText(current)
            self._set_status(
                f"发现 {len(hosts)} 个候选地址，已填入下拉框：{', '.join(hosts[:4])}",
                "ok",
            )

        def failed(error: BaseException) -> None:
            self._scanned = False
            self._scan_button.setEnabled(True)
            self._scan_button.setText("扫描局域网")
            self._set_status(f"扫描失败：{error}", "error")

        run_async(work, done, failed)

    # ------------------------------------------------------------------
    # 通道识别：不同型号的主/子码流路径不一样，只能实测
    # ------------------------------------------------------------------
    def _on_probe_streams(self) -> None:
        if self._probe is not None:
            return
        cfg = self.config()
        if not cfg["host"]:
            self._set_status("先填摄像头 IP；不知道就点「扫描局域网」", "warn")
            return
        if not _have_opencv():
            self._set_status("识别码流需要 opencv-python（本窗口的播放依赖）", "warn")
            return

        # 探测本身就要开流读帧，先断开在播的画面，免得两条连接抢带宽
        self.stop()

        self._probe_button.setEnabled(False)
        self._probe_button.setText("识别中…")
        self._set_status("正在逐条试通道，能出画面的会被记下来（约 5–20 秒）…", "idle")

        probe = _StreamProbe(
            cfg["host"],
            cfg["username"],
            cfg["password"],
            int(cfg["port"]),
            parent=self,
        )
        probe.progress.connect(lambda text: self._set_status(text, "idle"))
        probe.found.connect(self._on_probe_done)
        probe.finished.connect(self._on_probe_finished)
        self._probe = probe
        probe.start()

    def _on_probe_finished(self) -> None:
        self._probe = None
        self._probe_button.setEnabled(True)
        self._probe_button.setText("识别码流")

    def _stop_probe(self) -> None:
        probe = self._probe
        self._probe = None
        if probe is None:
            return
        probe.stop()
        # cv2 的 open/read 可能正卡在网络等待，等不到就放弃（daemon 语义）
        if not probe.wait(3000):
            probe.terminate()
            probe.wait(500)

    def _on_probe_done(self, results: list) -> None:
        if not results:
            self._set_status(
                "没有通道能出画面：确认用的是「硬盘录像机」那套账号密码、"
                "该开关已打开，且摄像头与电脑在同一路由器",
                "error",
            )
            return

        ordered = sorted(results, key=lambda item: item[1] * item[2], reverse=True)
        main_path, main_w, main_h = ordered[0]
        self._paths = {"main": main_path}
        # 只有一条通道能出画面时，主=子，不凭空编一个不存在的路径
        if len(ordered) > 1:
            self._paths["sub"] = ordered[-1][0]

        detail = " / ".join(f"{path} {w}×{h}" for path, w, h in ordered)
        self._set_status(f"识别完成：{detail}", "ok")
        self._config = self.config()
        self.config_changed.emit(self._key, dict(self._config))

    def _on_external(self) -> None:
        camera = self._current_camera()
        if camera is None:
            return
        if not find_player():
            # 按钮正常情况下已隐藏，这里兜住快捷键/程序化触发的路径
            self._set_status(
                "本机没装 VLC / ffplay / mpv。不用装——点「连接」就在本窗口内播放。",
                "warn",
            )
            return
        try:
            message = camera.launch_player()
        except RuntimeError as error:
            self._set_status(str(error), "error")
            return
        self._set_status(message, "ok")

    def _on_snapshot(self) -> None:
        image = self._view._image  # noqa: SLF001 - 同模块内的只读访问
        if image is None or image.isNull():
            self._set_status("当前没有画面可截", "warn")
            return
        folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        ))), "截图")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_name = "".join(c for c in self._name if c not in '\\/:*?"<>|').strip()
        path = os.path.join(folder, f"{safe_name or 'camera'}-{stamp}.png")
        if image.save(path, "PNG"):
            self._set_status(f"已保存：{path}", "ok")
        else:
            self._set_status("截图保存失败", "error")

    # ==================================================================
    # 窗口事件
    # ==================================================================
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        if not self._slid_in:
            self._slid_in = True
            motion.slide_in(self)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.stop()
        self._stop_probe()
        super().closeEvent(event)
