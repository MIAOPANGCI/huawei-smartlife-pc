"""验证发布包能真正启动：跑 8 秒看进程是否还活着，然后关掉。

不隔离配置目录——要验的就是"用户双击之后能不能起来"，用真实环境才有意义。
配了 --tray，如果本机有可恢复的会话，窗口根本不会弹出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "dist", "HuaweiSmartLife", "HuaweiSmartLife.exe")
LOG = os.path.join(os.path.expanduser("~"), ".huawei_smartlife", "app.log")


def log_size() -> int:
    try:
        return os.path.getsize(LOG)
    except OSError:
        return -1


def main() -> int:
    if not os.path.exists(EXE):
        print("没有发布产物，先跑 py -3 packaging/build_release.py")
        return 1

    before = log_size()
    started = time.time()
    process = subprocess.Popen(
        [EXE, "--tray"], cwd=os.path.dirname(EXE),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"已启动 pid={process.pid}，等待 8 秒…")

    alive = True
    for _ in range(8):
        time.sleep(1)
        if process.poll() is not None:
            alive = False
            break

    elapsed = time.time() - started
    print(f"存活: {alive}（{elapsed:.1f}s，退出码 {process.poll()}）")
    after = log_size()
    print(f"日志增长: {after - before if before >= 0 else '无日志文件'} 字节")

    if process.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
        )
        time.sleep(1)
        print("已结束进程")
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
