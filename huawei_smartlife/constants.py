"""华为智慧生活（非官方）客户端常量。

这些常量来自对华为智慧生活 App 云端协议的逆向研究（参考社区开源实现
ha-huawei-smarthome）。华为并未向个人开放官方 API，接口/参数可能随华为
更新而失效。本文件仅用于个人学习与研究，请遵守华为服务条款。
"""

# ---- 基础域名 ----
ACCOUNT_BASE_URL = "https://hwid-drcn.platform.hicloud.com"   # 华为账号
OAUTH_BASE_URL = "https://oauth-login.platform.hicloud.com"     # OAuth
SMART_HOME_BASE_URL = "https://smarthome.hicloud.com"           # 智慧生活主服务
CAS_BASE_URL = "https://id1.cloud.huawei.com"                  # 账号网页层(CAS)

# ---- App 标识 ----
SMART_HOME_APP_ID = "com.huawei.smarthome"
SMART_HOME_IOS_APP_ID = "com.huawei.smarthome-ios"
SMART_HOME_OAUTH_CLIENT_ID = "10406921"
SMART_HOME_ACCOUNT_VERSION = "69100"
SMART_HOME_ACCOUNT_CLIENT_VERSION = "ios_HwID_6.10.0.300"
SMART_HOME_ACCOUNT_USER_AGENT = (
    "SmartHome/17.0.3.320 CFNetwork/1335.0.3.4 Darwin/21.6.0"
)
SMART_HOME_USER_AGENT = "SmartHome/1.0.842 (iPhone; iOS 15.8.8; Scale/2.00)"
SMART_HOME_APP_VERSION = "17.0.3.320"

# ---- REST 端点路径 ----
DEVICE_SNAPSHOT_PATH = "/smart-life/v5/devices/info"
DEVICE_DYNAMIC_DATA_PATH = "/smart-life/v5/devices/dynamic/data"
HOME_SNAPSHOT_PATH = "/smart-life/v5/homes"
DEVICE_DETAIL_PATH = "/smart-life/v2/devices/{dev_id}"
MESSAGE_CENTER_LOGIN_PATH = "/message-center/v1/login"
CLOUD_ROUTE_SELECT_PATH = "/trs/v1/app/route/select"
HMS_LITE_TOKEN_PATH = "/smart-life/v2/hms-lite/token"

# ---- 智能场景 ----
# 注意：以下场景端点是基于社区逆向的合理推断，并非华为公开文档。真实路径/参数
# 可能随版本变化；若实际返回 404/业务错误，请用抓包工具核对后调整本常量。
SCENE_LIST_PATH = "/smart-life/v5/scenes"
SCENE_EXECUTE_PATH = "/smart-life/v5/scene/execute"

# ---- MQTT ----
MQTT_PORT = 8883
MQTT_FILTER = "/smartHome/signaltrans/v2/categories/command"
MQTT_SUBSCRIPTION_QOS = 2
# 旁听用通配订阅（校准命令格式时使用）：捕获官方 App 下发的真实命令。
# 遥测本身走服务端推送，不依赖这些订阅；旁听订阅失败不影响主连接。
MQTT_CAPTURE_FILTERS = ("/smartHome/signaltrans/#",)

# ---- CAS 账号网页层（用于下发短信验证码）----
CAS_LOGIN_PAGE = f"{CAS_BASE_URL}/CAS/portal/login.html"
CAS_AJAX_PATH = "/CAS/IDM_W/ajaxHandler"
CAS_CVERSION_BASE = "UP_CAS_6.26.2.100"
CAS_CVERSION_FALLBACK = f"{CAS_CVERSION_BASE}_blue"
CAS_BASE_SWITCH_ACTION = "common/getBaseSwitchInfo"
CAS_SERVICE = f"{CAS_BASE_URL}/AMW/portal/userCenter/index.html"
CAS_REQUEST_CLIENT_TYPE = "7"
CAS_LOGIN_CHANNEL = "7000000"
CAS_LANGUAGE = "zh-cn"
CAS_SITE_ID = "1"
CAS_PAGE_INFO_ACTION = "login/getPageInfo"
CAS_PAGE_NAME = "login"
CAS_ACCOUNT_TYPE_PHONE = "2"
CAS_SMS_OPER_TYPE = "8"
CAS_SMS_REQUEST_TYPE = "6"

# ---- OAuth scope ----
SCOPE_FOR_CODE = (
    "openid https://www.huawei.com/auth/account/accountlist "
    "https://www.huawei.com/auth/account/birthday "
    "https://www.huawei.com/auth/account/base.profile"
)
SCOPE_FOR_SESSION = (
    "openid https://www.huawei.com/auth/account/mobile.number email "
    "https://www.huawei.com/auth/account/accountlist "
    "https://www.huawei.com/auth/account/base.profile"
)
