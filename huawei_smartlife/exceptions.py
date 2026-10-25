"""华为智慧生活客户端异常定义。"""


class HuaweiSmartLifeError(Exception):
    """所有华为智慧生活客户端异常的基类。"""


class AuthenticationError(HuaweiSmartLifeError):
    """认证失败（账号/密码/验证码/Token 问题）。"""


class InvalidCredentialsError(AuthenticationError):
    """账号或密码/验证码无效。"""


class TransientAuthenticationError(AuthenticationError):
    """临时性认证错误（可重试）。"""


class TransientNetworkError(HuaweiSmartLifeError):
    """临时性网络错误（可重试）。"""


class RemoteOperationError(HuaweiSmartLifeError):
    """远端返回业务错误。"""


class AuthExpiredError(HuaweiSmartLifeError):
    """会话或 Token 已过期，需要重新登录。"""


class PermissionDeniedError(HuaweiSmartLifeError):
    """没有权限访问资源。"""


class RateLimitedError(HuaweiSmartLifeError):
    """请求过于频繁被限流。"""


class InvalidResponseError(HuaweiSmartLifeError):
    """响应格式不符合预期。"""


class MqttConnectionError(HuaweiSmartLifeError):
    """MQTT 连接不可用。"""


class MqttConfigurationError(HuaweiSmartLifeError):
    """MQTT 配置无效。"""


class MqttProtocolError(HuaweiSmartLifeError):
    """MQTT Broker 拒绝了协议操作。"""
