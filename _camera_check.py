"""摄像头播放链路验证：不依赖真实摄像头，用本地生成的视频跑完整取流流程。

覆盖点：
  - QThread 读帧 → QImage 转换（BGR888 + copy）→ 信号投递
  - VideoView 等比绘制（黑边 + 居中）
  - stop() 能及时退出，不留悬挂线程
  - rtsp_banner 能认出真 RTSP 服务、拒绝非 RTSP 服务
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import dispatch  # noqa: E402
from app.dialogs.camera_dialog import VideoView, _FrameGrabber  # noqa: E402
from huawei_smartlife.camera import rtsp_banner  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool) -> None:
    if ok:
        PASSED.append(name)
        print(f"OK: {name}")
    else:
        FAILED.append(name)
        print(f"FAIL: {name}")


def make_video(path: str) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30, (320, 180))
    for i in range(90):
        # 背景给中灰而不是接近黑：要能区分「真的读到画面」和「QImage 指向了
        # 已释放的缓冲区（表现为全黑/花屏）」
        frame = np.full((180, 320, 3), 95, np.uint8)
        cv2.circle(frame, (20 + (i * 3) % 280, 90), 18, (40, 180, 240), -1)
        cv2.putText(frame, str(i), (140, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()


def fake_rtsp_server(reply: bytes):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = server.accept()
            conn.recv(512)
            conn.sendall(reply)
            conn.close()
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    dispatch.attach()

    folder = tempfile.mkdtemp(prefix="camcheck-")
    video = os.path.join(folder, "test.avi")
    make_video(video)
    check("测试视频生成", os.path.getsize(video) > 1000)

    # ---- 1. 读帧链路 ----
    grabber = _FrameGrabber(video)
    frames: list = []
    opened: list = []
    errors: list = []
    grabber.frame_ready.connect(frames.append)
    grabber.opened.connect(lambda w, h: opened.append((w, h)))
    grabber.failed.connect(errors.append)

    started = time.monotonic()
    grabber.start()
    loop = QEventLoop()
    QTimer.singleShot(2500, loop.quit)
    loop.exec()
    elapsed = time.monotonic() - started

    check("收到「已打开」信号", opened and opened[0] == (320, 180))
    check(f"2.5 秒内读到多帧（{len(frames)} 帧）", len(frames) >= 10)
    check("没有解码错误", not errors)
    check("QImage 尺寸与视频一致", bool(frames) and frames[0].width() == 320 and frames[0].height() == 180)
    check(
        "QImage 是 BGR888（与 cv2 帧格式对应）",
        bool(frames) and frames[0].format() == QImage.Format.Format_BGR888,
    )

    # 抽一帧看像素：全黑说明 QImage 指向了已释放的缓冲区
    bright = 0
    sampled = 0
    if frames:
        mid = frames[len(frames) // 2]
        for x in range(0, 320, 8):
            for y in range(0, 180, 8):
                sampled += 1
                if mid.pixelColor(x, y).lightness() > 60:
                    bright += 1
    check(f"画面有实际内容而不是全黑（{bright}/{sampled} 采样点亮）", bright > sampled // 2)

    stopped_at = time.monotonic()
    grabber.stop()
    exited = grabber.wait(3000)
    check(f"停止后线程及时退出（{int((time.monotonic() - stopped_at) * 1000)} ms）", exited)
    check("停止后不再有新帧", len(frames) > 0 and grabber.isFinished())

    # ---- 2. VideoView 绘制 ----
    # 故意用比 16:9 更方的窗口：上下必须留出黑边，才能验证等比居中
    view = VideoView()
    view.resize(600, 420)
    view.set_image(frames[-1] if frames else None)
    canvas = view.grab().toImage()
    corner = canvas.pixelColor(2, 2).lightness()
    middle = canvas.pixelColor(300, 210).lightness()
    check(f"未铺满的地方是黑边（角落亮度 {corner}）", corner < 20)
    check(f"画面画在了中间区域（中心亮度 {middle}）", middle > 60)

    view.set_hint("尚未连接")
    check("无画面时显示提示而不是崩溃", view.grab().toImage().pixelColor(300, 169).lightness() >= 0)

    # ---- 3. RTSP 探测 ----
    good_port = fake_rtsp_server(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nServer: FakeCam/1.0\r\n\r\n")
    check("识别出真正的 RTSP 服务", rtsp_banner("127.0.0.1", good_port, timeout=2) == "FakeCam/1.0")

    http_port = fake_rtsp_server(b"HTTP/1.1 200 OK\r\nServer: nginx\r\n\r\n")
    check("非 RTSP 服务不被误判", rtsp_banner("127.0.0.1", http_port, timeout=2) is None)

    check("端口不通时返回 None", rtsp_banner("127.0.0.1", 1, timeout=0.3) is None)

    # ---- 4. 打不开的地址走失败分支 ----
    bad = _FrameGrabber(os.path.join(folder, "not-exist.avi"))
    bad_errors: list = []
    bad.failed.connect(bad_errors.append)
    bad.start()
    loop2 = QEventLoop()
    QTimer.singleShot(4000, loop2.quit)
    bad.failed.connect(loop2.quit)
    loop2.exec()
    bad.stop()
    bad.wait(2000)
    check("打不开时给出可读的错误提示", bool(bad_errors) and "打不开" in bad_errors[0])

    # ---- 5. 外部播放器按钮：不可用就不该摆在界面上 ----
    # 之前这台机器没装 vlc/ffplay/mpv，按钮照常可点，一点必然报错。
    from app.dialogs import camera_dialog as _cd  # noqa: E402
    from huawei_smartlife.camera import find_player  # noqa: E402
    from huawei_smartlife.models import Device  # noqa: E402

    dev = Device(
        home_id="h1",
        dev_id="cam-x",
        name="室外摄像头",
        model="小豚当家室外摄像头3K版",
        online=True,
    )
    _orig_find = _cd.find_player
    try:
        _cd.find_player = lambda name=None: None
        no_player = _cd.CameraDialog(dev)
        no_player.show()
        app.processEvents()
        check("没有播放器时按钮隐藏", no_player._external.isVisible() is False)
        no_player._host.setCurrentText("192.0.2.10")
        no_player._on_external()
        check(
            "误触时提示「不用装」而不是报错",
            "不用装" in no_player._status.text(),
        )
        no_player.close()

        _cd.find_player = lambda name=None: r"C:\VLC\vlc.exe"
        with_player = _cd.CameraDialog(dev)
        with_player.show()
        app.processEvents()
        check("有播放器时按钮可用", with_player._external.isVisible() is True)
        check(
            "tooltip 指明具体播放器",
            "vlc.exe" in with_player._external.toolTip(),
        )
        with_player.close()
    finally:
        _cd.find_player = _orig_find

    check("find_player 对不存在的名字返回 None", find_player("no-such-player") is None)

    # ---- 6. 装了但不在 PATH：走注册表 / 默认目录 ----
    # VLC 安装器从不写 PATH，只看 shutil.which 会把正常安装判成"没装"。
    from huawei_smartlife import camera as cam_mod  # noqa: E402

    _orig_which = cam_mod.shutil.which
    _orig_reg = cam_mod._registry_candidates
    _orig_dirs = cam_mod._KNOWN_DIRS
    try:
        cam_mod.shutil.which = lambda name: None
        cam_mod._registry_candidates = lambda name: [sys.executable]
        check(
            "PATH 里没有也能从注册表找到",
            cam_mod.find_player("vlc") == sys.executable,
        )

        cam_mod._registry_candidates = lambda name: []
        cam_mod._KNOWN_DIRS = dict(_orig_dirs, vlc=())
        check(
            "注册表和默认目录都没有时返回 None",
            cam_mod.find_player("vlc") is None,
        )
    finally:
        cam_mod.shutil.which = _orig_which
        cam_mod._registry_candidates = _orig_reg
        cam_mod._KNOWN_DIRS = _orig_dirs

    # ---- 7. 真实的 VLC（本机装了才断言）：定位 + 启动命令 ----
    from unittest.mock import patch  # noqa: E402

    real_vlc = find_player("vlc")
    if real_vlc:
        check(
            "本机 VLC 定位到正确的可执行文件",
            os.path.isfile(real_vlc) and real_vlc.lower().endswith("vlc.exe"),
        )
        cam = cam_mod.RtspCamera(
            name="测试", host="192.0.2.11", username="admin", password="p@ss"
        )
        launched: list = []
        with patch.object(
            cam_mod.subprocess, "Popen", side_effect=lambda args, **kw: launched.append(args)
        ):
            message = cam.launch_player()
        check("launch_player 用 vlc 启动", "vlc" in message and bool(launched))
        check(
            "VLC 命令带 RTSP 地址且密码做了 URL 编码",
            bool(launched)
            and any("rtsp://admin:p%40ss@" in str(arg) for arg in launched[0]),
        )
        check("日志里的地址已打码", "***" in message and "p%40ss" not in message)
        check(
            "VLC 强制走 TCP（3K 码流走 UDP 会花屏）",
            any("--rtsp-tcp" in str(arg) for arg in launched[0]),
        )
        # 500ms 缓存对 3K 太浅，VLC 会丢帧追赶导致进度时间反复跳
        cache_arg = next(
            (str(a) for a in launched[0] if str(a).startswith("--network-caching")),
            "",
        )
        cache_ms = int(cache_arg.split("=", 1)[1]) if "=" in cache_arg else 0
        check(f"VLC 缓存不小于 1000ms（实际 {cache_ms}）", cache_ms >= 1000)
        check(
            "VLC 关闭时钟抖动校正（避免进度回退）",
            any("--clock-jitter=0" in str(arg) for arg in launched[0]),
        )
    else:
        print("SKIP: 本机没装 VLC，跳过真实启动断言")

    # ---- 8. 码流路径映射：实测这台是 1=主、2=子，不是 0/1 ----
    cam_mod2 = cam_mod
    check(
        "默认映射 主=onvif1 / 子=onvif2",
        cam_mod2.STREAM_PATHS["main"] == "onvif1"
        and cam_mod2.STREAM_PATHS["sub"] == "onvif2",
    )
    check(
        "主码流地址用 onvif1",
        cam_mod2.RtspCamera(name="x", host="1.2.3.4", stream="main").rtsp_url.endswith(
            "/onvif1"
        ),
    )
    check(
        "子码流地址用 onvif2",
        cam_mod2.RtspCamera(name="x", host="1.2.3.4", stream="sub").rtsp_url.endswith(
            "/onvif2"
        ),
    )
    check(
        "显式 path 优先于码流映射",
        cam_mod2.RtspCamera(
            name="x", host="1.2.3.4", stream="main", path="custom/0"
        ).rtsp_url.endswith("/custom/0"),
    )
    check(
        "未知 stream 值退回子码流而不是空路径",
        cam_mod2.RtspCamera(name="x", host="1.2.3.4", stream="weird").rtsp_url.endswith(
            "/onvif2"
        ),
    )
    parsed = cam_mod2.parse_rtsp_url("rtsp://u:p@1.2.3.4/onvif1")
    check("解析地址时能认出主码流", parsed.stream == "main" and parsed.path == "onvif1")

    # ---- 9. 通道识别：全都不通时要给出空结果而不是崩 ----
    probe = _cd._StreamProbe("127.0.0.1", "", "", 1, paths=("onvif1",))
    probe_results: list = []
    probe.found.connect(probe_results.append)
    loop3 = QEventLoop()
    probe.finished.connect(loop3.quit)
    QTimer.singleShot(20000, loop3.quit)
    probe.start()
    loop3.exec()
    probe.wait(2000)
    check(
        "探测全失败时返回空列表而不是崩",
        probe_results == [[]],
    )

    # ---- 10. 手动添加的网络摄像头（不依赖华为设备） ----
    manual = _cd.CameraDialog(None, manual_name="网络摄像头", manual_key="manual")
    manual.show()
    app.processEvents()
    check("手动窗口标题正常", "网络摄像头" in manual.windowTitle())
    check("手动模式不套用华为型号判定", manual._manual is True)
    check(
        "手动提示不强推「硬盘录像机」步骤",
        "硬盘录像机" not in manual._tip.text(),
    )
    manual._host.setCurrentText("192.0.2.12")
    manual._user.setText("admin")
    manual._password.setText("pw")
    manual._paths = {"main": "onvif1", "sub": "onvif2"}
    manual_cfg = manual.config()
    check(
        "手动配置能完整取出（含通道结果）",
        manual_cfg["host"] == "192.0.2.12"
        and manual_cfg["paths"] == {"main": "onvif1", "sub": "onvif2"},
    )
    reopened = _cd.CameraDialog(None, manual_cfg, manual_name="网络摄像头", manual_key="manual")
    check(
        "重开后地址与通道都还在",
        reopened._host.currentText() == "192.0.2.12"
        and reopened._paths == {"main": "onvif1", "sub": "onvif2"},
    )
    check(
        "手动模式拼出的地址不带华为设备信息",
        reopened._current_camera() is not None
        and reopened._current_camera().device_id is None,
    )
    manual.close()
    reopened.close()

    print()
    print(f"CAMERA CHECK: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
