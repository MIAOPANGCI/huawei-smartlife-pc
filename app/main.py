"""应用启动引导：日志、高 DPI、全局异常兜底、主题初始化。

单独抽一个模块，是为了让 ``main.py`` 保持极简，同时给测试留出入口
（``create_app()`` 可以在无界面环境下构造 QApplication）。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from . import autostart, dispatch
from .controller import APP_DIR, UI_SETTINGS_PATH
from .main_window import MainWindow
from .theme import DEFAULT_ACCENT, set_theme

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def console_stream_available() -> bool:
    """判断当前进程能不能真的往控制台写。

    用 pythonw（或双击 .vbs）启动时没有控制台，``sys.stderr`` 可能是 ``None``，
    也可能是个存在的对象但底层句柄无效。两种情况都不能挂 StreamHandler，
    否则每条日志都会抛一次异常（被 logging 内部吞掉，表现是日志全丢）。
    所以这里实际试写一次，而不是只判断是否为 None。
    """
    stream = sys.stderr
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
    except Exception:  # noqa: BLE001 - 句柄无效 / 流已关闭
        return False
    return True


def configure_logging() -> None:
    """控制台 + 文件双输出（文件用于排查协议与崩溃问题）。"""
    os.makedirs(APP_DIR, exist_ok=True)
    log_path = os.path.join(APP_DIR, "app.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if console_stream_available() and not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in root.handlers
    ):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    except OSError:
        _LOGGER.warning("无法写入日志文件：%s", log_path)

    # MQTT 心跳日志比较吵，只保留告警以上
    logging.getLogger("paho").setLevel(logging.WARNING)


_CRASH_HANDLE = None


def install_crash_handler() -> None:
    """把解释器级崩溃（access violation 等）的栈写进 crash.log。

    ``sys.excepthook`` 只能接住 Python 异常；Qt 对象被访问已释放内存这种情况
    会直接杀掉进程，Windows 只弹一句 "python has stopped working"，
    什么线索都留不下。faulthandler 是唯一能拿到栈的手段。
    """
    global _CRASH_HANDLE
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        _CRASH_HANDLE = open(
            os.path.join(APP_DIR, "crash.log"), "a", buffering=1, encoding="utf-8"
        )
        faulthandler.enable(file=_CRASH_HANDLE, all_threads=True)
    except (OSError, ValueError) as error:  # noqa: BLE001 - 崩溃记录失败不影响运行
        _LOGGER.warning("无法启用崩溃日志：%s", error)


def install_exception_hook() -> None:
    """未捕获异常：写日志并弹窗，避免界面「静默死掉」。"""

    def hook(exc_type, exc_value, exc_tb):  # noqa: ANN001 - sys.excepthook 签名
        if issubclass(exc_type, KeyboardInterrupt):
            # pythonw 下 sys.stderr 是 None，默认钩子会自己再抛一次
            if sys.stderr is not None:
                sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger(__name__).error(
            "未捕获的异常", exc_info=(exc_type, exc_value, exc_tb)
        )
        try:
            QMessageBox.critical(
                None,
                "程序出错",
                f"发生未处理的错误：\n{exc_value}\n\n详细信息已写入日志：\n"
                f"{os.path.join(APP_DIR, 'app.log')}",
            )
        except Exception:  # noqa: BLE001 - 弹窗失败时不要再抛
            pass

    sys.excepthook = hook


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------
def make_app_icon(accent: str = "#d92b3a") -> QIcon:
    """用代码画一个应用图标，避免打包图片资源。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(2, 2, 60, 60, 14, 14)
    painter.fillPath(path, QColor(accent))
    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPointSize(30)
    font.setWeight(QFont.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "H")
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# 主题设置读取（在窗口构造前应用，避免先深后浅闪一下）
# ---------------------------------------------------------------------------
def load_ui_settings() -> dict:
    import json

    try:
        with open(UI_SETTINGS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _center_on_screen(window: MainWindow) -> None:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    window.move(
        available.x() + max(0, (available.width() - window.width()) // 2),
        available.y() + max(0, (available.height() - window.height()) // 2),
    )


TRAY_FLAG = "--tray"


def parse_launch_args(argv: list[str]) -> tuple[list[str], bool]:
    """拆出启动参数：``--tray`` 表示只在托盘里待命（开机自启动用）。

    这个参数不能留给 QApplication 解析，先摘掉再传进去。
    """
    args = [item for item in argv if item != TRAY_FLAG]
    return args, len(args) != len(argv)


def _migrate_autostart() -> None:
    """把旧版写在注册表里的自启动项换成启动文件夹的快捷方式。

    旧版那条在任务管理器里显示成「python」，用户认不出是谁。迁移失败不影响
    运行（旧项继续生效），所以这里只记日志。
    """
    try:
        if autostart.migrate():
            _LOGGER.info("已把开机自启动项迁移为快捷方式")
    except Exception:  # noqa: BLE001 - 迁移是锦上添花，不能影响启动
        _LOGGER.debug("迁移开机自启动项失败", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    """启动 GUI，返回进程退出码。"""
    args, start_hidden = parse_launch_args(list(argv if argv is not None else sys.argv))

    configure_logging()
    install_crash_handler()
    install_exception_hook()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(args)
    app.setApplicationName("华为智慧生活 PC 控制台")
    app.setOrganizationName("huawei-smartlife-pc")
    app.setStyle("Fusion")  # Fusion 对 QSS 的支持最一致

    settings = load_ui_settings()
    mode = settings.get("mode", "dark")
    accent = settings.get("accent", DEFAULT_ACCENT)
    set_theme(mode, accent)
    app.setWindowIcon(make_app_icon())
    # 主线程投递器：所有跨线程回调都经它回到主线程（见 app/dispatch.py）
    dispatch.attach()

    # 延迟导入：确保 set_theme 先于任何控件构造
    from .controller import AppController

    controller = AppController()
    window = MainWindow(controller, start_hidden=start_hidden)
    _center_on_screen(window)

    if start_hidden and window.tray.available:
        # 开机自启动：只留托盘图标，不弹主窗口挡住刚登录的桌面。
        # 托盘不可用时必须照常显示，否则用户开完机找不到这个程序。
        # 没有可恢复的会话时，_bootstrap 会把窗口放出来（见 MainWindow）。
        _LOGGER.info("以 --tray 启动，主窗口保持隐藏（托盘可用）")
    else:
        window.show()

    # 旧版自启动项迁移：推迟到窗口出来之后，别让 PowerShell 启动拖慢首屏
    QTimer.singleShot(1200, _migrate_autostart)

    exit_code = app.exec()

    # 退出前等待后台任务收敛，避免线程被强杀
    from .workers import runner

    runner().wait(3000)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
