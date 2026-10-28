"""不吃滚轮的控件。

设备详情窗放在 ``QScrollArea`` 里。用户想滚动页面时，鼠标很容易掠过滑块，
而 Qt 默认让滚轮直接改滑块的值——这个界面里滑块一动就会**立刻下发命令到设备**
（比如把空调从 26℃ 调到 30℃），属于不该发生的误操作。

处理方式：收到滚轮就把它**转发给外层滚动区**，页面照常滚。
只写 ``event.ignore()`` 不够：Qt 的冒泡只在特定事件路径下生效，实测直接把事件
投递给控件时，父级滚动区不会动（滚动条一直停在 0）。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QSlider,
    QWidget,
)


class _NoWheelMixin:
    """把滚轮交给外层滚动区。"""

    def _scroll_area(self) -> QAbstractScrollArea | None:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        area = self._scroll_area()
        if area is None:
            # 不在滚动容器里就干脆不管滚轮，避免意外改值
            event.ignore()
            return
        QApplication.sendEvent(area.viewport(), event)
        event.accept()


class NoWheelSlider(_NoWheelMixin, QSlider):
    """滚轮不调值的滑块。"""


class NoWheelComboBox(_NoWheelMixin, QComboBox):
    """滚轮不改选中项的下拉框。

    QComboBox 在拿到焦点时也会吃滚轮，滚页面时把「服务」「接收方式」这类选项
    换掉同样恼火——换了就得重新选，顺序错位后还容易点错。
    """
