"""华为智慧生活 PC 控制台 —— 启动入口。

运行方式（在项目根目录下）：
    python main.py
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from app.main import main as run_app
    except ImportError as error:  # 缺少依赖时给出可操作的提示
        print(f"依赖未安装或缺失：{error}", file=sys.stderr)
        print("请先执行：pip install -r requirements.txt", file=sys.stderr)
        return 1
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
