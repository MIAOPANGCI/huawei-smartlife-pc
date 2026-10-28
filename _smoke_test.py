"""无头自检：不联网、不弹窗，验证 UI 构建与关键交互链路。

运行：
    set QT_QPA_PLATFORM=offscreen
    python _smoke_test.py

覆盖：登录页 → 看板切换、卡片网格与流式布局不越界、设备详情弹窗、
属性控件生成、命令下发参数、遥测合并、主题与主题色切换、场景渲染、
搜索过滤、状态面板联动。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import device_spec, dispatch  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.theme import set_theme, theme  # noqa: E402
from huawei_smartlife.models import (  # noqa: E402
    Device,
    DiscoverySnapshot,
    Home,
    Scene,
    ServiceState,
)

PASSED: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        print(f"FAILED: {name}")
        raise SystemExit(1)
    PASSED.append(name)
    print(f"OK: {name}")


def build_fake_snapshot() -> DiscoverySnapshot:
    """构造贴近真实的设备集合：开关、多开关、只读统计、无开关设备。"""
    homes = (
        Home(home_id="h1", name="我的家"),
        Home(home_id="h2", name="父母家"),
    )
    devices = [
        Device(
            home_id="h1",
            dev_id="dev-light",
            name="客厅灯",
            room_name="客厅",
            online=True,
            model="HL-100",
            services={
                "switch": ServiceState(sid="switch", data={"on": 1}),
                "brightness": ServiceState(
                    sid="brightness", data={"brightness": 60, "min": 1, "max": 100}
                ),
            },
        ),
        Device(
            home_id="h1",
            dev_id="dev-heater",
            name="热水器",
            room_name="卫生间",
            online=True,
            services={
                "indicator": ServiceState(sid="indicator", data={"on": 0}),
                "switch": ServiceState(sid="switch", data={"on": 0}),
                "temperature": ServiceState(sid="temperature", data={"temperature": 48}),
            },
        ),
        Device(
            home_id="h1",
            dev_id="dev-socket",
            name="室外充电插座",
            room_name="室外",
            online=False,
            services={
                "switch": ServiceState(sid="switch", data={"on": 1}),
                "powerElectricity": ServiceState(
                    sid="powerElectricity",
                    data={"TotalElectricity": 403977, "power": 20},
                ),
            },
        ),
        Device(
            home_id="h2",
            dev_id="dev-ac",
            name="壁挂式空调",
            room_name="主卧",
            online=True,
            services={
                "airconditioner": ServiceState(
                    sid="airconditioner",
                    data={"on": 0, "targetTemp": 26, "min": 16, "max": 30},
                )
            },
        ),
        Device(
            home_id="h2",
            dev_id="dev-watch",
            name="HUAWEI WATCH FIT 3",
            room_name="",
            online=None,
            services={"battery": ServiceState(sid="battery", data={"battery": 82})},
        ),
    ]
    return DiscoverySnapshot(homes=homes, devices=tuple(devices))


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    set_theme("dark", "华为红")
    dispatch.attach()

    # ---- 0. 控制器（屏蔽所有网络调用）----
    controller = AppController()
    controller.restore_session = lambda: False
    controller.refresh = lambda: None
    controller.connect_mqtt = lambda: None
    controller.load_scenes = lambda: None

    # ---- 1. 数据层 ----
    store = controller.store
    snapshot = build_fake_snapshot()
    store.replace_snapshot(snapshot)
    check("快照载入设备数", store.total == 5)
    check("家庭统计", len(store.homes_with_counts()) == 2)

    # 主开关识别：不能选中 indicator
    heater = store.get("dev-heater")
    found = device_spec.find_switch_service(heater)
    check("多开关设备选主电源", found is not None and found[0] == "switch")

    # 统计服务不当作开关
    socket = store.get("dev-socket")
    check("统计服务功率不当开关", device_spec.find_switch_service(socket)[0] == "switch")

    # 只读服务属性全部只读
    specs = device_spec.build_service_specs(socket)
    electricity = next(s for s in specs if s.sid == "powerElectricity")
    check("电量统计服务整体只读", electricity.readonly is True)

    # 遥测合并
    changed = store.apply_telemetry(
        {"body": {"devId": "dev-ac", "services": [{"sid": "airconditioner", "data": {"on": 1}}]}}
    )
    check("遥测合并生效", changed is not None and changed[1] is True)
    check("遥测值写入设备", store.get("dev-ac").services["airconditioner"].data["on"] == 1)

    # ---- 2. 窗口 ----
    window = MainWindow(controller)
    window.resize(1280, 860)
    window.show()
    app.processEvents()
    check("启动停在登录页", window._stack.currentWidget() is window.login_page)

    # ---- 3. 登录成功 → 看板 ----
    window._on_login_succeeded("138****0000")
    window.dashboard.rebuild_cards()
    window.dashboard.reload_homes()
    app.processEvents()
    check("切换到看板", window._stack.currentWidget() is window.dashboard)
    check("卡片数量与设备一致", len(window.dashboard._cards) == 5)

    # 流式布局不越界
    from app.pages.dashboard_page import FlowContainer
    from app.widgets.device_card import DeviceCard

    overflow = []
    for container in window.dashboard.findChildren(FlowContainer):
        for card in container.findChildren(DeviceCard):
            if card.geometry().right() > container.width() + 1:
                overflow.append(card.geometry())
    check("卡片不超出容器宽度", not overflow)

    # ---- 4. 顶部区域（Tab / 胶囊 / 统计）----
    check("Tab 含场景与全部", {"scenes", "all"} <= set(window.dashboard._tab_buttons))
    check("Tab 含家庭", "home:h1" in window.dashboard._tab_buttons)
    check("标题旁统计正确", "5 台设备" in window.dashboard._summary.text())
    check("统计胶囊已生成", window.dashboard._chip_box.count() >= 2)

    window.dashboard.select_device("dev-heater")
    check(
        "选中态标记到卡片",
        window.dashboard._cards["dev-heater"].property("selected") == "true",
    )

    window._open_device("dev-heater")
    dialog = window._device_dialog
    check("详情弹窗创建", dialog is not None and dialog.isVisible())
    check("详情弹窗指向正确设备", dialog.dev_id == "dev-heater")
    # 主开关存在
    check("详情弹窗识别主开关", dialog._power_box.isVisible() is not False)

    # ---- 5. 命令下发 ----
    captured: list[tuple] = []
    dialog.command_requested.disconnect()
    dialog.command_requested.connect(lambda *args: captured.append(args))
    dialog._on_power_toggled(True)
    check(
        "开关命令参数正确",
        bool(captured) and captured[-1][1] == "switch" and captured[-1][2] == {"on": 1},
    )

    # 通用命令面板 JSON 校验
    dialog._on_advanced_toggled(True)
    dialog._data_edit.setText("{not json}")
    dialog._on_send_command()
    check("非法 JSON 被拦截", "JSON" in dialog._hint.text())
    dialog._data_edit.setText('{"on": 0}')
    dialog._on_send_command()
    check("合法 JSON 正常下发", captured[-1][2] == {"on": 0})

    # ---- 6. 主题切换 ----
    window._toggle_theme()
    check("切换到浅色", theme().mode == "light")
    check("QSS 已应用", "QMainWindow" in app.styleSheet())
    window._set_accent("蔚  蓝")
    check("主题色切换", theme().accent_name == "蔚  蓝")
    window._toggle_theme()
    check("切回深色", theme().mode == "dark")
    window._set_accent("华为红")

    # ---- 7. 场景 ----
    scenes = [Scene(scene_id="s1", name="回家模式", home_id="h1")]
    window.dashboard._apply_view(window.dashboard.VIEW_SCENES)
    window.dashboard.show_scenes(scenes)
    app.processEvents()
    check("场景卡片渲染", window.dashboard._scene_container.flow.count() == 1)

    # ---- 8. 搜索过滤 ----
    window.dashboard._on_search_changed("灯")
    window.dashboard.rebuild_cards()
    check("搜索过滤生效", len(window.dashboard._cards) == 1)
    window.dashboard._on_search_changed("")
    window.dashboard.rebuild_cards()
    check("清空搜索恢复", len(window.dashboard._cards) == 5)

    # ---- 9. 家庭筛选 ----
    window.dashboard._apply_filter("home:h1")
    check("家庭筛选生效", len(window.dashboard._cards) == 3)
    check("筛选后 Tab 同步", window.dashboard._tab_buttons["home:h1"].isChecked())
    window.dashboard._apply_filter(None)
    check("回到全部设备", len(window.dashboard._cards) == 5)
    window.dashboard._apply_filter("other")
    check("其他设备分组可用", len(window.dashboard._cards) == 0)

    # ---- 10. 弹窗去重 ----
    window._open_device("dev-light")
    check("切换设备不残留旧弹窗", window._device_dialog.dev_id == "dev-light")
    first = window._device_dialog
    window._open_device("dev-light")
    check("同设备不重复创建", window._device_dialog is first)

    first.close()
    app.processEvents()
    check("弹窗关闭后引用清空", window._device_dialog is None)

    # ---- 11. 窗口圆角 ----
    check("窗口启用半透明（圆角前提）", window.testAttribute(Qt.WA_TranslucentBackground))
    shot = window.grab().toImage()
    check("左上角为透明圆角", shot.pixelColor(1, 1).alpha() < 20)
    check("中心区域不透明", shot.pixelColor(shot.width() // 2, shot.height() // 2).alpha() == 255)

    window._sync_window_corners(True)
    check("最大化时取消圆角", window.centralWidget().property("winMaximized") == "true")
    check("标题栏同步取消圆角", window.title_bar.property("winMaximized") == "true")
    window._sync_window_corners(False)
    check("还原后恢复圆角", window.centralWidget().property("winMaximized") == "false")

    # ---- 12. 启动时默认分类 ----
    window._save_settings = lambda: None          # 不污染真实配置文件
    window._set_startup_view("scenes")
    check("记住启动分类", window._settings.get("startup_view") == "scenes")
    window.dashboard.show_view("logs")
    check("切换到日志视图", window.dashboard._stack_widget.currentIndex() == window.dashboard.VIEW_LOGS)
    window.dashboard.show_view("home:h1")
    check("可定位到指定家庭", window.dashboard._filter == "home:h1")
    window.dashboard.show_view("home:不存在的家庭")
    check("家庭失效回退全部", window.dashboard._filter is None)
    window.dashboard.show_view("all")
    check("回到全部设备", window.dashboard._filter is None)

    # ---- 13. 托盘面板与桌面挂件 ----
    panel = window.tray_panel
    panel._layout_mode = "grid"          # 真实配置里可能存的是单列，先归零
    panel.set_devices(controller.store.all())
    check("托盘面板渲染卡片", len(panel._cards) == 5)
    panel._toggle_layout()
    check("面板可切单列", panel._layout_mode == "list")
    check("单列高度随行数变化", panel.height() == 340)   # 5 个设备显示 4 行
    panel._toggle_layout()
    check("面板可切回双列", panel._layout_mode == "grid")
    check("双列高度随行数变化", panel.height() == 268)   # 3 行
    # 窗口高度必须刚好等于内容需要的高度，少一像素底部圆角就会被挤出窗口
    check(
        "窗口高度等于内容高度（底部圆角完整）",
        panel.height() == panel._surface.layout().minimumSize().height(),
    )
    panel._layout_mode = "grid"

    board = window.board
    window._sync_board_devices()
    check("挂件只放有开关的设备", 0 < len(board._cards) < 5)
    sample_card = next(iter(board._cards.values()))
    check("紧凑卡片不再响应整卡点击", not hasattr(sample_card, "clicked"))
    check(
        "电源键仍保留手型指针",
        sample_card._power.cursor().shape() == Qt.PointingHandCursor,
    )
    check("挂件标题带数量", "桌面挂件 · " in board._title.text())
    board.toggle_collapse()
    check("挂件可收起", board._collapsed is True)
    check("收起后隐藏卡片行", board._cards_row.isVisible() is False)
    board.toggle_collapse()
    check("挂件可展开", board._collapsed is False)
    board._locked = False           # 真实配置里可能已锁定，先归零再验证切换
    board._apply_state()
    board.toggle_lock()
    check("挂件可锁定", board._locked is True)
    board.toggle_lock()
    check("挂件可解锁", board._locked is False)

    window._save_settings = lambda: None
    core = controller.store.get("dev-heater")
    board.refresh_device(core)
    check("挂件单卡跟随刷新", board._cards["dev-heater"]._meta.text().startswith("卫生间"))

    # ---- 14. 独立窗口（主窗口最小化不该带走面板 / 挂件）----
    check("托盘面板不挂在主窗口下", window.tray_panel.parent() is None)
    check("桌面挂件不挂在主窗口下", window.board.parent() is None)

    # ---- 15. 自选设备与排序 ----
    switch_ids = [d.dev_id for d in window._default_order(True)]
    window._settings["board_devices"] = [switch_ids[2], switch_ids[0]]
    picked = window._ordered_devices("board_devices", require_switch=True)
    check(
        "自选顺序生效",
        [d.dev_id for d in picked] == [switch_ids[2], switch_ids[0]],
    )

    window._settings["board_devices"] = ["已删除的设备"]
    check("记录失效时回退默认", len(window._ordered_devices("board_devices")) > 0)

    window._settings.pop("board_devices", None)
    check(
        "未自选时默认在线优先",
        window._ordered_devices("board_devices")[0].online is not False,
    )

    # ---- 16. 设备选择对话框 ----
    from app.dialogs.device_picker import DevicePickerDialog  # noqa: E402

    devices = controller.store.all()
    dlg = DevicePickerDialog(
        "测试选择", devices, [devices[0].dev_id], max_count=3
    )
    check("选择器载入已选", dlg._selected.count() == 1)
    check("选择器载入可选", dlg._available.count() == len(devices) - 1)

    dlg._available.setCurrentRow(0)
    dlg._add_current()
    check("可加入设备", dlg._selected.count() == 2)
    dlg._selected.setCurrentRow(1)
    dlg._move(-1)
    check(
        "上移改变顺序",
        dlg.selected_ids() == [devices[1].dev_id, devices[0].dev_id],
    )
    dlg._move(1)
    check(
        "下移改变顺序",
        dlg.selected_ids() == [devices[0].dev_id, devices[1].dev_id],
    )
    dlg._selected.setCurrentRow(0)
    dlg._remove_current()
    check("可移出设备", dlg.selected_ids() == [devices[1].dev_id])
    dlg._reset_default()
    check("恢复默认清空已选", dlg._selected.count() == 0)

    limited = DevicePickerDialog(
        "上限测试", devices, [d.dev_id for d in devices[:2]], max_count=2
    )
    check("达到上限后禁用加入", limited._add_btn.isEnabled() is False)
    dlg.close()
    limited.close()
    dlg.deleteLater()
    limited.deleteLater()

    # ---- 17. 保存后对话框正常销毁（回归：点保存时 python 崩溃）----
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer  # noqa: E402
    from PySide6.QtWidgets import QApplication as _QApp  # noqa: E402

    def _auto_accept() -> None:
        for widget in _QApp.topLevelWidgets():
            if isinstance(widget, DevicePickerDialog) and widget.isVisible():
                widget.accept()

    window._settings.pop("board_devices", None)
    before = {
        id(widget)
        for widget in _QApp.topLevelWidgets()
        if isinstance(widget, DevicePickerDialog)
    }
    QTimer.singleShot(60, _auto_accept)
    window._edit_board_devices()
    # deleteLater 走的是 DeferredDelete 事件，processEvents 不会处理，必须显式派发
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    check(
        "保存后不残留对话框",
        not [
            widget
            for widget in _QApp.topLevelWidgets()
            if isinstance(widget, DevicePickerDialog) and id(widget) not in before
        ],
    )
    check("保存结果已写进设置", bool(window._settings.get("board_devices")))

    # ---- 18. 重建控件不产生顶层窗口（回归：登录后"一堆窗口闪现"）----
    # setParent(None) 会把子控件提升成顶层窗口，Windows 会为它们建任务栏按钮。
    window._sync_tray_devices()
    window._sync_board_devices()
    window.dashboard.rebuild_cards()
    window.dashboard.reload_homes()
    stray = [
        type(widget).__name__
        for widget in _QApp.topLevelWidgets()
        if "Card" in type(widget).__name__ or "Chip" in type(widget).__name__
    ]
    check("重建不产生顶层卡片窗口", not stray)

    # ---- 19. 二次验证渠道（回归：没选渠道就说"验证码已发送至设备"）----
    from huawei_smartlife.auth import _default_channel  # noqa: E402
    from huawei_smartlife.models import (  # noqa: E402
        CHALLENGE_KIND_DEVICE,
        CHALLENGE_KIND_SMS,
        ChallengeChannel,
        LoginChallenge,
        channel_prompt,
    )

    device_ch = ChallengeChannel(
        name="nova 6 (5G)", account_type="-1", kind=CHALLENGE_KIND_DEVICE, sent=True
    )
    sms_ch = ChallengeChannel(
        name="138****0000", account_type="2", kind=CHALLENGE_KIND_SMS, sent=False
    )
    channels = (device_ch, sms_ch)

    # 默认走设备渠道：华为在登录请求被拒时已经把码推到设备上了，
    # 用户抬头就能看到，不用点任何按钮
    check("默认用已发送的设备渠道", _default_channel(channels) is device_ch)
    check("仅有短信渠道时退回去", _default_channel((sms_ch,)) is sms_ch)
    check("文案不暴露内部 accountType", "类型" not in channel_prompt(device_ch))
    check("设备渠道说明是华为推送", "华为" in channel_prompt(device_ch))
    check("未发送的短信渠道说「将发送」", "将发送" in channel_prompt(sms_ch))

    page = window.login_page
    sent_codes: list = []
    page.channel_requested.connect(sent_codes.append)
    page.show_challenge(
        LoginChallenge(
            prompt=channel_prompt(device_ch),
            challenge_name=device_ch.name,
            challenge_type=device_ch.account_type,
            channels=channels,
            kind=CHALLENGE_KIND_DEVICE,
        )
    )
    check("默认停在设备渠道", page._current_channel().kind == CHALLENGE_KIND_DEVICE)
    check("渠道下拉已收起", not page._channel_combo.isVisible())
    check("提示写明华为已推送", "华为" in page._challenge_prompt.text())
    check("按钮是「重新获取」", page._send_button.text() == "重新获取")
    check("设备渠道下按钮可用", page._send_button.isEnabled())

    # 点「重新获取」要重新发起登录（华为会推新码），而不是本地切渠道。
    # 先断开主窗口的连接，否则这里会真的发一次登录请求。
    try:
        page.login_requested.disconnect()
    except RuntimeError:
        pass
    relogin: list = []
    page.login_requested.connect(lambda account, _pwd, _remember: relogin.append(account))
    page._account.setText("138****0000")
    page._password.setText("secret")
    page._on_send_code()
    check("重新获取会重新发起登录", relogin == ["138****0000"])

    page._account.clear()
    page._password.clear()
    page._on_send_code()
    check("缺账号密码时给出提示", "账号" in page._error.text())
    page._account.setText("138****0000")
    page._password.setText("secret")

    # 短信渠道下按钮语义回到「发送」
    page.show_challenge(
        LoginChallenge(
            prompt=channel_prompt(sms_ch),
            challenge_name=sms_ch.name,
            challenge_type=sms_ch.account_type,
            channels=channels,
            kind=CHALLENGE_KIND_SMS,
        )
    )
    check(
        "短信渠道按钮是发送",
        page._send_button.text() in ("发送验证码", "重新发送"),
    )

    page.show_error("上一条错误提示")
    page._channel_combo.setCurrentIndex(0)   # 切回设备
    page._on_channel_changed(0)
    check("切换渠道会清掉旧错误", not page._error.isVisible())
    page.reset()

    # ---- 20. 记住登录状态（回归：取消勾选后下次打开仍是登录态）----
    import tempfile  # noqa: E402

    import app.controller as _ctrl_mod  # noqa: E402

    tmp_session = os.path.join(tempfile.gettempdir(), "wbsmart_smoke_session.json")
    original_path = _ctrl_mod.SESSION_PATH
    # 换成临时路径：绝不能拿用户真实的 session.json 做实验
    _ctrl_mod.SESSION_PATH = tmp_session
    try:
        ctrl = window.controller
        original_save = ctrl.client.save_session
        ctrl.client.save_session = lambda path: open(  # noqa: SIM115
            path, "w", encoding="utf-8"
        ).write("{}")

        if os.path.exists(tmp_session):
            os.remove(tmp_session)
        ctrl._remember_session = True
        ctrl._persist_session_if_needed()
        check("勾选记住会写入会话文件", os.path.exists(tmp_session))

        ctrl._remember_session = False
        ctrl._persist_session_if_needed()
        check("取消记住会删掉旧会话文件", not os.path.exists(tmp_session))
        check("没有会话文件时不尝试免密恢复", ctrl.restore_session() is False)

        ctrl.login("", "", False)
        check("login 会把开关状态记下来", ctrl._remember_session is False)
        window.login_page.clear_error()

        ctrl.client.save_session = original_save
    finally:
        _ctrl_mod.SESSION_PATH = original_path
        if os.path.exists(tmp_session):
            os.remove(tmp_session)

    # ---- 21. 挑战失败的提示（回归：只显示 resultCode=70008804）----
    from huawei_smartlife.models import challenge_error_hint  # noqa: E402

    hint_sms = challenge_error_hint("70008804", channels, CHALLENGE_KIND_SMS)
    check("错误提示带华为返回码", "70008804" in hint_sms)
    check("短信渠道失败时提示切到设备渠道", "华为设备" in hint_sms)
    hint_device = challenge_error_hint("70008804", channels, CHALLENGE_KIND_DEVICE)
    check("设备渠道失败时不再让用户切换", "再提交一次" not in hint_device)
    check(
        "未知错误码也带出原始码",
        "12345678" in challenge_error_hint("12345678", channels, CHALLENGE_KIND_SMS),
    )

    # ---- 22. 切换接收方式必须同步到后端（回归：切到设备渠道仍报 70008804）----
    from huawei_smartlife import auth as _auth_mod  # noqa: E402

    auth_obj = _auth_mod.HuaweiAuth.__new__(_auth_mod.HuaweiAuth)  # 不初始化网络
    auth_obj._pending = _auth_mod._PendingLogin(
        account="138****0000",
        encrypted_password="x",
        challenge_name=sms_ch.name,
        challenge_type=sms_ch.account_type,
        channels=channels,
    )
    switched = auth_obj.switch_challenge_channel(device_ch)
    check("切换后挂起类型变成 -1（设备）", auth_obj._pending.challenge_type == "-1")
    check("切换后返回的 kind 是 device", switched.kind == CHALLENGE_KIND_DEVICE)
    check("切换不改变渠道列表", switched.channels == channels)

    # 界面下拉切换要真的发出信号，否则后端永远收不到
    page.show_challenge(
        LoginChallenge(
            prompt=channel_prompt(sms_ch),
            challenge_name=sms_ch.name,
            challenge_type=sms_ch.account_type,
            channels=channels,
            kind=CHALLENGE_KIND_SMS,
        )
    )
    notified: list = []
    page.channel_switched.connect(notified.append)
    page._channel_combo.setCurrentIndex(0)          # 切到设备项
    check("下拉切换会通知后端", bool(notified))
    check("通知里带的是设备渠道", notified[-1].kind == CHALLENGE_KIND_DEVICE)
    page.reset()

    # ---- 23. 遥测中文化（回归：日志里全是原始 JSON）----
    from app.telemetry_text import describe_query, describe_telemetry  # noqa: E402

    names = {"dev-heater": "热水器"}
    name_of = lambda dev_id: names.get(dev_id, "")  # noqa: E731

    telemetry = describe_telemetry(
        {
            "body": {
                "devId": "dev-heater",
                "services": [{"sid": "power", "data": {"current": 1717}}],
            }
        },
        name_of,
    )
    check("遥测摘要带设备名", "热水器" in telemetry)
    check("遥测摘要翻译属性名", "电流" in telemetry)
    check("遥测摘要带单位", "1717mA" in telemetry)
    check("遥测摘要不再是 JSON", "{" not in telemetry and "services" not in telemetry)

    # 服务名已经包含属性名时不再重复：「视频开关 开关 关」→「视频开关 关」
    video = describe_telemetry(
        {
            "body": {
                "devId": "dev-unknown",
                "services": [{"sid": "videoSwitch", "data": {"on": 0}}],
            }
        },
        name_of,
    )
    check("服务名与属性名不重复", "开关 开关" not in video)
    check("开关值翻译成开/关", video.endswith("关"))

    # 空字符串字段是"未设置"，不该占位置
    visit = describe_telemetry(
        {
            "body": {
                "devId": "dev-unknown",
                "services": [
                    {"sid": "visitPoint5", "data": {"move": 0, "enable": 0, "name": "", "id": ""}}
                ],
            }
        },
        name_of,
    )
    check("空字段被跳过", "—" not in visit)
    check("访客点翻译成中文", "访客点" in visit)

    # 查询响应：不再把 UUID 整条路径打到界面
    query = describe_query("/devices/6bcc4cab-33d6/services/gateway/ntwk/WlanBasic", 5)
    check("查询响应翻成中文路径", "网关" in query and "WiFi 信息" in query)
    check("查询响应带条数", "5 项" in query)
    check("查询响应不再出现设备 UUID", "6bcc4cab" not in query)

    unknown = describe_telemetry({"body": {"devId": "abcdefgh-1234", "services": []}}, name_of)
    check("未知设备退化成短 ID", "abcdefgh" in unknown)
    check("无法解析的报文不抛异常", "无法解析" in describe_telemetry({"bad": 1}, name_of))

    # ---- 24. 运行日志按级别折叠（遥测默认不显示）----
    board = window.dashboard
    board.clear_logs()
    board.set_log_verbose(False)
    board.append_log("已连接实时通道", "info")
    board.append_log("[遥测] 热水器 · 电源 电流 1717mA", "detail")
    visible = board._log_view.toPlainText()
    check("普通日志照常显示", "已连接实时通道" in visible)
    check("遥测默认折叠", "1717mA" not in visible)

    board.set_log_verbose(True)
    check("勾选后显示遥测", "1717mA" in board._log_view.toPlainText())

    board.set_log_verbose(False)
    check("取消勾选后遥测再次隐藏", "1717mA" not in board._log_view.toPlainText())
    check("切换筛选不影响普通日志", "已连接实时通道" in board._log_view.toPlainText())

    board.clear_logs()
    check("清空日志同时清缓存", not board._log_entries)
    check("清空后视图为空", not board._log_view.toPlainText())

    # ---- 25. 协议调试订阅（默认关闭，可切换）----
    ctrl = window.controller
    check("协议调试默认关闭", ctrl._capture_enabled is False)
    ctrl.set_capture_enabled(True, reconnect=False)
    check("可开启协议调试", ctrl._capture_enabled is True)
    ctrl.set_capture_enabled(False, reconnect=False)
    check("可关闭协议调试", ctrl._capture_enabled is False)
    # 未连接实时通道时切换不应触发重连（否则会打网络请求）
    check("未连接时不触发重连", ctrl._mqtt_connected is False)

    print()
    print(f"SMOKE TEST PASSED ({len(PASSED)} 项)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
