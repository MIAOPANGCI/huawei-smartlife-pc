"""打包完整应用（不是 launcher/ 那个转发器）并压成发布用的 zip。

产物：
    dist/HuaweiSmartLife/HuaweiSmartLife.exe     可直接运行，不需要 Python
    dist/HuaweiSmartLife-<版本>-win64.zip         上传 GitHub Release 用

版本号只有一个来源：``huawei_smartlife/version.py``。exe 的版本资源、zip 文件名、
Release tag 都从它派生，避免出现"界面写 1.0.0、exe 属性写 0.9"这种对不上的情况。

为什么用 onedir 而不是 onefile：
onefile 每次启动都要把上百 MB 解压到临时目录，冷启动要多等好几秒；onedir 双击即开，
代价是发布时多一层目录——反正要压 zip，没区别。

用法：
    py -3 packaging/build_release.py            # 打包 + 压缩
    py -3 packaging/build_release.py --no-zip   # 只打包，调试用
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BUILD_DIR = os.path.join(ROOT, "build", "release")
DIST_DIR = os.path.join(ROOT, "dist")
APP_NAME = "HuaweiSmartLife"
ENTRY = os.path.join(ROOT, "main.py")

# 这些模块要么没用到，要么会把体积撑大一倍
EXCLUDES = (
    "tkinter",
    "unittest",
    "pydoc",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
)


def read_version() -> str:
    from huawei_smartlife.version import VERSION

    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        raise SystemExit(f"版本号格式不对：{VERSION}（应为 主.次.修订）")
    return VERSION


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(item) for item in version.split(".")]
    return (parts[0], parts[1], parts[2], 0)


def write_version_file(path: str, version: str) -> None:
    """PyInstaller 的版本资源：任务管理器 / 文件属性里看到的就是这些字段。"""
    numbers = version_tuple(version)
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'HuaweiSmartLifePC'),
        StringStruct('FileDescription', '华为智慧生活 PC 控制台'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '仅供个人学习与研究使用'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '华为智慧生活 PC 控制台'),
        StringStruct('ProductVersion', '{version}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ],
)
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def build(version: str) -> str:
    from launcher.make_icon import build_ico

    os.makedirs(BUILD_DIR, exist_ok=True)
    icon = build_ico(os.path.join(BUILD_DIR, "app.ico"))
    version_file = os.path.join(BUILD_DIR, "version_info.txt")
    write_version_file(version_file, version)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        "--name",
        APP_NAME,
        "--icon",
        icon,
        "--version-file",
        version_file,
        "--distpath",
        DIST_DIR,
        "--workpath",
        BUILD_DIR,
        "--specpath",
        BUILD_DIR,
        # 摄像头窗口在函数里 import cv2，显式声明免得静态分析漏掉
        "--hidden-import",
        "cv2",
        "--hidden-import",
        "paho.mqtt.client",
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(ENTRY)

    print("开始打包（首次会慢一些）…", flush=True)
    started = time.time()
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("打包失败")

    exe = os.path.join(DIST_DIR, APP_NAME, f"{APP_NAME}.exe")
    if not os.path.exists(exe):
        raise SystemExit(f"打包命令成功但没有产物：{exe}")
    print(f"打包完成，用时 {time.time() - started:.0f}s")
    return exe


def folder_size_mb(path: str) -> float:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(base, name))
    return total / 1024 / 1024


def make_zip(version: str) -> str:
    zip_path = os.path.join(DIST_DIR, f"{APP_NAME}-{version}-win64.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    folder = os.path.join(DIST_DIR, APP_NAME)
    files = []
    for base, _dirs, names in os.walk(folder):
        for name in names:
            full = os.path.join(base, name)
            files.append((full, os.path.relpath(full, DIST_DIR)))

    print(f"压缩 {len(files)} 个文件…", flush=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for full, relative in files:
            archive.write(full, relative)
    return zip_path


def main() -> int:
    no_zip = "--no-zip" in sys.argv
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先执行：py -3 -m pip install pyinstaller")
        return 1

    version = read_version()
    print(f"版本：{version}")

    exe = build(version)
    folder = os.path.join(DIST_DIR, APP_NAME)
    print(f"产物目录：{folder}（{folder_size_mb(folder):.1f} MB）")
    print(f"可执行文件：{exe}")

    if no_zip:
        return 0

    zip_path = make_zip(version)
    print(f"发布包：{zip_path}（{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB）")

    # 上一次的临时启动器产物会和完整 exe 撞名，提醒一句免得混淆
    stray = os.path.join(ROOT, f"{APP_NAME}.exe")
    if os.path.exists(stray):
        print(
            f"提示：项目根还有 {APP_NAME}.exe（源码运行时的自启动启动器），"
            "和发布包不是同一个东西"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
