"""摄像头声音：单独解 RTSP 里的音轨并播放。

为什么要另起一路
----------------
``cv2.VideoCapture`` 只解视频，音轨它不碰。所以音频用 PyAV 再打开一次同一个
RTSP 地址（RTSP 是允许同一账号多路并发的），只取音频轨，重采样成 16bit/48kHz
立体声，交给 sounddevice 输出。

为什么不用 QtMultimedia：它属于 PySide6-Addons，本项目只装 Essentials，
为了一段 G.711 音频多捆一百多 MB 不划算；PyAV + sounddevice 加起来约 40MB。

音画不同步是可以接受的：监控场景主要听声音，画面另有 cv2 那一路，两者独立解码，
本来就会有 1 秒上下的差异。强行对齐需要自己写缓冲区，收益不值这份复杂度。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, Signal

SAMPLE_RATE = 48000
CHANNELS = 2

PREROLL_SECONDS = 0.25
"""起播前先攒这么多音频再放。用来吸收网络抖动（实测帧是成批到的）。"""

MAX_BUFFER_SECONDS = 0.60
"""积压上限。超过就丢最旧的，避免延迟越累积越大。

实测这路流的最大抖动是 215ms，0.6 秒有三倍余量；再大就只是白白加延迟
（1.5 秒时声音比画面晚一秒半，听着很别扭）。"""

# 软件增益上限。摄像头麦克风普遍偏小，G.711 又是窄带语音，实测本机两台在
# 100% 下都偏轻，150%–200% 比较合适。再往上人声开始削顶发闷，所以封在 3 倍。
MAX_VOLUME = 3.0

# 本次运行内共享：在一格上把音量调大了，换一格、开单台窗口也应该沿用，
# 不然每开一个新画面都要重新调一遍。
_shared_volume = 1.0


def shared_volume() -> float:
    return _shared_volume


def remember_volume(value: float) -> None:
    global _shared_volume
    _shared_volume = max(0.0, min(MAX_VOLUME, float(value)))


def available() -> tuple[bool, str]:
    """依赖是否齐。返回 (可用, 缺什么)。"""
    try:
        import av  # noqa: F401
    except ImportError:
        return False, "缺少 PyAV，无法解码音轨（pip install av）"
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        return False, "缺少 sounddevice，无法播放声音（pip install sounddevice）"
    try:
        import sounddevice as sd

        sd.query_devices(kind="output")
    except Exception:  # noqa: BLE001 - 没有声卡 / 被独占
        return False, "没有可用的音频输出设备"
    return True, ""


def _scale(data: bytes, volume: float) -> bytes:
    """按音量缩放 16bit 采样；没有 numpy 就原样返回。"""
    try:
        import numpy as np
    except ImportError:
        return data
    samples = np.frombuffer(data, dtype=np.int16).astype(np.int32)
    return (samples * volume).clip(-32768, 32767).astype(np.int16).tobytes()


class _AudioWorker(QThread):
    """解码线程 + 声卡回调。

    播放不是"解一块写一块"。实测这两路音频的帧是**成批到达**的：中位间隔 0ms，
    P99 到 130ms，最大 215ms——网络包是一阵一阵来的。原来直接 write() 给声卡，
    两批之间就是一段空白，听感就是一顿一顿（断音）。

    现在解码只负责往缓冲区塞，由声卡回调按时来取，取不到就补静音；再加 300ms
    预缓冲把网络抖动吸收掉。代价是声音比画面又晚了 0.3 秒，监控场景可以接受。
    """

    failed = Signal(str)
    ready = Signal(int)          # 实际使用的采样率

    def __init__(self, url: str, state: dict, parent=None) -> None:
        super().__init__(parent)
        self._url = url
        self._state = state      # {"stop": bool, "volume": float}
        self._container = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._preroll = int(SAMPLE_RATE * PREROLL_SECONDS) * CHANNELS * 2
        self._prerolled = False

    # ---- 声卡回调（在 PortAudio 的线程里，不能抛异常）----
    def _callback(self, outdata, frames, time_info, status) -> None:
        need = frames * CHANNELS * 2
        data = b""
        with self._lock:
            buffer = self._buffer
            if len(buffer) >= self._preroll:
                self._prerolled = True
            if self._prerolled and len(buffer) >= need:
                data = bytes(buffer[:need])
                del buffer[:need]
            elif self._prerolled:
                # 供给断了一下：补静音。宁可短暂无声，也别重播上一块——那是爆音
                data = bytes(buffer) + b"\x00" * (need - len(buffer))
                buffer.clear()
        if not data:
            # 还在攒预缓冲，先出静音
            data = b"\x00" * need
        volume = float(self._state.get("volume", 1.0))
        if abs(volume - 1.0) > 0.001:
            data = _scale(data, volume)
        outdata[:] = data

    def run(self) -> None:  # noqa: D401 - QThread 接口
        try:
            import av
            import sounddevice as sd
        except ImportError as error:
            self.failed.emit(f"缺少音频依赖：{error}")
            return

        try:
            self._container = av.open(
                self._url, options={"rtsp_transport": "tcp"}, timeout=8
            )
        except Exception as error:  # noqa: BLE001 - 网络 / 认证失败
            self.failed.emit(f"打不开音频通道：{error}")
            return

        try:
            audio_stream = next(
                (item for item in self._container.streams if item.type == "audio"),
                None,
            )
            if audio_stream is None:
                self.failed.emit("这路流没有音轨（很多机型只推视频）")
                return

            resampler = av.AudioResampler(
                format="s16", layout="stereo", rate=SAMPLE_RATE
            )
            try:
                output = sd.RawOutputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=0,        # 让 PortAudio 按设备自己挑
                    latency="high",     # 缓冲给厚一点，抗网络抖动
                    callback=self._callback,
                )
            except Exception as error:  # noqa: BLE001 - 设备被占用
                self.failed.emit(f"打不开音频设备：{error}")
                return

            cap = int(SAMPLE_RATE * MAX_BUFFER_SECONDS) * CHANNELS * 2
            with output:
                self.ready.emit(SAMPLE_RATE)
                while not self._state["stop"]:
                    decoded = False
                    for frame in self._container.decode(audio_stream):
                        decoded = True
                        if self._state["stop"]:
                            break
                        for chunk in resampler.resample(frame):
                            data = bytes(chunk.planes[0])
                            if not data:
                                continue
                            with self._lock:
                                self._buffer.extend(data)
                                overflow = len(self._buffer) - cap
                                if overflow > 0:
                                    # 积压太多说明网络远远跟不上。丢掉最旧的，
                                    # 否则延迟一路累积，声音会越来越晚
                                    del self._buffer[:overflow]
                    if not decoded:
                        raise RuntimeError("音频流已结束")
        except Exception as error:  # noqa: BLE001 - 断流等
            if not self._state["stop"]:
                self.failed.emit(f"音频中断：{error}")
        finally:
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:  # noqa: BLE001
                    pass
                self._container = None

    def request_stop(self) -> None:
        self._state["stop"] = True


class CameraAudio(QObject):
    """一路摄像头的音频播放器（复用同一个 worker）。"""

    failed = Signal(str)
    playing_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = {"stop": True, "volume": 1.0}
        self._worker: _AudioWorker | None = None
        self._muted = False
        # 沿用本次运行里上次调好的音量（默认 100%）
        self._volume = shared_volume()
        self._lock = threading.Lock()

    # ---- 状态 ----
    @property
    def playing(self) -> bool:
        worker = self._worker
        return worker is not None and worker.isRunning()

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._state["volume"] = 0.0 if self._muted else self._volume

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(MAX_VOLUME, float(volume)))
        remember_volume(self._volume)
        if not self._muted:
            self._state["volume"] = self._volume

    @property
    def volume(self) -> float:
        return self._volume

    # ---- 播放 ----
    def play(self, url: str) -> None:
        self.stop()
        ok, reason = available()
        if not ok:
            self.failed.emit(reason)
            return

        self._state["stop"] = False
        self._state["volume"] = 0.0 if self._muted else self._volume
        worker = _AudioWorker(url, self._state, self)
        worker.ready.connect(lambda _rate: self.playing_changed.emit(True))
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        worker.start()

    def stop(self) -> None:
        with self._lock:
            worker = self._worker
            self._worker = None
        self._state["stop"] = True
        if worker is None:
            return
        worker.request_stop()
        # decode 每帧才检查标志，正常情况下几十毫秒就退出来了
        if not worker.wait(1500):
            worker.terminate()
            worker.wait(300)
        worker.deleteLater()
        self.playing_changed.emit(False)

    def _on_worker_failed(self, message: str) -> None:
        self.playing_changed.emit(False)
        self.failed.emit(message)
