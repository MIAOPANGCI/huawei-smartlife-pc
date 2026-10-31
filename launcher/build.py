"""打包开机启动器 HuaweiSmartLife.exe（产物放项目根目录）。

为什么要这个 exe：任务管理器「启动应用」显示的是启动目标的产品名，
指向 pythonw.exe 就写「Python」；这个 exe 带了「华为智慧生活 PC 控制台」的
版本资源，自启动项指向它才会显示成应用名。

用法（装过一次 PyInstaller 即可）：
    py -3 launcher/build.py

产物：
    HuaweiSmartLife.exe    启动器（约 7 MB，单文件）
    launcher.json          记录本次打包用的 pythonw 路径（失效时会退回 PATH）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER_DIR = os.path.join(ROOT, "launcher")
BUILD_DIR = os.path.join(ROOT, "build", "launcher")
ENTRY = os.path.join(LAUNCHER_DIR, "launcher.py")
VERSION_FILE = os.path.join(LAUNCHER_DIR, "version_info.txt")
TARGET_NAME = "HuaweiSmartLife"


def find_pythonw() -> str:
    """打包用的解释器对应的 pythonw（开机时不弹黑框）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = sys.executable or ""
    windowed = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return windowed if os.path.exists(windowed) else exe


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先执行：py -3 -m pip install pyinstaller")
        return 1

    # launcher.json 记下解释器路径，启动器运行时优先用它
    config_path = os.path.join(ROOT, "launcher.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump({"pythonw": find_pythonw()}, handle, ensure_ascii=False, indent=2)
    print(f"已写入 {config_path}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name",
        TARGET_NAME,
        "--version-file",
        VERSION_FILE,
        "--distpath",
        ROOT,
        "--workpath",
        BUILD_DIR,
        "--specpath",
        BUILD_DIR,
        ENTRY,
    ]
    print("打包中：", " ".join(command[:6]), "…")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print("打包失败")
        return result.returncode

    exe = os.path.join(ROOT, f"{TARGET_NAME}.exe")
    if not os.path.exists(exe):
        print("打包命令成功但没有产物：" + exe)
        return 1
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"完成：{exe}（{size:.1f} MB）")

    info = _read_version(exe)
    if info:
        print("版本信息：", info)
    return 0


def _read_version(exe: str) -> str:
    """读回 exe 的版本资源，确认任务管理器会显示中文名。

    用 PowerShell 走 VersionInfo 最简单，但环境里可能禁用它；读不到不算失败。
    """
    script = (
        "$v=(Get-Item -LiteralPath '{}').VersionInfo;"
        "Write-Output ($v.FileDescription + ' | ' + $v.ProductName)"
    ).format(exe.replace("'", "''"))
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("mbcs", errors="replace").strip()


if __name__ == "__main__":
    raise SystemExit(main())
