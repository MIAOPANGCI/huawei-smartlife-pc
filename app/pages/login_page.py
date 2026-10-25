"""登录页：账号密码 + 二次验证（短信 / 设备挑战码）。

交互流程
--------
1. 输入华为账号（手机号 / 邮箱）与密码 → 登录；
2. 若账号开了二次验证，页面就地展开「验证码」区块，可选渠道并发送；
3. 提交验证码 → 登录成功 → 主窗口切到设备看板。

所有网络动作都交给控制器，本页只负责收集输入与展示状态。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from huawei_smartlife.models import (
    CHALLENGE_KIND_DEVICE,
    CHALLENGE_KIND_EMAIL,
    CHALLENGE_KIND_SMS,
    ChallengeChannel,
    LoginChallenge,
    channel_prompt,
)

from ..theme import theme
from ..widgets.controls import SwitchButton


class LoginPage(QWidget):
    """登录视图。"""

    login_requested = Signal(str, str, bool)   # account, password, remember
    code_submitted = Signal(str)               # code
    channel_requested = Signal(object)         # ChallengeChannel，要求发送验证码
    channel_switched = Signal(object)          # ChallengeChannel，只切换校验方式

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._challenge: LoginChallenge | None = None
        self._channels: list[ChallengeChannel] = []
        self._build()
        self.apply_theme()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.setObjectName("LoginSurface")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 24, 40, 40)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("LoginCard")
        card.setFixedWidth(430)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 70))
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 32, 34, 28)
        layout.setSpacing(12)

        title = QLabel("登录华为账号")
        title.setObjectName("LoginTitle")
        layout.addWidget(title)

        subtitle = QLabel("登录后可查看与控制「智慧生活」中的设备")
        subtitle.setObjectName("LoginSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(10)

        # ---- 账号 ----
        layout.addWidget(self._field_label("账号"))
        self._account = QLineEdit()
        self._account.setPlaceholderText("手机号 或 邮箱")
        self._account.setClearButtonEnabled(True)
        layout.addWidget(self._account)

        # ---- 密码 ----
        layout.addWidget(self._field_label("密码"))
        password_row = QHBoxLayout()
        password_row.setSpacing(6)
        self._password = QLineEdit()
        self._password.setPlaceholderText("华为账号密码")
        self._password.setEchoMode(QLineEdit.Password)
        password_row.addWidget(self._password, 1)
        self._show_password = QPushButton("显示")
        self._show_password.setObjectName("Ghost")
        self._show_password.setFixedWidth(56)
        self._show_password.setCheckable(True)
        self._show_password.toggled.connect(self._toggle_password_echo)
        password_row.addWidget(self._show_password)
        layout.addLayout(password_row)

        remember_row = QHBoxLayout()
        remember_row.setSpacing(9)
        self._remember_switch = SwitchButton(True)
        remember_row.addWidget(self._remember_switch, 0, Qt.AlignVCenter)
        remember_label = QLabel("记住登录状态（会话保存在本机）")
        remember_label.setObjectName("Body")
        remember_row.addWidget(remember_label, 1)
        layout.addLayout(remember_row)

        self._error = QLabel("")
        self._error.setObjectName("ErrorHint")
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)

        self._login_button = QPushButton("登  录")
        self._login_button.setObjectName("Primary")
        self._login_button.setFixedHeight(38)
        self._login_button.setCursor(Qt.PointingHandCursor)
        self._login_button.clicked.connect(self._on_login)
        layout.addWidget(self._login_button)

        # ---- 二次验证（默认隐藏）----
        self._challenge_box = QFrame()
        self._challenge_box.setObjectName("PanelFlat")
        challenge_layout = QVBoxLayout(self._challenge_box)
        challenge_layout.setContentsMargins(14, 12, 14, 12)
        challenge_layout.setSpacing(8)

        self._challenge_prompt = QLabel("")
        self._challenge_prompt.setObjectName("Body")
        self._challenge_prompt.setWordWrap(True)
        challenge_layout.addWidget(self._challenge_prompt)

        channel_row = QHBoxLayout()
        channel_row.setSpacing(8)
        # 渠道不再让用户选：默认就是华为推到设备上的那条验证码。
        # 控件保留（内部仍靠索引驱动），但永远不显示。
        self._channel_combo = QComboBox()
        self._channel_combo.setMinimumWidth(150)
        self._channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self._channel_combo.hide()
        channel_row.addWidget(self._channel_combo, 0)
        channel_row.addStretch(1)
        self._send_button = QPushButton("改用短信")
        self._send_button.setObjectName("Ghost")
        self._send_button.clicked.connect(self._on_send_code)
        channel_row.addWidget(self._send_button)
        challenge_layout.addLayout(channel_row)

        code_row = QHBoxLayout()
        code_row.setSpacing(8)
        self._code = QLineEdit()
        self._code.setPlaceholderText("输入验证码")
        self._code.returnPressed.connect(self._on_submit_code)
        code_row.addWidget(self._code, 1)
        self._submit_button = QPushButton("验证并登录")
        self._submit_button.setObjectName("Primary")
        self._submit_button.clicked.connect(self._on_submit_code)
        code_row.addWidget(self._submit_button)
        challenge_layout.addLayout(code_row)

        layout.addWidget(self._challenge_box)
        self._challenge_box.hide()

        layout.addSpacing(4)
        disclaimer = QLabel(
            "非官方客户端：基于社区对智慧生活云端协议的逆向研究，不会上传你的密码。\n"
            "仅限个人学习研究使用。"
        )
        disclaimer.setObjectName("Caption")
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)

        outer.addWidget(card)
        self._account.setFocus()

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def apply_theme(self) -> None:
        palette = theme().palette
        self.setStyleSheet(
            f"""
            QWidget#LoginSurface {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {palette.bg}, stop:0.55 {palette.bg},
                    stop:1 {palette.accent_soft});
            }}
            """
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        # 先让 QSS 画出底色，再叠加两枚柔光，避免登录页过于平板
        super().paintEvent(event)
        palette = theme().palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width, height = self.width(), self.height()
        for cx, cy, radius, alpha in (
            (width * 0.5, height * 0.05, max(width, height) * 0.50, 14),
            (width * 0.10, height * 0.98, max(width, height) * 0.38, 8),
        ):
            glow = QRadialGradient(cx, cy, radius)
            color = QColor(palette.accent)
            color.setAlpha(alpha)
            glow.setColorAt(0.0, color)
            color_end = QColor(palette.accent)
            color_end.setAlpha(0)
            glow.setColorAt(1.0, color_end)
            painter.fillRect(self.rect(), glow)
        painter.end()

    # ------------------------------------------------------------------
    # 行为
    # ------------------------------------------------------------------
    def _toggle_password_echo(self, shown: bool) -> None:
        self._password.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self._show_password.setText("隐藏" if shown else "显示")

    def _on_login(self) -> None:
        account = self._account.text().strip()
        password = self._password.text()
        if not account:
            self.show_error("请输入华为账号（手机号或邮箱）。")
            self._account.setFocus()
            return
        if not password:
            self.show_error("请输入账号密码。")
            self._password.setFocus()
            return
        self.clear_error()
        self.login_requested.emit(account, password, self._remember_switch.isChecked())

    def _on_send_code(self) -> None:
        channel = self._current_channel()
        if channel is not None and channel.is_sms:
            self.channel_requested.emit(channel)
            return
        # 设备渠道：重新发起一次登录，让华为推一条新验证码
        account = self._account.text().strip()
        password = self._password.text()
        if not account or not password:
            self.show_error("请先填写账号和密码，再重新获取验证码。")
            return
        self.login_requested.emit(account, password, self._remember_switch.isChecked())

    def _on_submit_code(self) -> None:
        code = self._code.text().strip()
        if not code:
            self.show_error("请输入收到的验证码。")
            return
        self.clear_error()
        self.code_submitted.emit(code)

    # ------------------------------------------------------------------
    # 外部驱动
    # ------------------------------------------------------------------
    def set_busy(self, busy: bool, message: str = "") -> None:
        self._login_button.setEnabled(not busy)
        self._submit_button.setEnabled(not busy)
        self._send_button.setEnabled(not busy)
        if busy:
            self._login_button.setText(message or "处理中…")
        else:
            self._sync_channel_state()
            self._login_button.setText("登  录")

    def show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def clear_error(self) -> None:
        self._error.clear()
        self._error.hide()

    def show_challenge(self, challenge: LoginChallenge) -> None:
        """展开验证码区块。

        默认停在服务端给的渠道上（现在一定是短信/邮件这类需要用户点按钮
        才真正发送的），不会出现"什么都没点就说验证码已发送"的错觉。
        """
        self._challenge = challenge
        self._channels = list(challenge.channels)

        self._channel_combo.blockSignals(True)
        self._channel_combo.clear()
        for channel in self._channels:
            self._channel_combo.addItem(self._channel_label(channel))
        index = 0
        for position, channel in enumerate(self._channels):
            if channel.kind == challenge.kind:
                index = position
                break
        if self._channels:
            self._channel_combo.setCurrentIndex(index)
        self._channel_combo.blockSignals(False)

        self._sync_channel_state()
        self._challenge_box.show()
        self._code.setFocus()
        self.clear_error()

    # ---------- 验证渠道 ----------
    @staticmethod
    def _channel_label(channel: ChallengeChannel) -> str:
        prefix = {
            CHALLENGE_KIND_SMS: "短信",
            CHALLENGE_KIND_EMAIL: "邮件",
            CHALLENGE_KIND_DEVICE: "华为设备",
        }.get(channel.kind, "验证")
        return f"{prefix} · {channel.name}"

    def _current_channel(self) -> ChallengeChannel | None:
        index = self._channel_combo.currentIndex()
        if 0 <= index < len(self._channels):
            return self._channels[index]
        return None

    def _on_channel_changed(self, _index: int) -> None:
        self._sync_channel_state()
        # 用户已经按提示换了接收方式，上一条"请切到 xxx"的错误就没意义了，
        # 留着只会让人以为刚才的操作没生效。
        self.clear_error()
        channel = self._current_channel()
        if channel is not None:
            # 必须告诉后端：提交验证码时要用对应渠道的类型。少了这一句，
            # 切到「华为设备」后提交仍按短信类型（vAcT=2）校验，必然 70008804。
            self.channel_switched.emit(channel)

    def focus_code(self) -> None:
        """把焦点放回验证码输入框（提交失败后方便直接重输）。"""
        if self._challenge_box.isVisible():
            self._code.setFocus()
            self._code.selectAll()

    def challenge_visible(self) -> bool:
        return self._challenge_box.isVisible()

    def clear_challenge_input(self) -> None:
        """新发起一次登录时清空验证码：旧的码一定已经作废。"""
        self._code.clear()

    def _sync_channel_state(self) -> None:
        """下拉换项时同步提示文案与「发送」按钮的可用性。"""
        channel = self._current_channel()
        if channel is None:
            fallback = self._challenge.prompt if self._challenge else ""
            self._challenge_prompt.setText(fallback or "需要二次验证，请获取验证码。")
            self._send_button.setEnabled(False)
            return

        self._challenge_prompt.setText(channel_prompt(channel))
        if channel.kind == CHALLENGE_KIND_DEVICE:
            # 设备码没法"发送"，但可以重新发起一次登录——华为会推一条新的。
            # 华为的验证窗口只有两分钟左右，超时后这是唯一的自救方式。
            self._send_button.setVisible(True)
            self._send_button.setText("重新获取")
            self._send_button.setEnabled(True)
        elif channel.is_sms:
            self._send_button.setVisible(True)
            self._send_button.setText("重新发送" if channel.sent else "发送验证码")
            self._send_button.setEnabled(True)
        else:
            self._send_button.setVisible(False)

    def reset(self) -> None:
        """登出后清空敏感输入。"""
        self._password.clear()
        self._code.clear()
        self._challenge = None
        self._channels = []
        self._challenge_box.hide()
        self.clear_error()
        self._login_button.setText("登  录")
        self._login_button.setEnabled(True)

    def set_account_hint(self, account: str) -> None:
        if account and not self._account.text():
            self._account.setText(account)
