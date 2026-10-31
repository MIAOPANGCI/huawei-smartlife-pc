"""开机自启动（Windows）。

为什么用「启动」文件夹里的快捷方式，而不是 Run 注册表项：
任务管理器的「启动应用」里，Run 项显示的是可执行文件名——我们的启动方式是
``pythonw.exe main.py``，于是那一栏就写着「python」，用户根本认不出这是谁，
还以为机器上多了个可疑的自启项。快捷方式的名字就是显示名，改成
「华为智慧生活 PC 控制台」就认得出了。

早期版本写的是 Run 键（``HuaweiSmartLifePC``），这里保留清理逻辑：
勾选/取消时都会顺手删掉旧项，避免和新快捷方式同时存在导致启动两次。
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

# ---- 旧版（Run 键）----
VALUE_NAME = "HuaweiSmartLifePC"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---- 新版（启动文件夹快捷方式）----
SHORTCUT_NAME = "华为智慧生活 PC 控制台.lnk"
APP_TITLE = "华为智慧生活 PC 控制台"

#: 打包出来的启动器（`py -3 launcher/build.py`）。有它就用它——任务管理器
#: 「启动应用」显示的是目标程序的产品名，指向 pythonw.exe 只会写「Python」。
LAUNCHER_EXE = "HuaweiSmartLife.exe"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launcher_path() -> str | None:
    """启动器 exe 的路径；没打包过则为 None。"""
    path = os.path.join(_PROJECT_ROOT, LAUNCHER_EXE)
    return path if os.path.exists(path) else None


def supported() -> bool:
    return sys.platform.startswith("win")


# ======================================================================
# 启动命令
# ======================================================================
def _launch_parts() -> tuple[str, str]:
    """(启动目标, 参数)。

    有打包好的 HuaweiSmartLife.exe 就指向它：任务管理器按目标程序的产品名显示，
    那个 exe 的产品名是「华为智慧生活 PC 控制台」。没有就退回 pythonw——
    功能一样能跑，只是任务管理器里会显示成「Python」。
    """
    launcher = launcher_path()
    if launcher is not None:
        return launcher, "--tray"

    if getattr(sys, "frozen", False):
        return sys.executable, "--tray"

    interpreter = sys.executable or "pythonw.exe"
    windowed = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
    if os.path.exists(windowed):
        interpreter = windowed
    return interpreter, f'"{os.path.join(_PROJECT_ROOT, "main.py")}" --tray'


def launch_command() -> str:
    """完整命令行（给人看 / 给测试查）。"""
    interpreter, arguments = _launch_parts()
    return f'"{interpreter}" {arguments}'


# ======================================================================
# 快捷方式
# ======================================================================
def _startup_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming"
    )
    return os.path.join(base, r"Microsoft\Windows\Start Menu\Programs\Startup")


def shortcut_path() -> str:
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串转义：内部的单引号要写两遍。"""
    return value.replace("'", "''")


def _run_powershell(script: str) -> None:
    """用 -EncodedCommand 执行，避免中文路径在 GBK 命令行下变问号。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            creationflags=creationflags,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError(f"调用 PowerShell 失败：{error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("mbcs", errors="replace").strip()
        raise OSError(detail or "创建快捷方式失败")


def _create_shortcut() -> None:
    directory = _startup_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as error:
        raise OSError(f"无法创建启动文件夹：{error}") from error

    interpreter, arguments = _launch_parts()
    script = f"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{_ps_quote(shortcut_path())}')
$shortcut.TargetPath = '{_ps_quote(interpreter)}'
$shortcut.Arguments = '{_ps_quote(arguments)}'
$shortcut.WorkingDirectory = '{_ps_quote(_PROJECT_ROOT)}'
$shortcut.Description = '{_ps_quote(APP_TITLE)}'
$shortcut.Save()
"""
    _run_powershell(script)


# ======================================================================
# 旧版 Run 键
# ======================================================================
def registry_command() -> str | None:
    """旧版写在注册表里的命令；没有返回 None。"""
    if not supported():
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return None


def _remove_registry() -> None:
    """删掉旧版留下的 Run 项。

    不删的话会和新的快捷方式同时生效——开机启动两次，第二次还会因为
    单实例之外的端口/会话竞争出现莫名其妙的重复窗口。
    """
    if not supported():
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except (FileNotFoundError, OSError):
        pass        # 没有就不存在要清理的东西


# ======================================================================
# 对外接口
# ======================================================================
def is_enabled() -> bool:
    """快捷方式在，或旧版注册表项还在，都算「已开启」。"""
    return os.path.exists(shortcut_path()) or registry_command() is not None


def enable() -> None:
    if not supported():
        raise OSError("只有 Windows 支持开机自启动")
    # 先建新的，再把旧项清掉；反过来万一建失败，用户就两头落空
    _create_shortcut()
    _remove_registry()


def disable() -> None:
    if not supported():
        return
    path = shortcut_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as error:
            raise OSError(f"删除快捷方式失败：{error}") from error
    _remove_registry()


def needs_migration() -> bool:
    """旧版只在注册表里登记过、还没换成快捷方式。"""
    return registry_command() is not None and not os.path.exists(shortcut_path())


def migrate() -> bool:
    """把旧版 Run 项换成快捷方式（启动时静默做一次）。

    用户当初勾过自启动，不该因为我们换了实现方式就悄悄失效；
    迁移到快捷方式后，任务管理器里也终于显示成应用名。
    """
    if not needs_migration():
        return False
    try:
        enable()
    except OSError:
        # 迁移失败不影响程序使用，旧项继续生效，用户下次勾选会再试
        return False
    return True
