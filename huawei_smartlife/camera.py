"""华为 / 鸿蒙智选摄像头实时观看辅助模块（非官方）。

**能不能在电脑上看实时画面，取决于型号，不是软件问题。**

  ✅ 开放 RTSP 的型号（小豚当家室外 3K、3K 升级版、3K 升级版星闪款、5 4K 版）：
     在「华为智慧生活」App 里进入摄像头 → 右上角 ∷ → 设置 →「硬盘录像机」→
     打开「连接外部硬盘录像机」并单独设一个账号密码（≠ 华为账号），
     就能在内网用标准 RTSP 取流：

         rtsp://账号:密码@内网IP/onvif0    主码流（高清，适合录像）
         rtsp://账号:密码@内网IP/onvif1    子码流（低码率，预览更省带宽）

     RTSP 端口 554，ONVIF 发现端口 5000。摄像头必须与电脑在同一局域网、
     同一网段，不能网线直连。

  ❌ 不开放的型号（AI 全彩双频版、鲸瞳 / 鲸瞳 Max / 鲸瞳 4G、室外 4G 版、
     T5 3K 双摄版、智能摄像头 6 4K，以及全部室内机）：实时画面走华为云
     私有加密通道，第三方无法还原，只能手机 App 或智慧生活网页端观看。

本模块只做「识别型号 + 拼地址 + 探端口 + 拉起播放器」，不碰任何云鉴权逻辑。
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

from .models import Device

_LOGGER = logging.getLogger(__name__)

# 名称/型号/产品ID 中出现这些关键词时，判定为摄像头类设备
_CAMERA_KEYWORDS = (
    "camera",
    "cam",
    "ipc",
    "摄像头",
    "摄像",
    "监控",
    "cctv",
    "nvr",
    "dvr",
    "小豚",
)
# 设备服务中出现这些服务ID时，也判定为摄像头
_CAMERA_SERVICE_IDS = ("video", "camera", "liveview", "live", "rtsp", "media")

# 明确不开放 RTSP 的型号特征（先判否定，因为「室外 4G 版」同时含「室外」）
_BLOCKED_HINTS = (
    ("双频", "AI 全彩双频版不开放 RTSP"),
    ("4g", "4G 版不开放 RTSP"),
    ("t5", "T5 3K 双摄版不开放 RTSP"),
    ("鲸瞳", "鲸瞳系列不开放 RTSP"),
    ("室内", "室内机型不开放 RTSP"),
    ("摄像头6", "智能摄像头 6 不开放 RTSP"),
    ("摄像头 6", "智能摄像头 6 不开放 RTSP"),
    ("camera 6", "智能摄像头 6 不开放 RTSP"),
)
# 开放 RTSP 的型号特征：室外 + 3K/4K 分辨率
_OUTDOOR_HINTS = ("室外", "户外", "outdoor")
_OPEN_RES_HINTS = ("3k", "4k")

RTSP_PORT = 554
ONVIF_PORT = 5000


# ----------------------------------------------------------------------
# 设备识别
# ----------------------------------------------------------------------
def _haystack(device: Device, prefer_model: bool = True) -> str:
    """拼出用于关键词匹配的字符串。

    ``model`` 是设备自报的型号，可信度最高；为空时才退回用户起的设备名
    （用户可能把室内机命名成「门口摄像头」之类，容易误判）。
    """
    parts: list[str] = []
    if prefer_model and device.model:
        parts.append(str(device.model))
    elif device.name:
        parts.append(str(device.name))
    if device.prod_id:
        parts.append(str(device.prod_id))
    if device.manufacturer:
        parts.append(str(device.manufacturer))
    return " ".join(parts).lower()


def looks_like_camera(device: Device) -> bool:
    """基于名称/型号/产品ID/服务ID 启发式判断设备是否为摄像头。"""
    haystack = " ".join(
        str(x)
        for x in (device.name, device.model, device.prod_id, device.manufacturer)
        if x
    ).lower()
    if any(kw in haystack for kw in _CAMERA_KEYWORDS):
        return True
    for svc in device.services.values():
        sid = (svc.sid or "").lower()
        if any(k in sid for k in _CAMERA_SERVICE_IDS):
            return True
    return False


def filter_cameras(devices: Iterable[Device]) -> list[Device]:
    """从设备列表中筛出摄像头设备。"""
    return [d for d in devices if looks_like_camera(d)]


@dataclass(frozen=True)
class NvrSupport:
    """某台摄像头是否可能开放 RTSP 取流。"""

    ok: bool | None  # True 支持 / False 不支持 / None 型号信息不足
    reason: str


def nvr_support(device: Device) -> NvrSupport:
    """按型号判断是否开放 RTSP（结论来自官方支持文档，非逆向推测）。

    返回 ``ok=None`` 表示型号字段是纯数字或缺失，无法从名字判断——
    这种情况让用户直接去 App 里看有没有「硬盘录像机」这一项最准。
    """
    text = _haystack(device)
    if not text.strip():
        return NvrSupport(None, "设备没有上报型号，无法判断")

    for hint, reason in _BLOCKED_HINTS:
        if hint in text:
            return NvrSupport(False, reason)

    outdoor = any(hint in text for hint in _OUTDOOR_HINTS)
    open_res = any(hint in text for hint in _OPEN_RES_HINTS)
    if outdoor and open_res:
        return NvrSupport(True, "室外机型，官方开放 RTSP/ONVIF 取流")

    if not device.model:
        return NvrSupport(None, "型号未上报，去 App 看有没有「硬盘录像机」选项")
    return NvrSupport(
        None, f"型号「{device.model}」不在已知清单里，去 App 看有没有「硬盘录像机」选项"
    )


def consumer_camera_guidance(device: Device) -> str:
    """消费级华为云摄像头（未开放 RTSP）的观看指引。"""
    support = nvr_support(device)
    return (
        f"设备「{device.name}」的实时画面走华为云私有加密通道，"
        f"第三方无法在电脑上还原。\n\n原因：{support.reason}\n\n"
        "可行的观看方式：\n"
        "  1. 手机端：打开「华为智慧生活」App，进入该摄像头实时画面；\n"
        "  2. 网页端：登录 https://smarthome.hicloud.com 查看（部分账号可用）；\n"
        "  3. 同一华为账号 + 同一局域网时，可用华为手机的「畅连」共享看护画面。\n\n"
        "想长期在电脑上看，只能换开放 RTSP 的型号（如小豚当家室外 3K / 5 4K 版）。"
    )


# ----------------------------------------------------------------------
# 外部播放器探测
# ----------------------------------------------------------------------
#: 能直接吃 RTSP 地址的常见命令行播放器，按优先级排列
PLAYER_NAMES = ("vlc", "ffplay", "mpv")

#: 装在默认位置但没进 PATH 的情况。**VLC 安装器从不改 PATH**，
#: 只靠 shutil.which 会漏掉绝大多数正常安装的 VLC。
_KNOWN_DIRS = {
    "vlc": (
        r"%ProgramFiles%\VideoLAN\VLC",
        r"%ProgramFiles(x86)%\VideoLAN\VLC",
        r"%LOCALAPPDATA%\Programs\VideoLAN\VLC",
        r"%LOCALAPPDATA%\VideoLAN\VLC",
    ),
    "ffplay": (
        r"%ProgramFiles%\ffmpeg\bin",
        r"%ProgramFiles(x86)%\ffmpeg\bin",
        r"C:\ffmpeg\bin",
    ),
    "mpv": (
        r"%ProgramFiles%\mpv",
        r"%LOCALAPPDATA%\Programs\mpv",
        r"%USERPROFILE%\scoop\apps\mpv\current",
    ),
}

#: 厂商在注册表里记的安装目录：(播放器, 根键名, 子键, 值名)
_REGISTRY_DIRS = (
    ("vlc", "HKEY_LOCAL_MACHINE", r"SOFTWARE\VideoLAN\VLC", "InstallDir"),
    ("vlc", "HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\VideoLAN\VLC", "InstallDir"),
    ("vlc", "HKEY_CURRENT_USER", r"SOFTWARE\VideoLAN\VLC", "InstallDir"),
)


def _exe_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _registry_candidates(name: str) -> list[str]:
    """从注册表里挖播放器路径。

    ``App Paths`` 存的是可执行文件完整路径，最准；厂商自己的键只记安装目录，
    拼上可执行文件名即可。两者都是「装了但不在 PATH」时唯一的线索。
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # 非 Windows 或精剪运行环境
        return []

    exe = _exe_name(name)
    found: list[str] = []

    for root_name in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"):
        sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
        try:
            with winreg.OpenKey(getattr(winreg, root_name), sub) as key:
                value, _ = winreg.QueryValueEx(key, None)  # 默认值
                if isinstance(value, str) and value.strip():
                    found.append(value.strip().strip('"'))
        except OSError:
            continue

    for vendor, root_name, sub, value_name in _REGISTRY_DIRS:
        if vendor != name:
            continue
        try:
            with winreg.OpenKey(getattr(winreg, root_name), sub) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                if isinstance(value, str) and value.strip():
                    found.append(os.path.join(value.strip().strip('"'), exe))
        except OSError:
            continue

    return found


def find_player(name: str | None = None) -> str | None:
    """返回可用的播放器可执行文件路径；全都没有时返回 None。

    name 为 None 时按 :data:`PLAYER_NAMES` 的顺序取第一个能用的。
    查找顺序：PATH → 注册表 → 默认安装目录。第 2、3 步不能省，
    Windows 上 VLC 装完不会进 PATH，只看 PATH 等于判定「没装」。
    """
    candidates = (name,) if name else PLAYER_NAMES
    for candidate in candidates:
        exe = _locate_player(candidate)
        if exe:
            return exe
    return None


def _locate_player(name: str) -> str | None:
    # 允许直接传可执行文件路径（which 对完整路径同样有效）
    exe = shutil.which(name)
    if exe:
        return exe

    if os.path.isabs(name) and os.path.isfile(name):
        return name

    for path in _registry_candidates(name):
        if os.path.isfile(path):
            return path

    exe = _exe_name(name)
    for folder in _KNOWN_DIRS.get(name, ()):
        path = os.path.join(os.path.expandvars(folder), exe)
        if os.path.isfile(path):
            return path
    return None


# ----------------------------------------------------------------------
# RTSP 地址
# ----------------------------------------------------------------------
#: 码流 → RTSP 路径尾段。**这是实测结果，不是 ONVIF 规范**：
#: 小豚当家室外 3K 版上 onvif0 不存在、onvif1 是 2880×1620 主码流、
#: onvif2 是 640×368 子码流。通道号从 1 开始，跟海康/大华那套不一样，
#: 也不能按「0=主 1=子」推。换型号时用界面上的「自动识别」实测。
STREAM_PATHS = {"main": "onvif1", "sub": "onvif2"}

#: 自动识别时依次尝试的通道（命中即止，见 camera_dialog 里的探测线程）
CANDIDATE_PATHS = (
    "onvif1",
    "onvif2",
    "onvif0",
    "onvif3",
    "live/ch0",
    "live/ch1",
)


@dataclass
class RtspCamera:
    """一台可用标准 RTSP 取流的摄像头。"""

    name: str
    host: str
    username: str = ""
    password: str = ""
    port: int = RTSP_PORT
    stream: str = "sub"  # main（主码流高清）/ sub（子码流省带宽）
    path: str | None = None  # 显式指定时优先，留空按 stream 推断
    device_id: str | None = None

    @property
    def resolved_path(self) -> str:
        if self.path:
            return self.path.strip("/")
        return STREAM_PATHS.get(self.stream, STREAM_PATHS["sub"])

    @property
    def rtsp_url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"rtsp://{auth}{self.host}:{self.port}/{self.resolved_path}"

    @property
    def masked_url(self) -> str:
        """打码后的地址，用于日志与界面展示。"""
        auth = f"{quote(self.username, safe='')}:***@" if self.username else ""
        return f"rtsp://{auth}{self.host}:{self.port}/{self.resolved_path}"

    def launch_player(self, player: str | None = None) -> str:
        """用本地播放器打开 RTSP 流，返回实际使用的命令信息。

        player 可选：'vlc' / 'ffplay' / 'mpv'，也可以直接给可执行文件路径；
        为 None 时按系统可用性自动选择。
        """
        candidates = [player] if player else list(PLAYER_NAMES)
        url = self.rtsp_url
        for name in candidates:
            exe = find_player(name)
            if not exe:
                continue
            # 入参可能是完整路径，认品牌要看可执行文件名，不能直接比字符串
            brand = os.path.splitext(os.path.basename(exe))[0].lower()
            try:
                if brand == "vlc":
                    subprocess.Popen(
                        [
                            exe,
                            url,
                            "--no-video-title-show",
                            # 3K 码流走 UDP 一丢包就花屏/黑屏，实时监控必须吃 TCP
                            "--rtsp-tcp",
                            # 缓存 500ms 对 3K 太浅，VLC 会不停丢帧追赶，
                            # 表现为进度时间来回跳、画面一顿一顿
                            "--network-caching=1500",
                            # 有的 RTSP 源 PTS 抖动会让 VLC 主时钟反复校正，
                            # 进度显示跟着回退；置 0 表示严格跟随流时间戳
                            "--clock-jitter=0",
                        ]
                    )
                elif brand == "ffplay":
                    subprocess.Popen(
                        [exe, "-fflags", "nobuffer", "-flags", "low_delay", url]
                    )
                else:  # mpv
                    subprocess.Popen([exe, url])
                _LOGGER.info("已用 %s 打开 RTSP 流: %s", brand, self.masked_url)
                return f"已使用 {brand} 打开 RTSP 流（{self.masked_url}）"
            except OSError as error:
                _LOGGER.warning("启动 %s 失败: %s", brand, error)
                continue
        raise RuntimeError(
            "没找到可用的本地播放器（VLC / ffplay / mpv）。这台电脑可以直接用"
            "窗口内嵌播放，无需安装任何播放器。"
        )


def parse_rtsp_url(url: str, name: str = "摄像头") -> RtspCamera:
    """把用户粘贴的 rtsp:// 地址解析成 RtspCamera（用于「直接用地址」的场景）。"""
    text = url.strip()
    if not text:
        raise ValueError("RTSP 地址为空")
    if "://" not in text:
        text = "rtsp://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() != "rtsp":
        raise ValueError(f"不是 RTSP 地址：{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"地址里没有主机：{url}")
    path = parsed.path.lstrip("/") or None
    return RtspCamera(
        name=name,
        host=host,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        port=parsed.port or RTSP_PORT,
        path=path,
        stream="main" if path == STREAM_PATHS["main"] else "sub",
    )


# ----------------------------------------------------------------------
# 网络探测
# ----------------------------------------------------------------------
def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_open(host: str, port: int = RTSP_PORT, timeout: float = 2.0) -> bool:
    """554 端口通不通。

    开流之前先问这一句很值：OpenCV 打开一个不通的地址会死等 30 秒
    （CAP_PROP_OPEN_TIMEOUT_MSEC 在 5.0 上不生效），而 socket 只要 2 秒。
    常见的「IP 填错 / 不在同一网段」能在这一步秒回。
    """
    return _port_open(host, port, timeout)


def local_ipv4() -> str | None:
    """本机在局域网里的 IPv4 地址（UDP connect 不会真的发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def rtsp_banner(host: str, port: int = RTSP_PORT, timeout: float = 1.5) -> str | None:
    """发一条 RTSP OPTIONS，确认 554 上跑的确实是 RTSP 服务。

    返回 ``Server`` 头（或状态行）；端口不通/不是 RTSP 时返回 None。
    未认证的摄像头通常回 ``RTSP/1.0 401 Unauthorized``，同样以 ``RTSP/`` 开头，
    所以这里只判前缀，不做鉴权。
    """
    request = (
        f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: huawei-smartlife-pc\r\n\r\n"
    )
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request.encode("ascii", "ignore"))
            data = sock.recv(1024)
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    if not text.startswith("RTSP/"):
        return None
    for line in text.splitlines():
        if line.lower().startswith("server:"):
            return line.split(":", 1)[1].strip() or text.splitlines()[0].strip()
    return text.splitlines()[0].strip()


def scan_lan(
    port: int = RTSP_PORT,
    timeout: float = 0.45,
    workers: int = 64,
    subnet: str | None = None,
) -> list[str]:
    """扫描本机所在 /24 网段的指定端口，返回开着的 IP（已排序）。

    254 个地址 × 0.45s ÷ 64 并发 ≈ 2 秒，可以直接在界面线程外跑。
    """
    if subnet:
        prefix = subnet.rstrip(".")
    else:
        ip = local_ipv4()
        if not ip:
            return []
        prefix = ".".join(ip.split(".")[:3])
    targets = [f"{prefix}.{n}" for n in range(1, 255)]
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, opened in zip(
            targets, pool.map(lambda t: _port_open(t, port, timeout), targets)
        ):
            if opened:
                found.append(host)
    return sorted(found, key=lambda a: int(a.rsplit(".", 1)[-1]))


def identify_hosts(hosts: Iterable[str], port: int = RTSP_PORT) -> list[tuple[str, str]]:
    """对扫描结果逐个发 OPTIONS，返回 (IP, Server 头)，拿不到 Server 的跳过。"""
    result: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for host, banner in zip(hosts, pool.map(lambda h: rtsp_banner(h, port), hosts)):
            if banner:
                result.append((host, banner))
    return result
