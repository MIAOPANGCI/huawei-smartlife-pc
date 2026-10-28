"""主题系统：调色板 + 全局 QSS 生成。

设计原则
--------
1. **单一数据源**：所有颜色集中在 ``PALETTES``，QSS 通过占位符（``@key``）
   注入，切换深/浅或主题色时只需重新生成一次样式表，无需逐个控件改属性。
2. **占位符替换而非 str.format**：QSS 自身含大量 ``{ }`` 块，用
   ``str.format`` 需要转义所有花括号，极易出错；这里统一用 ``@key`` 前缀替换
   （按键名长度倒序替换，避免 ``@border`` 吃掉 ``@border_soft``）。
3. **主题色参与派生**：强调色会根据深/浅模式自动调整明度，保证置于强调色
   之上的文字始终可读（对比度 ≥ 4.5:1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# ---------------------------------------------------------------------------
# 主题色预设
# ---------------------------------------------------------------------------
ACCENT_PRESETS: Final[dict[str, str]] = {
    "华为红": "#d92b3a",
    "翠  绿": "#12b76a",
    "蔚  蓝": "#2e7cf6",
    "暖  橙": "#ef7c1a",
}
DEFAULT_ACCENT: Final[str] = "华为红"


@dataclass(frozen=True)
class Palette:
    """一套完整调色板。所有 QSS 颜色都从这里取。"""

    mode: str
    accent: str

    bg: str
    bg_elev: str
    sidebar: str
    surface: str
    surface_2: str
    surface_3: str

    text: str
    text_2: str
    text_3: str

    border: str
    border_soft: str

    ok: str
    warn: str
    danger: str

    # 强调色的派生色（hover / 半透明底 / 描边）
    accent_hover: str
    accent_soft: str
    accent_line: str
    on_accent: str

    card_border: str = "transparent"
    panel_bg: str = "transparent"
    board_bg: str = "transparent"
    window_bg: str = "transparent"
    titlebar_bg: str = "transparent"
    radius_sm: str = "8px"
    radius_md: str = "12px"
    radius_lg: str = "14px"
    extra: dict[str, str] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, str]:
        data: dict[str, str] = {
            "mode": self.mode,
            "bg": self.bg,
            "bg_elev": self.bg_elev,
            "sidebar": self.sidebar,
            "surface": self.surface,
            "surface_2": self.surface_2,
            "surface_3": self.surface_3,
            "text": self.text,
            "text_2": self.text_2,
            "text_3": self.text_3,
            "border": self.border,
            "border_soft": self.border_soft,
            "ok": self.ok,
            "warn": self.warn,
            "danger": self.danger,
            "accent": self.accent,
            "accent_hover": self.accent_hover,
            "accent_soft": self.accent_soft,
            "accent_line": self.accent_line,
            "on_accent": self.on_accent,
            "card_border": self.card_border,
            "panel_bg": self.panel_bg,
            "board_bg": self.board_bg,
            "window_bg": self.window_bg,
            "titlebar_bg": self.titlebar_bg,
            "radius_sm": self.radius_sm,
            "radius_md": self.radius_md,
            "radius_lg": self.radius_lg,
        }
        data.update(self.extra)
        return data


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------
def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"非法颜色: {value}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{_clamp(c):02x}" for c in rgb)


def mix(color_a: str, color_b: str, ratio: float) -> str:
    """按比例混合两色（ratio=0 取 a，1 取 b）。"""
    ratio = max(0.0, min(1.0, ratio))
    a = hex_to_rgb(color_a)
    b = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * ratio for i in range(3)))


def rgba(color: str, alpha: float) -> str:
    r, g, b = hex_to_rgb(color)
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, alpha)):.3f})"


def _luminance(color: str) -> float:
    """相对亮度（WCAG 公式），用于决定强调色上的文字颜色。"""

    def channel(c: int) -> float:
        srgb = c / 255.0
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def readable_on(color: str) -> str:
    """返回置于 `color` 之上可读的文字色（深色或白色）。"""
    return "#12161a" if _luminance(color) > 0.45 else "#ffffff"


# ---------------------------------------------------------------------------
# 调色板构建
# ---------------------------------------------------------------------------
def build_palette(mode: str, accent: str) -> Palette:
    """按模式与主题色构建调色板。mode: "dark" | "light"。"""
    accent = accent if accent.startswith("#") else "#d92b3a"
    dark = mode == "dark"

    if dark:
        card_border = "transparent"
        window_bg = "#131517"
        panel_bg = "rgba(255, 255, 255, 0.05)"
        board_bg = "rgba(23, 25, 28, 0.96)"
        bg = "#131517"
        bg_elev = "#17191c"
        sidebar = "#151719"
        surface = "#23262b"
        surface_2 = "#1a1d20"
        surface_3 = "#2e3238"
        text = "#e9eaec"
        text_2 = "#9aa2a9"
        text_3 = "#6f7780"
        border = "#2c3036"
        border_soft = "#232629"
        accent_use = mix(accent, "#ffffff", 0.12)  # 深色底上略提亮
        accent_hover = mix(accent, "#ffffff", 0.24)
        accent_soft = rgba(accent_use, 0.16)
        accent_line = rgba(accent_use, 0.42)
    else:
        card_border = "#e7ecf2"
        window_bg = "#f1f3f7"
        panel_bg = "#f4f6fa"
        board_bg = "rgba(250, 251, 252, 0.96)"
        bg = "#f1f3f7"
        bg_elev = "#ffffff"
        sidebar = "#ffffff"
        surface = "#ffffff"
        surface_2 = "#f5f7fa"
        surface_3 = "#eceff4"
        text = "#171b21"
        text_2 = "#5d6a77"
        text_3 = "#8b97a3"
        border = "#e3e8ef"
        border_soft = "#eef1f5"
        accent_use = accent
        accent_hover = mix(accent, "#000000", 0.14)
        accent_soft = rgba(accent_use, 0.10)
        accent_line = rgba(accent_use, 0.34)

    return Palette(
        mode=mode,
        accent=accent_use,
        bg=bg,
        bg_elev=bg_elev,
        sidebar=sidebar,
        surface=surface,
        surface_2=surface_2,
        surface_3=surface_3,
        text=text,
        text_2=text_2,
        text_3=text_3,
        border=border,
        border_soft=border_soft,
        ok="#22c55e" if dark else "#12a150",
        warn="#f2b13c" if dark else "#d18b10",
        danger="#f2555a" if dark else "#d92b3a",
        accent_hover=accent_hover,
        accent_soft=accent_soft,
        accent_line=accent_line,
        on_accent=readable_on(accent_use),
        card_border=card_border,
        window_bg=window_bg,
        panel_bg=panel_bg,
        board_bg=board_bg,
    )


# ---------------------------------------------------------------------------
# QSS 模板
# ---------------------------------------------------------------------------
_QSS = """
* {
    outline: 0;
}

QWidget {
    color: @text;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}

QMainWindow {
    background: transparent;
}
#RootSurface {
    background: @window_bg;
    border: 1px solid @border_soft;
    border-radius: 14px;
}
#RootSurface[winMaximized="true"] {
    border: none;
    border-radius: 0px;
}

QToolTip {
    background: @surface_3;
    color: @text;
    border: 1px solid @border;
    border-radius: 6px;
    padding: 6px 9px;
}

/* ---------------- 无边框窗口 ---------------- */
#TitleBar {
    background: @bg_elev;
    border-bottom: 1px solid @border_soft;
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
}
#TitleBar[winMaximized="true"] {
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
}
#TitleBar QLabel {
    background: transparent;
}
#AppTitle {
    font-size: 14px;
    font-weight: 600;
    color: @text;
    letter-spacing: 0.4px;
}
#AppSubTitle {
    color: @text_3;
    font-size: 11px;
}
#TitleButton {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: @text_2;
    padding: 0;
    font-family: "Segoe MDL2 Assets";
    font-size: 14px;
}
#TitleButton:hover {
    background: @surface_3;
    color: @text;
}
#TitleButtonClose {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: @text_2;
    padding: 0;
    font-family: "Segoe MDL2 Assets";
    font-size: 14px;
}
#TitleButtonClose:hover {
    background: #e81123;
    color: #ffffff;
}

/* ---------------- 侧边栏 ---------------- */
#Sidebar {
    background: @sidebar;
    border-right: 1px solid @border_soft;
}
#SidebarSection {
    color: @text_3;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.2px;
    padding: 4px 12px;
}
QPushButton#NavItem {
    background: transparent;
    border: none;
    border-radius: 9px;
    color: @text_2;
    text-align: left;
    padding: 9px 12px;
    font-size: 13px;
}
QPushButton#NavItem:hover {
    background: @surface_2;
    color: @text;
}
QPushButton#NavItem:checked {
    background: @accent_soft;
    color: @accent;
    font-weight: 600;
}

/* ---------------- 顶部区域（参考实现风格） ---------------- */
QPushButton#FamilyTitle {
    background: transparent;
    border: none;
    color: @text;
    font-size: 23px;
    font-weight: 700;
    padding: 0;
    text-align: left;
}
QPushButton#FamilyTitle:hover {
    color: @accent;
}

QLabel#PageSummary {
    color: @text_3;
    font-size: 12px;
    padding-bottom: 4px;
}

QPushButton#TabButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    color: @text_2;
    padding: 5px 15px;
    font-size: 13px;
}
QPushButton#TabButton:hover {
    background: @surface_2;
    color: @text;
}
QPushButton#TabButton:checked {
    background: @accent;
    color: @on_accent;
    font-weight: 600;
}

QFrame#Chip {
    background: @surface_2;
    border: 1px solid @border_soft;
    border-radius: 13px;
}
QLabel#ChipText {
    color: @text_2;
    font-size: 11px;
}

QPushButton#ToolButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    color: @text_2;
    font-family: "Segoe MDL2 Assets";
    font-size: 15px;
}
QPushButton#ToolButton:hover {
    background: @surface_2;
    color: @text;
}
QPushButton#ToolButton[active="true"] {
    background: @accent_soft;
    color: @accent;
}

QMenu {
    background: @surface;
    border: 1px solid @border;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 22px 7px 14px;
    border-radius: 6px;
    color: @text;
    font-size: 13px;
}
QMenu::item:selected {
    background: @accent_soft;
    color: @text;
}
QMenu::separator {
    height: 1px;
    background: @border_soft;
    margin: 5px 8px;
}

/* ---------------- 通用按钮 ---------------- */
QPushButton {
    background: @surface_2;
    color: @text;
    border: 1px solid @border;
    border-radius: @radius_sm;
    padding: 7px 14px;
    font-size: 13px;
}
QPushButton:hover {
    background: @surface_3;
    border-color: @border;
}
QPushButton:pressed {
    background: @bg;
}
QPushButton:disabled {
    color: @text_3;
    background: @surface_2;
    border-color: @border_soft;
}
QPushButton:checked {
    background: @accent_soft;
    border-color: @accent_line;
    color: @accent;
    font-weight: 600;
}
QPushButton#Primary {
    background: @accent;
    color: @on_accent;
    border: 1px solid @accent;
    font-weight: 600;
}
QPushButton#Primary:hover {
    background: @accent_hover;
    border-color: @accent_hover;
}
QPushButton#Primary:disabled {
    background: @surface_3;
    color: @text_3;
    border-color: @border_soft;
}
QPushButton#Ghost {
    background: transparent;
    border: 1px solid @border;
    color: @text_2;
}
QPushButton#Ghost:hover {
    background: @surface_2;
    color: @text;
}
QPushButton#Danger {
    background: transparent;
    border: 1px solid @accent_line;
    color: @accent;
}
QPushButton#Danger:hover {
    background: @accent_soft;
}
QPushButton#LinkButton {
    background: transparent;
    border: none;
    color: @accent;
    padding: 2px 4px;
}
QPushButton#LinkButton:hover {
    text-decoration: underline;
}

/* ---------------- 输入控件 ---------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
    background: @surface;
    color: @text;
    border: 1px solid @border;
    border-radius: @radius_sm;
    padding: 7px 10px;
    selection-background-color: @accent;
    selection-color: @on_accent;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: @accent_line;
    background: @surface;
}
QLineEdit:disabled {
    color: @text_3;
    background: @surface_2;
}
QLineEdit#SearchBox {
    background: @surface_2;
    border: 1px solid transparent;
    border-radius: 15px;
    padding: 5px 12px;
}
QLineEdit#SearchBox:focus {
    border-color: @accent_line;
    background: @surface;
}

/* QWidget 定了全局样式后，QCheckBox 也切换成 QSS 绘制；此时若不给
   indicator 定义样式，Qt 会画成一个纯色方块，勾没勾都长一样。
   这里显式画出空心/实心两种状态。 */
QCheckBox {
    background: transparent;
    color: @text;
    spacing: 7px;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid @text_3;
    border-radius: 4px;
    background: @surface_2;
}
QCheckBox::indicator:hover {
    border-color: @accent_line;
}
QCheckBox::indicator:checked {
    background: @accent;
    border-color: @accent;
}
QCheckBox::indicator:checked:hover {
    background: @accent_hover;
    border-color: @accent_hover;
}
QCheckBox:disabled {
    color: @text_3;
}
QCheckBox::indicator:disabled {
    background: @surface_3;
    border-color: @border_soft;
}

QComboBox {
    background: @surface_2;
    color: @text;
    border: 1px solid @border;
    border-radius: @radius_sm;
    padding: 6px 10px;
    min-height: 18px;
}
QComboBox:hover {
    border-color: @accent_line;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox::down-arrow {
    width: 8px;
    height: 8px;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: @surface;
    color: @text;
    border: 1px solid @border;
    border-radius: @radius_sm;
    padding: 4px;
    selection-background-color: @accent_soft;
    selection-color: @text;
    outline: 0;
}

QToolButton {
    background: transparent;
    border: none;
    color: @text_2;
    padding: 2px 0;
}
QToolButton:hover {
    color: @accent;
}

/* ---------------- 托盘面板 / 桌面挂件 ---------------- */
/* 托盘面板 / 挂件的外壳改成 RoundedSurface 自绘圆角，
   这里不再给背景，否则会在抗锯齿边缘外面再糊一层直角。 */
QLabel#PopoverTitle {
    font-size: 13px;
    font-weight: 600;
    color: @text;
}
QPushButton#PopoverButton {
    background: transparent;
    border: none;
    border-radius: 7px;
    color: @text_2;
}
QPushButton#PopoverButton:hover {
    background: @surface_2;
    color: @text;
}


QLabel#BoardTitle {
    font-size: 12px;
    font-weight: 600;
    color: @text_2;
}

QFrame#MiniCard {
    background: @surface;
    border: 1px solid @card_border;
    border-radius: 10px;
}
QFrame#MiniCard:hover {
    background: @surface_3;
}
QFrame#MiniCard[offline="true"] {
    background: @surface_2;
}
QLabel#MiniCardTitle {
    font-size: 12px;
    font-weight: 600;
    color: @text;
}
QLabel#MiniCardMeta {
    font-size: 10px;
    color: @text_3;
}

/* ---------------- 滑块 ---------------- */
QSlider::groove:horizontal {
    height: 4px;
    background: @surface_3;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: @accent;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff;
    border: 2px solid @accent;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    border-color: @accent_hover;
}
QSlider::groove:horizontal:disabled {
    background: @surface_2;
}
QSlider::sub-page:horizontal:disabled {
    background: @text_3;
}
QSlider::handle:horizontal:disabled {
    border-color: @text_3;
}

/* ---------------- 滚动条 ---------------- */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: @surface_3;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: @text_3;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: @surface_3;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ---------------- 卡片与面板 ---------------- */
QFrame#Card, QWidget#Card {
    background: @surface;
    border: 1px solid @card_border;
    border-radius: 12px;
}
QFrame#Card:hover {
    background: @surface_3;
}
QFrame#Card[selected="true"] {
    border: 1px solid @accent_line;
}
QFrame#Card[offline="true"] {
    background: @surface_2;
}
QFrame#Card[offline="true"] QLabel#CardTitle {
    color: @text_2;
}
QFrame#Card[offline="true"]:hover {
    background: @surface_3;
}
QFrame#Panel {
    background: @surface;
    border: 1px solid @border_soft;
    border-radius: @radius_lg;
}
QFrame#PanelFlat {
    background: @panel_bg;
    border: 1px solid transparent;
    border-radius: @radius_md;
}
QFrame#Divider {
    background: @border_soft;
    border: none;
    max-height: 1px;
    min-height: 1px;
}

/* ---------------- 文本层级 ---------------- */
QLabel#PageTitle {
    font-size: 19px;
    font-weight: 700;
    color: @text;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: 600;
    color: @text_2;
}
QLabel#CardTitle {
    font-size: 13px;
    font-weight: 600;
    color: @text;
}
QLabel#Body {
    font-size: 13px;
    color: @text_2;
}
QLabel#Caption {
    font-size: 11px;
    color: @text_3;
}
QLabel#CaptionStrong {
    font-size: 11px;
    color: @text_2;
}
QLabel#Value {
    font-size: 13px;
    color: @text;
    font-weight: 600;
}
QLabel#Mono {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px;
    color: @text_2;
}
QLabel#StatusOnline {
    color: @ok;
    font-size: 11px;
}
QLabel#StatusOffline {
    color: @text_3;
    font-size: 11px;
}
QLabel#ErrorHint {
    color: @danger;
    font-size: 12px;
}

/* ---------------- 日志 ---------------- */
QPlainTextEdit#LogView {
    background: @surface_2;
    color: @text_2;
    border: 1px solid @border_soft;
    border-radius: @radius_md;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px;
    padding: 8px;
}

/* ---------------- 登录页 ---------------- */
#LoginSurface {
    background: @bg;
    border-radius: 13px;
}
QFrame#LoginCard {
    background: @bg_elev;
    border: 1px solid @border;
    border-radius: 18px;
}
QLabel#LoginTitle {
    font-size: 24px;
    font-weight: 700;
    color: @text;
}
QLabel#LoginSubtitle {
    font-size: 12px;
    color: @text_3;
}
QLabel#FieldLabel {
    font-size: 12px;
    color: @text_2;
}

/* ---------------- 对话框 ---------------- */
QDialog {
    background: @bg_elev;
}
#DialogSurface {
    background: @bg_elev;
    border: 1px solid @border;
    border-radius: @radius_lg;
}
#DialogTitleBar {
    background: transparent;
}
#DialogTitle {
    font-size: 15px;
    font-weight: 600;
}
#DialogButtonBar {
    background: @bg_elev;
}

/* ---------------- 表格/列表（场景） ---------------- */
QListWidget {
    background: transparent;
    border: none;
    outline: 0;
}
QListWidget::item {
    background: @surface;
    border: 1px solid @border_soft;
    border-radius: @radius_md;
    padding: 12px;
    margin: 4px 2px;
    color: @text;
}
QListWidget::item:hover {
    border-color: @accent_line;
}
QListWidget::item:selected {
    background: @accent_soft;
    border-color: @accent_line;
    color: @text;
}

/* ---------------- 设备选择对话框 ---------------- */
QListWidget#PickerList {
    background: @bg_elev;
    border: 1px solid @border_soft;
    border-radius: @radius_md;
    outline: 0;
    padding: 4px;
}
QListWidget#PickerList::item {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 7px 8px;
    margin: 1px 0;
    color: @text;
    font-size: 12px;
}
QListWidget#PickerList::item:hover {
    background: @surface_2;
}
QListWidget#PickerList::item:selected {
    background: @accent_soft;
    border-color: @accent_line;
    color: @text;
}

QPushButton#PickerAction {
    padding: 0 4px;
    background: @surface_2;
    border: 1px solid @border_soft;
    border-radius: 8px;
    color: @text_2;
    font-size: 11px;
}
QPushButton#PickerAction:hover {
    background: @surface_3;
    border-color: @border;
    color: @text;
}
QPushButton#PickerAction:disabled {
    background: @surface_2;
    border-color: @border_soft;
    color: @text_3;
}

/* ---------------- 消息框 ---------------- */
QMessageBox {
    background: @bg_elev;
}
QMessageBox QLabel {
    color: @text;
    font-size: 13px;
}
"""


def build_qss(palette: Palette) -> str:
    """把调色板注入 QSS 模板。"""
    qss = _QSS
    mapping = palette.as_mapping()
    # 长键优先替换，避免 @border 命中 @border_soft 的前缀
    for key in sorted(mapping, key=len, reverse=True):
        qss = qss.replace("@" + key, mapping[key])
    return qss


class ThemeManager:
    """持有当前模式/主题色/调色板，负责向 Qt 应用推样式。"""

    MODES = ("dark", "light")

    def __init__(self, mode: str = "dark", accent: str = DEFAULT_ACCENT) -> None:
        self._mode = mode if mode in self.MODES else "dark"
        self._accent = accent if accent in ACCENT_PRESETS else DEFAULT_ACCENT
        self._palette = build_palette(self._mode, ACCENT_PRESETS[self._accent])
        self._listeners: list = []

    # -- 属性 --
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def accent_name(self) -> str:
        return self._accent

    @property
    def accent_color(self) -> str:
        return self._palette.accent

    @property
    def palette(self) -> Palette:
        return self._palette

    # -- 变更 --
    def on_changed(self, callback) -> None:
        """注册主题变更回调（用于刷新自绘控件）。"""
        self._listeners.append(callback)

    def set_mode(self, mode: str) -> None:
        if mode not in self.MODES or mode == self._mode:
            return
        self._mode = mode
        self._rebuild()

    def toggle_mode(self) -> str:
        self.set_mode("light" if self._mode == "dark" else "dark")
        return self._mode

    def set_accent(self, accent_name: str) -> None:
        if accent_name not in ACCENT_PRESETS or accent_name == self._accent:
            return
        self._accent = accent_name
        self._rebuild()

    def _rebuild(self) -> None:
        self._palette = build_palette(self._mode, ACCENT_PRESETS[self._accent])
        for callback in list(self._listeners):
            try:
                callback(self._palette)
            except Exception:  # noqa: BLE001 - 单个监听器异常不影响其它控件
                pass


# 全局主题实例（GUI 启动时设置）
_theme = ThemeManager()


def theme() -> ThemeManager:
    return _theme


def set_theme(mode: str, accent_name: str) -> ThemeManager:
    global _theme
    _theme = ThemeManager(mode, accent_name)
    return _theme
