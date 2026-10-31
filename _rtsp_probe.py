"""RTSP 通道探测（命令行版，与界面「识别码流」同一套逻辑）。

为什么不用手写 RTSP DESCRIBE：华为/海思这类设备的鉴权是
``Digest realm="HIipCamera"``，自己拼认证头很容易被判 400，探测结果
不可信。直接用 cv2（底层 FFmpeg）打开读一帧，跟实际播放走完全相同的
代码路径，结论才和看到画面一致。

用法：
    py -3 _rtsp_probe.py <ip> <用户名> <密码> [路径 ...]

例：
    py -3 _rtsp_probe.py 192.168.1.100 cam-user cam-pass
    py -3 _rtsp_probe.py 192.168.1.100 cam-user cam-pass onvif1 onvif2
"""

from __future__ import annotations

import os
import sys
import threading

# 必须在 cv2 打开流之前设置：默认 UDP 在家庭 Wi-Fi 上极易花屏/黑屏
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000"
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from huawei_smartlife.camera import CANDIDATE_PATHS, STREAM_PATHS, RtspCamera


def probe_one(camera: RtspCamera, timeout: float = 20.0) -> tuple[int, int] | None:
    """打开一条流读一帧，返回 (宽, 高)；失败返回 None。

    放在子线程里跑并限时：cv2 的 open 卡在网络等待时不会自己回来。
    """
    import cv2

    result: dict[str, tuple[int, int] | None] = {}

    def run() -> None:
        capture = None
        try:
            capture = cv2.VideoCapture(camera.rtsp_url, cv2.CAP_FFMPEG)
            if capture.isOpened():
                ok, frame = capture.read()
                if ok and frame is not None:
                    height, width = frame.shape[:2]
                    result["size"] = (int(width), int(height))
                    return
        except Exception:  # noqa: BLE001 - 探测工具，异常等同于"这条不通"
            pass
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:  # noqa: BLE001
                    pass
        result["size"] = None

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return result.get("size")


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2

    host, user, password = argv[1], argv[2], argv[3]
    paths = argv[4:] or list(CANDIDATE_PATHS)

    print(f"目标 {host}，试 {len(paths)} 条通道（TCP，单条最多 20 秒）\n")
    hits: list[tuple[str, int, int]] = []

    for path in paths:
        camera = RtspCamera(
            name="probe", host=host, username=user, password=password, path=path
        )
        size = probe_one(camera)
        if size:
            hits.append((path, size[0], size[1]))
            print(f"  OK  {path:12s} {size[0]}×{size[1]}")
        else:
            print(f"  --  {path:12s} 打不开或无画面")

    print()
    if not hits:
        print("没有任何通道能出画面。检查：")
        print("  1. 账号密码是 App 里「硬盘录像机」那套，不是华为账号；")
        print("  2. 该开关已打开；摄像头与电脑在同一路由器、同一网段；")
        print("  3. 摄像头没被其他客户端占满连接数（先关掉手机 App 的实时画面）。")
        return 1

    hits.sort(key=lambda item: item[1] * item[2], reverse=True)
    main_path, main_w, main_h = hits[0]
    print(f"主码流：{main_path}  {main_w}×{main_h}")
    if len(hits) > 1:
        sub_path, sub_w, sub_h = hits[-1]
        print(f"子码流：{sub_path}  {sub_w}×{sub_h}")
    else:
        print("子码流：这台只暴露出一个通道，主=子")

    expected = f"main={STREAM_PATHS['main']} sub={STREAM_PATHS['sub']}"
    if main_path != STREAM_PATHS["main"]:
        print(f"\n注意：默认映射是 {expected}，但这台的实际情况不同。")
        print("在界面点「识别码流」后会记住这台设备的结果，不用改代码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
