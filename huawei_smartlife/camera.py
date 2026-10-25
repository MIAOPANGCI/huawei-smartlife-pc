"""华为摄像头实时观看（非官方）辅助模块。

重要限制说明（请务必阅读）：
  - 消费级华为/华为智选摄像头（如海雀等）的实时视频流走华为云私有加密协议，
    社区目前没有可靠的、可在 PC 上直接拉流的开源方案。本模块**无法**为这类
    摄像头提供本地实时画面，只能通过官方 App/网页回退观看。
  - 仅「支持标准 RTSP / ONVIF 」的专业/企业级华为摄像机（或用户自行接入的
    ONVIF 摄像头）可以通过本模块在局域网内构造 rtsp:// 地址并用本地播放器
    观看。这类设备需要用户已知其 IP、账号、密码。

本模块不依赖华为账号鉴权，纯粹是「识别摄像头 + 本地拉流/回退提示」的工具。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote

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
)
# 设备服务中出现这些服务ID时，也判定为摄像头
_CAMERA_SERVICE_IDS = ("video", "camera", "liveview", "live", "rtsp", "media")


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


def consumer_camera_guidance(device: Device) -> str:
    """为消费级华为云摄像头返回观看指引。"""
    return (
        f"设备「{device.name}」(ID={device.dev_id}) 是华为云私有协议的消费级摄像头，\n"
        "其视频流经过华为云端加密，社区没有可在 PC 直接拉流的开源方案。\n\n"
        "可行的观看方式：\n"
        "  1. 手机端：打开「华为智慧生活」App，进入该摄像头实时画面；\n"
        "  2. 网页端：登录华为智慧生活网页 https://smarthome.hicloud.com 查看（部分账号可用）；\n"
        "  3. 若你有抓包得到的预览地址，可手动用 VLC 打开（需自行验证可用性）。\n\n"
        "如需在 PC 长期观看，建议改用支持 RTSP/ONVIF 的摄像头（见下方 RTSP 方案）。"
    )


@dataclass
class RtspCamera:
    """支持标准 RTSP 的摄像头（专业/企业级或 ONVIF 设备）。"""

    name: str
    host: str
    username: str
    password: str
    port: int = 554
    path: str = "LiveMedia/ch1/Media1"
    device_id: str | None = None

    @property
    def rtsp_url(self) -> str:
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"rtsp://{user}:{pwd}@{self.host}:{self.port}/{self.path}"

    def launch_player(self, player: str | None = None) -> str:
        """尝试用本地播放器打开 RTSP 流，返回实际使用的命令信息。

        player 可选：'vlc' / 'ffplay' / 'mpv'；为 None 时按系统可用性自动选择。
        """
        candidates = [player] if player else ["vlc", "ffplay", "mpv"]
        url = self.rtsp_url
        for name in candidates:
            exe = shutil.which(name)
            if not exe:
                continue
            try:
                if name == "vlc":
                    subprocess.Popen([exe, url, "--no-video-title-show"])
                elif name == "ffplay":
                    subprocess.Popen([exe, "-fflags", "nobuffer", "-flags", "low_delay", url])
                else:  # mpv
                    subprocess.Popen([exe, url, "--no-cache"])
                _LOGGER.info("已用 %s 打开 RTSP 流: %s", name, url)
                return f"已使用 {name} 打开 RTSP 流（{url}）"
            except OSError as error:
                _LOGGER.warning("启动 %s 失败: %s", name, error)
                continue
        raise RuntimeError(
            "未找到可用的本地播放器（vlc/ffplay/mpv）。请先安装其中之一，"
            "或在调用时显式指定可执行文件路径。"
        )
