"""隔离验证启动器：把 exe 复制到临时目录，用一个假的 main.py 记录它被怎么调起。

不碰真实项目目录，也不会启动第二个应用实例。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "HuaweiSmartLife.exe")

PROBE = """
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.out"),
          "w", encoding="utf-8") as handle:
    json.dump({"argv": sys.argv, "cwd": os.getcwd()}, handle, ensure_ascii=False)
"""


def main() -> int:
    if not os.path.exists(EXE):
        print("跳过：还没打包启动器（py -3 launcher/build.py）")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(EXE, os.path.join(tmp, "HuaweiSmartLife.exe"))
        # 启动器固定找同目录的 main.py，这里换成探针
        with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as handle:
            handle.write(PROBE)

        python = sys.executable
        windowed = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(windowed):
            python = windowed
        with open(os.path.join(tmp, "launcher.json"), "w", encoding="utf-8") as handle:
            json.dump({"pythonw": python}, handle, ensure_ascii=False)

        exe = os.path.join(tmp, "HuaweiSmartLife.exe")
        started = time.time()
        completed = subprocess.run([exe, "--tray"], timeout=60, capture_output=True)
        print(f"启动器退出码: {completed.returncode}  耗时: {time.time() - started:.2f}s")

        out = os.path.join(tmp, "probe.out")
        for _ in range(30):
            if os.path.exists(out):
                break
            time.sleep(0.2)

        if not os.path.exists(out):
            print("探针没有输出：启动器没能拉起脚本")
            return 1

        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        print("子进程 argv:", data["argv"])
        print("子进程 cwd :", data["cwd"])

        ok_args = data["argv"][1:] == ["--tray"]
        ok_cwd = os.path.normcase(os.path.abspath(data["cwd"])) == os.path.normcase(
            os.path.abspath(tmp)
        )
        print(("OK   " if ok_args else "FAIL ") + "参数原样转发")
        print(("OK   " if ok_cwd else "FAIL ") + "工作目录 = exe 所在目录")
        return 0 if (ok_args and ok_cwd) else 1


if __name__ == "__main__":
    raise SystemExit(main())
