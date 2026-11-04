"""喇叭按钮上的右键音量菜单。

单击喇叭还是原来的"静音开关"，右键才弹这个。

为什么需要它：摄像头麦克风普遍偏小（G.711 又是窄带语音），只靠"静音 / 不静音"
两档根本不够——系统音量拉到 100% 还是听不清屋里说话。所以这里能往上推增益。
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu, QWidget

from ..audio import MAX_VOLUME

# (标签, 倍数)。超过 100% 是软件增益，200% 以上人声开始削顶发闷，封在 300%。
CHOICES: tuple[tuple[str, float], ...] = (
    ("30%", 0.3),
    ("50%", 0.5),
    ("100%（原始音量）", 1.0),
    ("150%", 1.5),
    ("200%", 2.0),
    (f"{round(MAX_VOLUME * 100)}%（最大）", MAX_VOLUME),
)


def show_volume_menu(anchor: QWidget, current: float, on_pick) -> None:
    """在 anchor 下方弹出音量档位菜单，选中后回调 on_pick(倍数)。"""
    menu = QMenu(anchor)
    for label, value in CHOICES:
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(abs(current - value) < 0.01)
        action.triggered.connect(lambda _checked=False, v=value: on_pick(v))
    menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))


def volume_label(volume: float) -> str:
    """100% 以上标出"增强"，让用户知道自己在推增益而不是调系统音量。"""
    percent = round(volume * 100)
    return f"音量 {percent}%" + ("（增强）" if volume > 1.001 else "")
