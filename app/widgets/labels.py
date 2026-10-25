"""文本小组件：按控件宽度自动省略的 QLabel。

QLabel 本身不会省略长文本，只能被截断或撑宽。设备名称长度差异很大
（"热水器" vs "HUAWEI 路由器 BE7 Pro"），用固定宽度 QLabel 会把电源按钮
挤出卡片，所以这里按实际像素宽度做 elide。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QWidget


class ElidedLabel(QLabel):
    """超出宽度时显示省略号，完整文本放在 tooltip。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        super().setText(text)
        self.setToolTip(text)

    # 覆盖 setText：记住全文，再按当前宽度省略
    def setText(self, text: str) -> None:  # noqa: N802 - Qt 接口
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elide()

    @property
    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 8 or not self._full_text:
            super().setText(self._full_text)
            return
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.ElideRight, width)
        super().setText(elided)
