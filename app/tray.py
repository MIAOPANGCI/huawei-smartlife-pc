"""系统托盘：图标、右键菜单、左键弹出设备面板。

托盘在部分环境（无桌面会话、远程终端）不可用，``available`` 为 False 时
调用方会跳过「关闭到托盘」等逻辑，不影响主程序。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayController(QObject):
    """封装 QSystemTrayIcon，把交互转成信号。"""

    open_panel = Signal()
    open_main = Signal()
    open_board = Signal()
    quit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("华为智慧生活 · PC 控制台")

        menu = QMenu()
        menu.addAction("打开主界面", self.open_main.emit)
        menu.addAction("托盘面板", self.open_panel.emit)
        menu.addAction("桌面挂件", self.open_board.emit)
        menu.addSeparator()
        menu.addAction("退出", self.quit_requested.emit)
        self._menu = menu
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_activated)

    # ---------- 生命周期 ----------
    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        if self.available:
            self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def show_message(self, title: str, message: str) -> None:
        if self.available:
            self._tray.showMessage(title, message, QSystemTrayIcon.Information, 3000)

    # ---------- 交互 ----------
    def _on_activated(self, reason) -> None:  # noqa: ANN001 - Qt 枚举
        if reason in (
            QSystemTrayIcon.Trigger,
            QSystemTrayIcon.DoubleClick,
            QSystemTrayIcon.MiddleClick,
        ):
            self.open_panel.emit()
