"""静态检查：代码里引用的 `Qt.xxx` 在当前 PySide6 上是否真的存在。

为什么需要它
------------
`Qt.Escape` 这种写法编辑器不报错、跑起来也不报错，只有真的按到 ESC 才抛
`AttributeError`——用户碰上了才发现。Qt6 把按键常量收进了 `Qt.Key` 命名空间
（正确写法是 `Qt.Key_Escape`），而 `Qt.PointingHandCursor` 这类还留在 `Qt` 上，
很容易记混。

这里用 AST 取所有 `Qt.` 属性访问（跳过注释和字符串），逐个 `hasattr` 校验。

跑法：
    python _api_check.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAN_DIRS = ("app", "huawei_smartlife", "launcher", "packaging")


def collect_qt_attributes(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Qt"
        ):
            names.add(node.attr)
    return names


def main() -> int:
    from PySide6.QtCore import Qt

    missing: dict[str, list[str]] = {}
    checked = 0
    for folder in SCAN_DIRS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for name in collect_qt_attributes(path):
                checked += 1
                if not hasattr(Qt, name):
                    missing.setdefault(name, []).append(
                        f"{path.relative_to(ROOT)}"
                    )

    if missing:
        print(f"发现 {len(missing)} 个不存在的 Qt 属性：")
        for name, files in sorted(missing.items()):
            print(f"  Qt.{name}  <- {', '.join(sorted(set(files)))}")
        print()
        print("Qt6 里按键常量在 Qt.Key 下（Qt.Key_Escape），其余多数仍在 Qt 上。")
        return 1

    print(f"OK：{checked} 处 Qt 属性引用都存在（PySide6 {_version()}）")
    return 0


def _version() -> str:
    import PySide6

    return PySide6.__version__


if __name__ == "__main__":
    raise SystemExit(main())
