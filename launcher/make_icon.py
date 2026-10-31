"""把代码画的应用图标导出成多尺寸 .ico（PyInstaller 需要）。

为什么不直接在 Qt 里 ``save("app.ico")``：Qt 的 ICO 写入只出一个尺寸，
Windows 任务栏、桌面、Alt-Tab 各取不同尺寸，缺了大图会糊。这里自己拼 ICO
容器——每个尺寸一张 PNG（Vista 之后系统都认 PNG 压缩的 ICO），几十行就够。

用法：
    py -3 launcher/make_icon.py [输出路径]
"""

from __future__ import annotations

import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png_bytes(image) -> bytes:
    """QImage → PNG 字节（走内存，不落临时文件）。"""
    from PySide6.QtCore import QBuffer, QByteArray

    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def build_ico(path: str, sizes: tuple[int, ...] = SIZES) -> str:
    # QPixmap 需要 GUI 应用实例；没有窗口也能渲染，用 offscreen 避免闪一下
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    from app.main import make_app_icon

    if QGuiApplication.instance() is None:
        QGuiApplication(sys.argv[:1])

    icon = make_app_icon()
    frames: list[tuple[int, bytes]] = []
    for size in sizes:
        image = icon.pixmap(size, size).toImage()
        frames.append((size, _png_bytes(image)))

    # ICONDIR(6) + ICONDIRENTRY(16 × n) + 图像数据
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = b""
    blobs = b""
    for size, data in frames:
        dimension = 0 if size >= 256 else size      # 256 用 0 表示
        entries += struct.pack(
            "<BBBBHHII",
            dimension,          # 宽
            dimension,          # 高
            0,                  # 调色板数
            0,                  # 保留
            1,                  # 色彩平面
            32,                 # 位深
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(header + entries + blobs)
    return path


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "app.ico")
    path = build_ico(target)
    print(f"已生成 {path}（{os.path.getsize(path) / 1024:.1f} KB，{len(SIZES)} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
