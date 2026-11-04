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


class _AudioWorker(QThread):
    """解码 + 播放循环。stop 是协作式的：每帧检查一次标志。"""

    failed = Signal(str)
    ready = Signal(int)          # 实际使用的采样率

    def __init__(self, url: str, state: dict, parent=None) -> None:
        super().__init__(parent)
        self._url = url
        self._state = state      # {"stop": bool, "volume": float}
        self._container = None

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
                    blocksize=1024,
                )
            except Exception as error:  # noqa: BLE001 - 设备被占用
                self.failed.emit(f"打不开音频设备：{error}")
                return

            with output:
                self.ready.emit(SAMPLE_RATE)
                while not self._state["stop"]:
                    decoded = False
                    for frame in self._container.decode(audio_stream):
                        decoded = True
                        if self._state["stop"]:
                            break
                        for chunk in resampler.resample(frame):
                            self._write(output, bytes(chunk.planes[0]))
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

    def _write(self, stream, data: bytes) -> None:
        volume = float(self._state.get("volume", 1.0))
        if volume <= 0.001:
            # 静音也要照常写入（零数据），否则流会积压、恢复时一下涌出来
            stream.write(b"\x00" * len(data))
            return
        if volume < 0.999:
            try:
                import numpy as np

                samples = np.frombuffer(data, dtype=np.int16).astype(np.int32)
                samples = (samples * volume).clip(-32768, 32767).astype(np.int16)
                data = samples.tobytes()
            except Exception:  # noqa: BLE001 - 没有 numpy 就按原音量播
                pass
        stream.write(data)

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
        self._volume = 0.8
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
        self._volume = max(0.0, min(1.0, float(volume)))
        if not self._muted:
            self._state["volume"] = self._volume

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
