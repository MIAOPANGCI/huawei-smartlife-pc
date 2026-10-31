"""临时 bug 探测：拿真实报文序列走一遍 controller 的查询分支。

不起 GUI、不连网，只看「同样的报文进来，状态和日志对不对」。
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.controller import AppController  # noqa: E402
from app.dispatch import attach as init_dispatch  # noqa: E402
from huawei_smartlife.models import (  # noqa: E402
    Device,
    DiscoverySnapshot,
    Home,
    ServiceState,
)

# 编造的固定 UUID：真实报文里的设备 ID 不能进公开仓库
DEV = "0f1e2d3c-4b5a-4c6d-8e9f-0123456789ab"
TIMER_URI = f"/devices/{DEV}/services/timer"
DELAY_URI = f"/devices/{DEV}/services/delay"

FAILS: list[str] = []
PASSES: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def resp(from_uri: str, body: object, method: str = "GET") -> dict:
    return {
        "header": {
            "notifyType": "commandRsp",
            "method": method,
            "from": from_uri,
            "to": "/users/u1",
        },
        "body": body,
    }


def build() -> tuple[AppController, list[tuple[str, str]]]:
    app = QApplication.instance() or QApplication(sys.argv)
    init_dispatch()
    ctrl = AppController()
    logs: list[tuple[str, str]] = []
    ctrl.log.connect(lambda msg, kind: logs.append((kind, msg)))
    ctrl.store.replace_snapshot(
        DiscoverySnapshot(
            homes=(Home(home_id="h1", name="家"),),
            devices=(
                Device(
                    home_id="h1",
                    dev_id=DEV,
                    name="空调",
                    online=True,
                    services={
                        "timer": ServiceState(
                            sid="timer", data={"num": 0, "timer": []}
                        ),
                        "delay": ServiceState(sid="delay", data={"on": 0}),
                    },
                ),
            ),
        )
    )
    app.processEvents()
    return ctrl, logs


def drain(app: QApplication, rounds: int = 40) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.01)


TASKS = [
    {
        "id": 3,
        "enable": 1,
        "week": 0,
        "start": "170000Z",
        "end": "170500Z",
        "para": "on",
        "paraValue": 1,
        "sid": "switch",
    }
]


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    # ---- 场景 1：主动查询被网关 408，绝不能把 errcode 写进服务数据 ----
    ctrl, logs = build()
    ctrl._pending_queries[TIMER_URI] = time.monotonic()
    packet = resp(TIMER_URI, {"errcode": 408})
    ctrl._on_command_response("rid", TIMER_URI, 408, {"errcode": 408})
    ctrl._on_mqtt_message("/smartHome/signaltrans/v2/categories/command", packet)
    timer_data = dict(ctrl.store.get(DEV).services["timer"].data)
    check("408 不会污染 timer 数据", "errcode" not in timer_data)
    check("408 不会覆盖已有定时数组", timer_data.get("timer") == [])
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("408 提示成超时而不是成功", "超时/失败" in texts)
    check("408 不再出现「界面已刷新」", "已刷新" not in texts)
    check("408 只提示一次", texts.count("超时/失败") == 1)
    check("查询登记已消费", TIMER_URI not in ctrl._pending_queries)

    # ---- 场景 2：主动查询成功，数据回填 + 内容摘要 ----
    ctrl, logs = build()
    ctrl._pending_queries[TIMER_URI] = time.monotonic()
    packet = resp(TIMER_URI, {"num": 1, "timer": TASKS})
    ctrl._on_command_response("rid", TIMER_URI, 0, {"num": 1, "timer": TASKS})
    ctrl._on_mqtt_message("/smartHome/signaltrans/v2/categories/command", packet)
    timer_data = dict(ctrl.store.get(DEV).services["timer"].data)
    check("查询成功回填定时任务", timer_data.get("timer") == TASKS)
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("日志里带出任务时间", "17:00:00" in texts)
    check("成功时不再报「设备已接受命令」", "设备已接受命令" not in texts)

    # ---- 场景 3：旁听到别人（网关）的查询，不能动本地状态 ----
    ctrl, logs = build()
    gw_uri = "/devices/gw-1/services/wifiInfo"
    ctrl._on_mqtt_message(
        "/smartHome/signaltrans/v2/categories/command",
        resp(gw_uri, {"ssid": "home", "rssi": -40}),
    )
    ac = ctrl.store.get(DEV)
    check("旁听结果不会凭空造出服务", "wifiInfo" not in ac.services)
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("旁听结果标成旁听", "旁听" in texts)

    # ---- 场景 4：普通命令响应仍按原逻辑提示 ----
    ctrl, logs = build()
    ctrl._on_command_response("rid", TIMER_URI, 0, {"num": 1})
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("普通命令成功仍提示接受", "设备接受" in texts)

    # ---- 场景 5：普通命令被拒，带错误码解释 ----
    ctrl, logs = build()
    ctrl._on_command_response("rid", TIMER_URI, -12, {"devicedelay": 0, "devicectltype": 3})
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("-12 带中文解释", "设备拒绝执行" in texts)
    check("-12 仍带原始字段", "devicedelay" in texts)

    # ---- 场景 6：body 里 errcode=0 也不能写进服务数据 ----
    ctrl, logs = build()
    ctrl._pending_queries[DELAY_URI] = time.monotonic()
    ctrl._on_mqtt_message(
        "/smartHome/signaltrans/v2/categories/command",
        resp(DELAY_URI, {"errcode": 0}),
    )
    delay_data = dict(ctrl.store.get(DEV).services["delay"].data)
    check("errcode=0 不会被当成字段写进服务", "errcode" not in delay_data)

    # ---- 场景 7：查询登记过期后，普通命令响应不会被吞掉 ----
    ctrl, logs = build()
    ctrl._pending_queries[TIMER_URI] = time.monotonic() - 999
    ctrl._on_command_response("rid", TIMER_URI, 0, {})
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("过期登记不影响普通命令提示", "设备接受" in texts)
    check("过期登记被清掉", TIMER_URI not in ctrl._pending_queries)

    # ---- 场景 8：GET 回包是列表 / None（畸形）不崩 ----
    ctrl, logs = build()
    for weird in ([], None, "oops"):
        ctrl._on_mqtt_message(
            "/smartHome/signaltrans/v2/categories/command", resp(TIMER_URI, weird)
        )
    check("畸形 GET 回包不崩", True)

    # ---- 场景 9：MQTT 没连时不发命令、不留登记 ----
    ctrl, logs = build()
    ctrl.query_service(DEV, "timer")
    drain(app)
    texts = " ".join(m for _k, m in logs)
    check("未连接时提示先连接", "未连接" in texts)
    check("未连接时不产生查询登记", not ctrl._pending_queries)

    # ---- 场景 10：别人的定时轮询不能刷屏 ----
    # 官方 App / 云端每 10 秒查一次 gateway，成功失败各一条。原来两条都按
    # warning/tag 逐条记录，几分钟就把真实操作记录顶出屏幕。
    ctrl, logs = build()
    GW_URI = "/devices/gateway/services/gateway"
    TOPIC = "/smartHome/signaltrans/v2/categories/command"
    for _ in range(6):
        ctrl._on_mqtt_message(TOPIC, resp(GW_URI, {"errcode": 408}))
    drain(app)
    heard = [(k, m) for k, m in logs if "旁听" in m]
    check("旁听失败 6 次只记 1 条", len(heard) == 1)
    check("旁听失败降到 detail 级（默认折叠）", bool(heard) and heard[0][0] == "detail")
    check(
        "旁听失败不写成「查询超时/失败」",
        bool(heard) and "超时/失败" not in heard[0][1],
    )
    check("旁听失败标明是旁听", bool(heard) and "旁听" in heard[0][1])

    # 模拟窗口已过：下一条要补记，并说明期间合并了多少
    ctrl._log_throttle[f"旁听失败|{GW_URI}"] = (0.0, 5)
    ctrl._on_mqtt_message(TOPIC, resp(GW_URI, {"errcode": 408}))
    drain(app)
    heard = [(k, m) for k, m in logs if "旁听" in m]
    check("窗口过后补记一条", len(heard) == 2)
    check("补记时说明合并了 5 次", bool(heard) and "已合并" in heard[-1][1])

    # 自己的查询失败不能被节流吞掉：每次都要有反馈
    ctrl._pending_queries[GW_URI] = time.monotonic()
    ctrl._on_mqtt_message(TOPIC, resp(GW_URI, {"errcode": 408}))
    ctrl._pending_queries[GW_URI] = time.monotonic()
    ctrl._on_mqtt_message(TOPIC, resp(GW_URI, {"errcode": 408}))
    drain(app)
    own = [(k, m) for k, m in logs if "查询超时/失败" in m]
    check("自己的查询失败每次都提示（2 次都在）", len(own) == 2)
    check("自己的查询失败是 warning 级", bool(own) and own[0][0] == "warning")

    print()
    print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
    if FAILS:
        for item in FAILS:
            print("  FAIL:", item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
