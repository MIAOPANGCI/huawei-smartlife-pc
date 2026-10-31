"""华为智慧生活 PC 客户端（非官方）。

仅供个人学习与研究使用，请遵守华为相关服务条款。华为未向个人开放官方 API，
接口与参数可能随版本更新而失效。
"""

from .camera import (
    PLAYER_NAMES,
    NvrSupport,
    RtspCamera,
    consumer_camera_guidance,
    filter_cameras,
    find_player,
    identify_hosts,
    looks_like_camera,
    local_ipv4,
    nvr_support,
    parse_rtsp_url,
    rtsp_banner,
    scan_lan,
)
from .client import HuaweiSmartLifeClient
from .exceptions import (
    AuthenticationError,
    AuthExpiredError,
    HuaweiSmartLifeError,
    InvalidCredentialsError,
    InvalidResponseError,
    MqttConfigurationError,
    MqttConnectionError,
    PermissionDeniedError,
    RateLimitedError,
    RemoteOperationError,
    TransientAuthenticationError,
    TransientNetworkError,
)
from .models import (
    AuthSession,
    ChallengeChannel,
    Device,
    DeviceState,
    DiscoverySnapshot,
    Home,
    LoginChallenge,
    LoginStart,
    MessageChannelSession,
    Scene,
    SmartHomeCloudRoute,
)

from .version import VERSION as __version__

__all__ = [
    "HuaweiSmartLifeClient",
    "PLAYER_NAMES",
    "NvrSupport",
    "RtspCamera",
    "consumer_camera_guidance",
    "filter_cameras",
    "find_player",
    "identify_hosts",
    "looks_like_camera",
    "local_ipv4",
    "nvr_support",
    "parse_rtsp_url",
    "rtsp_banner",
    "scan_lan",
    "HuaweiSmartLifeError",
    "AuthenticationError",
    "AuthExpiredError",
    "InvalidCredentialsError",
    "InvalidResponseError",
    "MqttConfigurationError",
    "MqttConnectionError",
    "PermissionDeniedError",
    "RateLimitedError",
    "RemoteOperationError",
    "TransientAuthenticationError",
    "TransientNetworkError",
    "AuthSession",
    "ChallengeChannel",
    "Device",
    "DeviceState",
    "DiscoverySnapshot",
    "Home",
    "LoginChallenge",
    "LoginStart",
    "MessageChannelSession",
    "Scene",
    "SmartHomeCloudRoute",
    "__version__",
]
