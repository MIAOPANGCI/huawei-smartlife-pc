"""设置对话框：外观（深浅模式 / 主题色）、账号信息与说明。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import ACCENT_PRESETS, theme


class _ColorSwatch(QPushButton):
    """主题色色块。"""

    def __init__(self, name: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setCheckable(True)
        self.setToolTip(name)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {color};
                border: 2px solid transparent;
                border-radius: 15px;
            }}
            QPushButton:checked {{
                border: 2px solid {theme().palette.text};
            }}
            """
        )


class SettingsDialog(QDialog):
    """外观与账号设置。"""

    mode_changed = Signal(str)
    accent_changed = Signal(str)
    startup_view_changed = Signal(str)
    logout_requested = Signal()

    def __init__(
        self,
        account: str,
        families: list[tuple[str, str]] | None = None,
        startup_view: str = "all",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setFixedWidth(430)
        self._account = account
        self._families = families or []
        self._startup_view = startup_view
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title = QLabel("设置")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        # ---- 外观 ----
        layout.addWidget(self._section("外观"))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for mode, text in (("dark", "深色"), ("light", "浅色")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(theme().mode == mode)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)   # 避免 Fusion 焦点框盖住选中态
            button.clicked.connect(lambda _c=False, m=mode: self.mode_changed.emit(m))
            self._mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        accent_row = QHBoxLayout()
        accent_row.setSpacing(10)
        accent_label = QLabel("主题色")
        accent_label.setObjectName("Body")
        accent_row.addWidget(accent_label)
        self._accent_group = QButtonGroup(self)
        self._accent_group.setExclusive(True)
        for name, color in ACCENT_PRESETS.items():
            swatch = _ColorSwatch(name.strip(), color)
            swatch.setChecked(name == theme().accent_name)
            swatch.setFocusPolicy(Qt.NoFocus)
            swatch.clicked.connect(lambda _c=False, n=name: self.accent_changed.emit(n))
            self._accent_group.addButton(swatch)
            accent_row.addWidget(swatch)
        accent_row.addStretch(1)
        layout.addLayout(accent_row)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        # ---- 启动 ----
        layout.addWidget(self._section("启动"))
        self._startup_combo = QComboBox()
        self._startup_combo.setMinimumWidth(220)
        options: list[tuple[str, str]] = [
            ("全部设备", "all"),
            ("智能场景", "scenes"),
            ("运行日志", "logs"),
        ]
        for home_id, name in self._families:
            options.append((f"家庭 · {name}", f"home:{home_id}"))
        current_index = 0
        for index, (_label, key) in enumerate(options):
            self._startup_combo.addItem(_label, key)
            if key == self._startup_view:
                current_index = index
        self._startup_combo.setCurrentIndex(current_index)
        self._startup_combo.currentIndexChanged.connect(self._on_startup_changed)
        startup_row = QHBoxLayout()
        startup_row.setSpacing(10)
        startup_label = QLabel("启动时默认打开")
        startup_label.setObjectName("Body")
        startup_row.addWidget(startup_label, 0)
        startup_row.addWidget(self._startup_combo, 1)
        layout.addLayout(startup_row)

        hint = QLabel("下次启动登录后自动切换到这个分类。")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        divider_startup = QFrame()
        divider_startup.setObjectName("Divider")
        divider_startup.setFixedHeight(1)
        layout.addWidget(divider_startup)

        # ---- 账号 ----
        layout.addWidget(self._section("账号"))
        account_label = QLabel(f"当前账号：{self._account or '未登录'}")
        account_label.setObjectName("Body")
        layout.addWidget(account_label)

        session_hint = QLabel(
            "登录凭据以明文保存在 ~/.huawei_smartlife/session.json，"
            "仅供本机免密登录使用；退出登录会自动删除该文件。"
        )
        session_hint.setObjectName("Caption")
        session_hint.setWordWrap(True)
        layout.addWidget(session_hint)

        logout = QPushButton("退出登录并删除本地会话")
        logout.setObjectName("Danger")
        logout.setCursor(Qt.PointingHandCursor)
        logout.clicked.connect(self._on_logout)
        layout.addWidget(logout, 0, Qt.AlignLeft)

        divider2 = QFrame()
        divider2.setObjectName("Divider")
        divider2.setFixedHeight(1)
        layout.addWidget(divider2)

        # ---- 关于 ----
        layout.addWidget(self._section("关于"))
        about = QLabel(
            "华为智慧生活 PC 控制台 · 非官方客户端\n"
            "基于社区对智慧生活云端协议的逆向研究实现，华为未开放官方 API，"
            "接口可能随版本更新失效。仅供个人学习与研究使用。"
        )
        about.setObjectName("Caption")
        about.setWordWrap(True)
        layout.addWidget(about)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        layout.addLayout(close_row)

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    def _on_startup_changed(self, index: int) -> None:
        key = self._startup_combo.itemData(index)
        if isinstance(key, str):
            self.startup_view_changed.emit(key)

    def _on_logout(self) -> None:
        self.logout_requested.emit()
        self.accept()
