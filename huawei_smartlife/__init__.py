"""华为智慧生活 PC 客户端（非官方）。

仅供个人学习与研究使用，请遵守华为相关服务条款。华为未向个人开放官方 API，
接口与参数可能随版本更新而失效。
"""

from .camera import (
    RtspCamera,
    consumer_camera_guidance,
    filter_cameras,
    looks_like_camera,
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

__version__ = "0.1.0"

__all__ = [
    "HuaweiSmartLifeClient",
    "RtspCamera",
    "consumer_camera_guidance",
    "filter_cameras",
    "looks_like_camera",
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
