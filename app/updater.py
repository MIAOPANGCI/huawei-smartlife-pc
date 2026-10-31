"""检查更新与一键更新。

两种运行形态走两条路：

* **源码运行**（目录里有 ``.git``）：``git pull``。git 自己处理冲突与快进，
  更新完提示重启即可，不碰文件。
* **打包运行**（``sys.frozen``）：下载 Release 里的 zip，解压到临时目录，再让
  **新版本自己**以 ``--apply-update`` 参数去覆盖旧目录。正在运行的 exe 不能被
  覆盖，所以替换必须由一个"已经跑在新目录里的进程"来做。

为什么不用 .bat 收尾：黑框会闪一下，路径含中文/空格时容易被引号坑死，
杀毒软件对"脚本替换 exe"也更敏感。让新版本自己干，编码和路径都由 Python 处理。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = "MIAOPANGCI/huawei-smartlife-pc"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_RELEASES = f"https://github.com/{REPO}/releases"

APP_NAME = "HuaweiSmartLife"
APPLY_FLAG = "--apply-update"

#: 解压目录固定，避免每次更新在临时目录里堆一份上百 MB 的残留
STAGE_DIR_NAME = "HuaweiSmartLife-update"
CHUNK = 256 * 1024


class UpdateError(RuntimeError):
    """检查 / 下载 / 替换过程中的可预期失败，界面直接显示这句话。"""


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    zip_url: str
    zip_size: int
    page_url: str


# ======================================================================
# 版本
# ======================================================================
def parse_version(text: str) -> tuple[int, int, int]:
    """``v1.2.3`` / ``1.2`` → (1,2,3) / (1,2,0)，解析不了当作 0。"""
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split(".")[:3]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ======================================================================
# 查询
# ======================================================================
def _system_proxy() -> str | None:
    """Windows「Internet 选项」里配的代理（Clash 之类工具的"系统代理"开关）。

    urllib 只认环境变量、不读注册表：只开了系统代理的用户会看到"连不上 GitHub"，
    明明浏览器能打开。所以这里补一手，环境变量优先（显式配置更该被尊重）。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _kind = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _kind = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None

    text = str(server or "").strip()
    if not text:
        return None
    if "=" in text:      # http=127.0.0.1:7890;https=127.0.0.1:7890
        for part in text.split(";"):
            if part.lower().startswith("http="):
                return "http://" + part.split("=", 1)[1]
        return None
    return "http://" + text


def _open(url: str, timeout: float):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}-updater",
        },
    )
    handlers = []
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    proxy = _system_proxy()
    if proxy and not env_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def fetch_latest(timeout: float = 12.0) -> Release:
    """查最新 Release。成功返回 Release，失败抛 UpdateError（信息给用户看）。"""
    try:
        with _open(API_LATEST, timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("仓库里还没有发布任何版本") from error
        if error.code == 403:
            raise UpdateError("查询太频繁，被 GitHub 限流了，过一会儿再试") from error
        raise UpdateError(f"查询失败：HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"连不上 GitHub（{error}），检查网络或代理") from error
    except json.JSONDecodeError as error:
        raise UpdateError("GitHub 返回的内容看不懂，稍后再试") from error

    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError("最新版本没有 tag，无法比较版本")
    version = tag.lstrip("vV")

    asset_url = ""
    asset_size = 0
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.endswith(".zip") and "win64" in name:
            asset_url = str(asset.get("browser_download_url") or "")
            asset_size = int(asset.get("size") or 0)
            break
    if not asset_url:
        raise UpdateError(f"{tag} 没有可下载的 win64 安装包")

    return Release(
        version=version,
        tag=tag,
        notes=str(data.get("body") or ""),
        zip_url=asset_url,
        zip_size=asset_size,
        page_url=str(data.get("html_url") or PAGE_RELEASES),
    )


# ======================================================================
# 下载
# ======================================================================
def download(url: str, dest: Path, progress: dict | None = None, timeout: float = 60.0) -> Path:
    """下载到 dest；progress 是个字典，界面靠轮询它显示进度。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    if progress is not None:
        progress.update(done=0, total=0, state="downloading")

    try:
        with _open(url, timeout) as response, open(dest, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            if progress is not None:
                progress["total"] = total
            done = 0
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress["done"] = done
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"下载失败：{error}") from error

    if progress is not None:
        progress["state"] = "done"
    if dest.stat().st_size < 1024:
        raise UpdateError("下载到的文件不完整")
    return dest


# ======================================================================
# 源码模式：git pull
# ======================================================================
def source_root() -> Path | None:
    """源码运行且是真 git 仓库时返回仓库根目录。"""
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def git_pull(root: Path | None = None) -> str:
    """拉取最新代码，返回给人看的一句话。"""
    root = root or source_root()
    if root is None:
        raise UpdateError("当前不是 git 仓库，无法用 git 更新")
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"执行 git pull 失败：{error}") from error

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        tail = output.strip().splitlines()[-3:]
        raise UpdateError("拉取失败：" + " / ".join(tail) if tail else "拉取失败")
    if "Already up to date" in output or "已经是最新" in output:
        return "已经是最新版本"
    if "Fast-forward" in output or "快进" in output:
        return "代码已更新，重启程序后生效"
    return "已拉取，重启程序后生效"


# ======================================================================
# 打包模式：解压 + 交接给新版本
# ======================================================================
def stage(zip_path: Path, workdir: Path | None = None) -> Path:
    """把 zip 解到临时目录，返回新版本 exe 所在目录。"""
    workdir = workdir or (Path(os.environ.get("TEMP", ".")) / STAGE_DIR_NAME)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(workdir)
    except (zipfile.BadZipFile, OSError) as error:
        raise UpdateError(f"解压失败：{error}") from error

    # zip 里带一层 HuaweiSmartLife/ 目录
    for candidate in (workdir / APP_NAME, workdir):
        if (candidate / f"{APP_NAME}.exe").exists():
            return candidate
    raise UpdateError("下载的包里找不到主程序")


def target_dir() -> Path:
    """正在运行的打包版所在目录（源码模式返回项目根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def launch_updater(new_dir: Path, wait_pid: int | None = None) -> None:
    """从解压出的新版本里拉起"更新执行者"，它会在本进程退出后替换文件。"""
    exe = new_dir / f"{APP_NAME}.exe"
    if not exe.exists():
        raise UpdateError(f"新版本里没有 {APP_NAME}.exe")

    command = [
        str(exe),
        APPLY_FLAG,
        "--target",
        str(target_dir()),
        "--wait-pid",
        str(wait_pid if wait_pid is not None else os.getpid()),
    ]
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            command,
            cwd=str(new_dir),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise UpdateError(f"启动更新程序失败：{error}") from error


def _arg(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _wait_for_exit(pid: int, timeout_ms: int = 120000) -> None:
    """等旧进程退出，否则它的 exe 还占着文件，复制会失败。"""
    if pid <= 0:
        return
    if sys.platform.startswith("win"):
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
            ctypes.windll.kernel32.CloseHandle(handle)
            return
    # 非 Windows / 打不开句柄时退化为轮询
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def apply_update(argv: list[str]) -> int:
    """``--apply-update`` 模式：把新版本覆盖到目标目录，再把它启动起来。

    先改名备份再复制：复制中途失败就把备份改回来，不至于把程序弄成半新半旧。
    """
    target_text = _arg(argv, "--target")
    if not target_text:
        return 2
    target = Path(target_text).resolve()
    # --source 只给测试和手动调试用；正常流程就是"自己所在的目录"
    source_text = _arg(argv, "--source")
    new_dir = (
        Path(source_text).resolve() if source_text
        else Path(sys.executable).resolve().parent
    )
    if not target.exists() or not new_dir.exists():
        return 3

    _wait_for_exit(int(_arg(argv, "--wait-pid") or 0))
    # 进程刚退出时文件句柄可能还没释放干净
    for attempt in range(10):
        try:
            backup = target.with_name(target.name + ".old")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            target.rename(backup)
            break
        except OSError:
            if attempt == 9:
                return 4
            time.sleep(0.5)

    try:
        shutil.copytree(new_dir, target, dirs_exist_ok=True)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        try:
            backup.rename(target)
        except OSError:
            pass
        return 5

    exe = target / f"{APP_NAME}.exe"
    try:
        subprocess.Popen([str(exe)], cwd=str(target), close_fds=True)
    except OSError:
        return 6

    shutil.rmtree(backup, ignore_errors=True)
    return 0
