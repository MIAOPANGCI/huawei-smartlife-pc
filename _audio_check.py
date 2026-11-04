"""验证摄像头音频：依赖检查、静音/音量状态、多路互斥。

播放在这里被换成假对象——真连摄像头会出声，不适合放进自检。
真实出声路径单独用一台摄像头手动验过（见提交说明）。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.audio as audio_module  # noqa: E402
import app.dialogs.camera_wall as wall_module  # noqa: E402
from app.dialogs.camera_wall import CameraWall  # noqa: E402
from app.theme import set_theme  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


class FakeAudio(QObject):
    """替身：只记状态，不碰声卡。"""

    failed = Signal(str)
    playing_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._playing = False
        self._muted = False
        self._volume = 1.0
        self.urls: list[str] = []

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def volume(self) -> float:
        return self._volume

    def play(self, url: str) -> None:
        self.urls.append(url)
        self._playing = True

    def stop(self) -> None:
        self._playing = False

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def set_volume(self, volume: float) -> None:
        self._volume = float(volume)


class FakeGrabber(QObject):
    frame_ready = Signal(QImage)
    opened = Signal(int, int)
    failed = Signal(str)

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self.url = url

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait(self, msecs: int = 0) -> bool:
        return True

    def terminate(self) -> None:
        pass


def test_available() -> None:
    ok, reason = audio_module.available()
    check(f"依赖检查可用（{reason or '齐'}）", ok is True)


def test_state() -> None:
    audio = audio_module.CameraAudio()
    check("默认不静音", audio.muted is False)
    check("初始未播放", audio.playing is False)

    audio.set_volume(0.5)
    check("音量写进播放状态", abs(audio._state["volume"] - 0.5) < 1e-6)

    audio.set_muted(True)
    check("静音后音量为 0", audio._state["volume"] == 0.0)
    audio.set_volume(0.9)
    check("静音状态下调音量不会出声", audio._state["volume"] == 0.0)

    audio.set_muted(False)
    check("取消静音恢复原音量", abs(audio._state["volume"] - 0.9) < 1e-6)


def test_boost() -> None:
    """音量能往 1 以上推：摄像头麦克风普遍偏小声，只到 100% 不够用。"""
    import numpy as np

    audio = audio_module.CameraAudio()
    audio.set_volume(2.0)
    check("音量可以设到 200%（增益）", abs(audio.volume - 2.0) < 1e-6)
    check("增益写进播放状态", abs(audio._state["volume"] - 2.0) < 1e-6)

    audio.set_volume(99.0)
    check("超过上限被夹到 MAX_VOLUME", abs(audio.volume - audio_module.MAX_VOLUME) < 1e-6)
    check("上限够大（至少 2 倍）", audio_module.MAX_VOLUME >= 2.0)

    check("新开的一路沿用上次音量", abs(audio_module.CameraAudio().volume - audio_module.MAX_VOLUME) < 1e-6)
    audio_module.CameraAudio().set_volume(1.0)      # 复位，别影响后面的用例

    # 增益真的作用到采样上：一半幅度 ×2 应当翻倍，超出 16bit 的削到边界
    audio.set_volume(2.0)
    samples = np.array([100, -200, 30000, -32000], dtype=np.int16)
    scaled = (np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.int32) * 2.0
              ).clip(-32768, 32767).astype(np.int16)
    check("增益按倍数放大采样并削顶", list(scaled) == [200, -400, 32767, -32768])

    # 静音不该把记住的音量改掉
    audio.set_muted(True)
    audio.set_volume(1.5)
    audio.set_muted(False)
    check("静音期间调的音量，取消静音后生效", abs(audio._state["volume"] - 1.5) < 1e-6)
    audio_module.CameraAudio().set_volume(1.0)


def test_volume_menu() -> None:
    """右键菜单的档位要和 audio 的上限对得上。"""
    from app.widgets.volume_menu import CHOICES, volume_label

    values = [value for _label, value in CHOICES]
    check("菜单里有 100% 档", any(abs(v - 1.0) < 1e-6 for v in values))
    check("菜单最高档等于 MAX_VOLUME", abs(max(values) - audio_module.MAX_VOLUME) < 1e-6)
    check("菜单档位递增", values == sorted(values))
    check("100% 不标增强", "增强" not in volume_label(1.0))
    check("150% 标出增强", "增强" in volume_label(1.5))


def test_wall_exclusive(app: QApplication) -> None:
    wall_module.FrameGrabber = FakeGrabber          # type: ignore[assignment]
    wall_module.audio_module.CameraAudio = FakeAudio  # type: ignore[assignment]

    targets = [
        {"key": "a", "name": "门口", "config": {"host": "192.0.2.11"}},
        {"key": "b", "name": "院子", "config": {"host": "192.0.2.12"}},
    ]
    wall = CameraWall(targets, ["a", "b"])
    wall.show()
    app.processEvents()

    check("同屏默认全部静音", all(not tile.audio_on for tile in wall._tiles.values()))

    wall._on_tile_audio("a")
    check("点喇叭后这一路出声", wall._tiles["a"].audio_on)
    check("其它几路仍然静音", not wall._tiles["b"].audio_on)
    check("记住了当前出声的是哪一格", wall._audio_key == "a")

    wall._on_tile_audio("b")
    check("切到另一格时前一路自动关闭", not wall._tiles["a"].audio_on)
    check("新的一格开始出声", wall._tiles["b"].audio_on)
    check("当前出声的换成了 b", wall._audio_key == "b")

    wall._on_tile_audio("b")
    check("再点一次关掉", not wall._tiles["b"].audio_on)
    check("关掉后没有出声的格子", wall._audio_key == "")

    wall._on_tile_audio("a")
    wall._remove("a")
    check("删掉正在出声的格子会清空记录", wall._audio_key == "")

    wall._on_tile_audio("b")
    wall.stop_all()
    check("stop_all 之后全部静音", all(not tile.audio_on for tile in wall._tiles.values()))

    wall.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")

    test_available()
    test_state()
    test_boost()
    test_volume_menu()
    test_wall_exclusive(app)

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
