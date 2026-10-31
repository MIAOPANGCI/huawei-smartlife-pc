"""开机启动器：一个只有几十行逻辑、但带自己名字信息的 exe。

为什么需要它
------------
任务管理器「启动应用」那一栏的名称，读的是**启动目标的产品名**，不是快捷方式的
文件名。本程序以 ``pythonw.exe`` 运行，于是那一栏永远写着「Python」——用户认不出
是谁，还以为是陌生的自启项。

这个 exe 的产品名是「华为智慧生活 PC 控制台」，自启动项指向它，它再把请求转给
pythonw 拉起 main.py。它自己不实现任何业务，退出得也快（拉起子进程即结束）。

怎么找 pythonw
--------------
优先读同目录 ``launcher.json`` 里记的路径（打包时写入），失效则退回 PATH 上的
``pythonw.exe`` / ``pyw.exe``。项目目录挪动不会影响它——它只认自己的同级目录。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

CONFIG_NAME = "launcher.json"


def _root() -> str:
    """exe 所在目录。

    打包后 ``__file__`` 指向临时解包目录，必须用 ``argv[0]``。
    """
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _load_config(root: str) -> dict:
    try:
        with open(os.path.join(root, CONFIG_NAME), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _interpreter(config: dict) -> str | None:
    candidates: list[str] = []
    configured = str(config.get("pythonw") or "")
    if configured:
        candidates.append(configured)
    for name in ("pythonw.exe", "pyw.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def main() -> int:
    root = _root()
    entry = os.path.join(root, "main.py")
    if not os.path.exists(entry):
        return 2

    interpreter = _interpreter(_load_config(root))
    if interpreter is None:
        return 3

    command = [interpreter]
    # 只有 py launcher 需要显式指定版本；pythonw.exe / python.exe 直接吃脚本路径
    if os.path.basename(interpreter).lower() in ("py.exe", "pyw.exe"):
        command.append("-3")
    command.append(entry)
    command.extend(sys.argv[1:])

    # 脱离父进程：启动器立刻退出，应用继续跑；否则关掉启动器会带走子进程
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        subprocess.Popen(
            command,
            cwd=root,
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
