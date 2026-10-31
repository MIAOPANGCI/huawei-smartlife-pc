"""版本号唯一来源。

为什么放在协议层而不是 ``app/``：依赖方向是 ``app`` → ``huawei_smartlife``，
反过来会让纯协议层依赖 GUI 包。发版时**只改这一个字符串**，界面、exe 版本资源、
Git tag 都由它派生（打包脚本会读这里）。
"""

from __future__ import annotations

VERSION = "1.0.0"

#: 公开的版本字符串，遵循语义化版本：主版本.次版本.修订号
__all__ = ["VERSION"]
