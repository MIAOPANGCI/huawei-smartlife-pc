"""华为智慧生活 PC 控制台 —— 启动入口。

运行方式（在项目根目录下）：
    python main.py          从终端启动，日志直接打到终端
    双击 run.vbs             无控制台窗口启动（推荐日常使用）
    双击 run.bat             首次安装依赖用，会显示窗口输出安装进度
"""

from __future__ import annotations

import sys

_TITLE = "华为智慧生活 PC 控制台"


def _report(message: str) -> None:
    """把启动阶段的错误告诉用户。

    用 ``pythonw`` 启动时没有控制台：``sys.stderr`` 可能是 ``None``，也可能是
    个存在但句柄无效的对象。两种情况 ``print`` 都不生效——用户双击后只会看到
    "什么都没有发生"。所以先试着真写一次，失败就改用系统弹窗。
    """
    stream = sys.stderr
    if stream is not None:
        try:
            print(message, file=stream)
            return
        except Exception:  # noqa: BLE001 - 句柄无效，落到弹窗
            pass
    try:
        import ctypes

        # 0x10 = MB_ICONERROR
        ctypes.windll.user32.MessageBoxW(None, message, _TITLE, 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或弹窗失败，只能作罢
        pass


def main() -> int:
    try:
        from app.main import main as run_app
    except ImportError as error:  # 缺少依赖时给出可操作的提示
        _report(
            f"依赖未安装或缺失：{error}\n\n"
            "请先执行：\n  py -3 -m pip install -r requirements.txt\n\n"
            "或直接双击 run.bat，它会自动安装。"
        )
        return 1
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
