"""多路同屏：一个窗口里同时看多台摄像头。

每格是独立的 FrameGrabber + VideoView（读帧参数和单台窗口完全一致，都是
camera_dialog 导出的那一套），所以一台断流、卡住或没配置，都只影响自己那一格。

格数按 1 → 2 → 4 → 6 → 9 排布（列数取 √n 向上取整），最多 9 路。
路数越多越吃 CPU：每路都是独立的解码线程，都是用子码流（默认）省带宽。
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.camera import RtspCamera

from .. import audio as audio_module
from ..theme import theme
from ..widgets.glyph_icons import make_icon
from .camera_dialog import FrameGrabber, VideoView, have_opencv

MAX_TILES = 9


def _rtsp_url(target: dict) -> str | None:
    """从配置拼出 RTSP 地址；没填 host 返回 None（这一格显示去配置的提示）。"""
    config = target.get("config") or {}
    host = str(config.get("host") or "").strip()
    if not host:
        return None
    stream = str(config.get("stream") or "sub")
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    camera = RtspCamera(
        name=str(target.get("name") or "摄像头"),
        host=host,
        username=str(config.get("username") or ""),
        password=str(config.get("password") or ""),
        port=int(config.get("port") or 554),
        stream=stream,
        path=str(paths.get(stream)) if paths.get(stream) else None,
    )
    return camera.rtsp_url


class _Tile(QFrame):
    """一格画面：顶部名字条 + 状态，下面是 VideoView。"""

    remove_requested = Signal(str)
    double_clicked = Signal()
    audio_toggled = Signal(str)

    def __init__(self, target: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraTile")
        # QWidget/QFrame 默认不绘制样式表里的 background，必须显式打开，
        # 否则名字条是透明的——文字看着就像被压暗了一层
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._target = target
        self._key = str(target.get("key") or "")
        self._grabber: FrameGrabber | None = None
        self._audio = audio_module.CameraAudio(self)
        self._audio.failed.connect(self._on_audio_failed)
        self._frames = 0
        self._fps = 0.0
        self._fps_from = time.monotonic()
        self._size_text = "—"
        self._build()

    # ---- 构建 ----
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("TileBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)   # 同上：不然背景不画
        bar.setFixedHeight(30)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(10, 0, 6, 0)
        bar_layout.setSpacing(6)

        self._name = QLabel(str(self._target.get("name") or "摄像头"))
        self._name.setObjectName("TileName")
        bar_layout.addWidget(self._name)
        bar_layout.addStretch(1)

        self._status = QLabel("连接中…")
        self._status.setObjectName("TileStatus")
        bar_layout.addWidget(self._status)

        self._audio_btn = QPushButton()
        self._audio_btn.setObjectName("TileClose")
        self._audio_btn.setFixedSize(20, 20)
        self._audio_btn.setCursor(Qt.PointingHandCursor)
        self._audio_btn.setIconSize(QSize(13, 13))
        self._audio_btn.setToolTip("听这一路的声音")
        self._audio_btn.clicked.connect(lambda: self.audio_toggled.emit(self._key))
        ok, reason = audio_module.available()
        if ok:
            bar_layout.addWidget(self._audio_btn)
            self._refresh_audio_icon()
        else:
            self._audio_btn.hide()
            self._audio_btn.setToolTip(reason)

        remove = QPushButton("×")
        remove.setObjectName("TileClose")
        remove.setFixedSize(20, 20)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip("从同屏里移除")
        remove.clicked.connect(lambda: self.remove_requested.emit(self._key))
        bar_layout.addWidget(remove)
        layout.addWidget(bar)

        self._view = VideoView(minimum=(200, 112))
        self._view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._view.installEventFilter(self)      # 双击任一格都切整窗全屏
        layout.addWidget(self._view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_fps)
        self._timer.start()

    # ---- 播放 ----
    def start(self) -> None:
        self.stop()
        url = _rtsp_url(self._target)
        if url is None:
            self._view.set_hint("还没填地址\n右键设备 → 实时画面，配好再回来")
            self._status.setText("未配置")
            self._status.setProperty("state", "warn")
            self._restyle(self._status)
            return

        if not have_opencv():
            self._view.set_hint("缺少 opencv-python")
            self._status.setText("无解码器")
            return

        self._view.set_hint("正在连接…\n首帧 1–3 秒")
        self._status.setText("连接中")
        grabber = FrameGrabber(url, self)
        grabber.frame_ready.connect(self._on_frame)
        grabber.opened.connect(self._on_opened)
        grabber.failed.connect(self._on_failed)
        self._grabber = grabber
        grabber.start()

    def stop(self) -> None:
        self._timer.stop()
        self._audio.stop()
        grabber = self._grabber
        self._grabber = None
        if grabber is None:
            return
        grabber.stop()
        # 多路一起关时别让每一路都等满 2 秒
        if not grabber.wait(1500):
            grabber.terminate()
            grabber.wait(300)
        grabber.deleteLater()

    # ---- 回调 ----
    def _on_frame(self, image: QImage) -> None:
        self._view.set_image(image)
        self._frames += 1

    def _on_opened(self, width: int, height: int) -> None:
        self._size_text = f"{width}×{height}" if width and height else "—"
        self._view.set_hint("")
        self._status.setText("在线")
        self._restyle(self._status)

    def _on_failed(self, message: str) -> None:
        short = message.split("\n")[0][:40]
        self._view.set_hint(short)
        self._status.setText("断开")
        self._restyle(self._status)

    def _refresh_fps(self) -> None:
        now = time.monotonic()
        span = now - self._fps_from
        if span <= 0:
            return
        self._fps = self._frames / span
        self._frames = 0
        self._fps_from = now
        if self._status.text() == "在线":
            self._status.setText(f"{self._fps:.0f} fps · {self._size_text}")

    def _restyle(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ---- 声音 ----
    @property
    def audio_on(self) -> bool:
        return self._audio.playing

    def set_audio(self, on: bool) -> None:
        """开/关这一路的声音。多路同屏里由窗口保证同时只有一路在响。"""
        if not on:
            self._audio.stop()
            self._refresh_audio_icon()
            return
        url = _rtsp_url(self._target)
        if url is None:
            self._audio_btn.setEnabled(False)
            self._audio_btn.setToolTip("这路还没填地址")
            return
        ok, reason = audio_module.available()
        if not ok:
            self._audio_btn.setEnabled(False)
            self._audio_btn.setToolTip(reason)
            return
        self._audio.play(url)
        self._refresh_audio_icon()

    def _refresh_audio_icon(self) -> None:
        on = self.audio_on
        self._audio_btn.setIcon(
            make_icon("sound" if on else "mute", theme().palette.text_2, 13)
        )
        self._audio_btn.setToolTip("关掉这一路的声音" if on else "听这一路的声音")

    def _on_audio_failed(self, message: str) -> None:
        # 声音失败不影响画面，标在按钮上就行
        self._audio_btn.setEnabled(False)
        self._audio_btn.setToolTip(message)
        self._refresh_audio_icon()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        if obj is self._view and event.type() == QEvent.MouseButtonDblClick:
            self.double_clicked.emit()
            return True
        return super().eventFilter(obj, event)


class CameraWall(QDialog):
    """多路同屏窗口。"""

    selection_changed = Signal(list)

    def __init__(
        self,
        targets: list[dict],
        selection: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._targets = [dict(item) for item in targets]
        self._tiles: dict[str, _Tile] = {}
        self._placeholder: QLabel | None = None
        # 同一时刻只允许一路出声（再多就是噪音了）
        self._audio_key = ""
        self._drag_offset = None
        available = {str(item.get("key")) for item in self._targets}
        # 上次的选择可能包含已经删掉的摄像头，过滤掉
        picked = [key for key in (selection or []) if key in available]
        self._selection = picked[:MAX_TILES] if picked else self._default_selection()

        self.setWindowTitle("多路同屏")
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowSystemMenuHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._build()
        self._relayout()

    # ---- 构建 ----
    def _default_selection(self) -> list[str]:
        """首次打开：挑已填地址的，最多 4 路。"""
        configured = [
            str(item.get("key"))
            for item in self._targets
            if str((item.get("config") or {}).get("host") or "").strip()
        ]
        return configured[:4] or [str(item.get("key")) for item in self._targets[:1]]

    def _build(self) -> None:
        self._shell = QVBoxLayout(self)
        self._shell.setContentsMargins(14, 14, 14, 14)

        surface = QFrame()
        surface.setObjectName("DialogSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(Qt.black)
        surface.setGraphicsEffect(shadow)
        self._shadow = shadow
        self._shell.addWidget(surface)

        outer = QVBoxLayout(surface)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._titlebar = self._build_titlebar()
        outer.addWidget(self._titlebar)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(12, 12, 12, 12)
        self._grid.setSpacing(10)
        outer.addWidget(self._grid_host, 1)

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("DialogTitleBar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 10, 0)
        layout.setSpacing(8)

        self._title = QLabel("多路同屏")
        self._title.setObjectName("DialogTitle")
        layout.addWidget(self._title)
        layout.addStretch(1)

        self._add_btn = QPushButton("+ 添加")
        self._add_btn.setObjectName("Ghost")
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.setToolTip(f"加一路，最多 {MAX_TILES} 路")
        self._add_btn.clicked.connect(self._show_add_menu)
        layout.addWidget(self._add_btn)

        full = QPushButton()
        full.setObjectName("TitleButton")
        full.setFixedSize(32, 28)
        full.setFlat(True)
        full.setCursor(Qt.PointingHandCursor)
        full.setToolTip("全屏（双击画面也可以）")
        full.setIconSize(QSize(15, 15))
        full.setIcon(make_icon("maximize", theme().palette.text_2, 15))
        full.clicked.connect(self._toggle_fullscreen)
        self._full_btn = full
        layout.addWidget(full)

        close = QPushButton()
        close.setObjectName("TitleButtonClose")
        close.setFixedSize(32, 28)
        close.setFlat(True)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("关闭")
        close.setIconSize(self._icon_size())
        close.setIcon(make_icon("close", theme().palette.text_2, 15))
        close.clicked.connect(self.close)
        layout.addWidget(close)

        bar.mousePressEvent = self._bar_press
        bar.mouseMoveEvent = self._bar_move
        bar.mouseReleaseEvent = self._bar_release
        return bar

    @staticmethod
    def _icon_size() -> QSize:
        return QSize(15, 15)

    # ---- 全屏 ----
    def _toggle_fullscreen(self) -> None:
        """整窗全屏：收起标题栏，格子按比例铺满整块屏幕。"""
        full = not self.isFullScreen()
        self._titlebar.setVisible(not full)
        margins = (0, 0, 0, 0) if full else (14, 14, 14, 14)
        self._shell.setContentsMargins(*margins)
        self._shadow.setEnabled(not full)
        self._full_btn.setIcon(
            make_icon("restore" if full else "maximize", theme().palette.text_2, 15)
        )
        self._full_btn.setToolTip(
            "退出全屏（Esc）" if full else "全屏（双击画面也可以）"
        )
        if full:
            self.showFullScreen()
        else:
            self.showNormal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        if event.key() == Qt.Key_Escape:
            if self.isFullScreen():
                self._toggle_fullscreen()
            # 非全屏也吃掉 ESC：默认行为是关窗，误触就把整屏画面关了
            return
        super().keyPressEvent(event)

    def _bar_press(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def _bar_move(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _bar_release(self, event) -> None:
        self._drag_offset = None

    # ---- 网格 ----
    def _relayout(self) -> None:
        """按当前选择重排格子。

        只做增量的部分：删掉的停下并销毁，新加的建出来开局，**已经在放的那几路
        只是挪个位置**——早先的写法是全部销毁重建，等于每加/删一路就把所有摄像头
        重连一遍，看着就像全在闪。
        """
        by_key = {str(item.get("key")): item for item in self._targets}
        wanted = [key for key in self._selection if key in by_key]

        # 1. 移除不再需要的格子（只停这一个）
        for key in list(self._tiles):
            if key not in wanted:
                tile = self._tiles.pop(key)
                tile.stop()
                tile.setParent(None)
                tile.deleteLater()

        if not wanted:
            self._title.setText("多路同屏")
            self.resize(520, 260)
            self._show_placeholder()
            return
        self._clear_placeholder()

        # 2. 新加的格子：先建好，排完版再开局
        fresh: list[str] = []
        for key in wanted:
            if key not in self._tiles:
                tile = _Tile(by_key[key], self._grid_host)
                tile.remove_requested.connect(self._remove)
                tile.double_clicked.connect(self._toggle_fullscreen)
                tile.audio_toggled.connect(self._on_tile_audio)
                self._tiles[key] = tile
                fresh.append(key)

        columns = max(1, math.ceil(math.sqrt(len(wanted))))
        rows = max(1, math.ceil(len(wanted) / columns))
        self._layout_tiles(wanted, columns)

        for key in fresh:
            self._tiles[key].start()

        self._title.setText(f"多路同屏 · {len(wanted)} 路")
        # 单格至少 480×270（16:9），再小就看不清车牌/人脸这类细节了
        count = len(wanted)
        tile_w = 480 if count > 1 else 820
        tile_h = 270 if count > 1 else 462
        self.resize(
            min(1800, columns * tile_w + (columns - 1) * 10 + 24 + 28),
            min(1150, rows * tile_h + (rows - 1) * 10 + 24 + 44 + 28),
        )

    def _layout_tiles(self, wanted: list[str], columns: int) -> None:
        """把格子摆进网格。

        QGridLayout 没有"删掉某一行"的接口，而 ``setRowStretch`` 会把网格撑到
        那个行号。所以每次重排都要**把已有行的 stretch 全部归零**再按新行数设 1：
        腾空的行 stretch 为 0、高度就是 0，不会继续分走空间——4 路删到 2 路时
        画面被挤成一条窄带、下面空一大片，根因就是旧的 stretch 一直留着。
        """
        grid = self._grid
        for tile in self._tiles.values():
            grid.removeWidget(tile)

        for index in range(max(grid.rowCount(), MAX_TILES)):
            grid.setRowStretch(index, 0)
        for index in range(max(grid.columnCount(), MAX_TILES)):
            grid.setColumnStretch(index, 0)

        for index, key in enumerate(wanted):
            grid.addWidget(self._tiles[key], index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for row in range(max(1, math.ceil(len(wanted) / columns))):
            grid.setRowStretch(row, 1)

    def _show_placeholder(self) -> None:
        if self._placeholder is not None:
            return
        label = QLabel("还没有可看的摄像头\n点右上角「+ 添加」补一路")
        label.setObjectName("Caption")
        label.setAlignment(Qt.AlignCenter)
        self._grid.addWidget(label, 0, 0)
        self._placeholder = label

    def _clear_placeholder(self) -> None:
        if self._placeholder is None:
            return
        self._placeholder.setParent(None)
        self._placeholder.deleteLater()
        self._placeholder = None

    # ---- 选择 ----
    def _show_add_menu(self) -> None:
        if len(self._selection) >= MAX_TILES:
            self._add_btn.setToolTip(f"最多 {MAX_TILES} 路")
            return
        remaining = [
            item
            for item in self._targets
            if str(item.get("key")) not in self._selection
        ]
        menu = QMenu(self)
        if not remaining:
            action = menu.addAction("没有其它摄像头了")
            action.setEnabled(False)
        for item in remaining:
            name = str(item.get("name") or "摄像头")
            configured = bool(str((item.get("config") or {}).get("host") or "").strip())
            label = name if configured else f"{name}（未配置）"
            menu.addAction(label, lambda k=str(item.get("key")): self._add(k))
        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _add(self, key: str) -> None:
        if not key or key in self._selection or len(self._selection) >= MAX_TILES:
            return
        self._selection.append(key)
        self._relayout()
        self.selection_changed.emit(list(self._selection))

    def _remove(self, key: str) -> None:
        if key in self._selection:
            self._selection.remove(key)
            if self._audio_key == key:
                self._audio_key = ""
            self._relayout()
            self.selection_changed.emit(list(self._selection))

    def _on_tile_audio(self, key: str) -> None:
        """点某格的喇叭：要么关掉它，要么把它打开、同时把别的关掉。

        几台一起响根本分不清是哪来的声音，还会盖住你要听的那一路。
        """
        tile = self._tiles.get(key)
        if tile is None:
            return
        if tile.audio_on:
            tile.set_audio(False)
            if self._audio_key == key:
                self._audio_key = ""
            return
        for other_key, other in self._tiles.items():
            if other_key != key:
                other.set_audio(False)
        tile.set_audio(True)
        self._audio_key = key if tile.audio_on else ""

    # ---- 生命周期 ----
    def selection(self) -> list[str]:
        return list(self._selection)

    def stop_all(self) -> None:
        self._audio_key = ""
        for tile in self._tiles.values():
            tile.set_audio(False)
            tile.stop()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.stop_all()
        super().closeEvent(event)
