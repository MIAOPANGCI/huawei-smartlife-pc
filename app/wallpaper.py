"""把窗口钉到 Windows 桌面壁纸层。

普通无边框窗口只是"浮在桌面上方的窗口"：按 Win+D 会被一起收走，任务栏上也会
占一格。真正的壁纸层是把窗口 ``SetParent`` 到桌面那一层里，让它变成桌面的一部分：

- 其它窗口正常盖住它（它不是悬浮层）
- 按 Win+D「显示桌面」时它还在（它是子窗口，不参与最小化）
- 不进任务栏、不进 Alt-Tab

**挂到哪一层**（实测 Windows 11 24H2 / 2560×1440）：

- ``Progman`` 下挂着 ``SHELLDLL_DefView``（桌面图标）和一个 ``WorkerW``；
- 挂到 ``WorkerW`` 或 ``Progman`` 上**看不到**——壁纸是 DefView 自己画的，
  它盖在这两个窗口上面；
- 挂到 ``SHELLDLL_DefView`` 上就能正常显示。

所以优先级是：DefView → Progman 的子 WorkerW → Progman → 经典顶层 WorkerW。
最后两个在 Win10 上是有效的（那时壁纸在独立的 WorkerW 里）。

**两个必须注意的点**：

1. 子窗口不能是分层窗口（layered）。``WA_TranslucentBackground`` 的 Qt 窗口
   挂上去会**完全不渲染**，所以壁纸模式下窗口必须不透明、四角用 region 遮罩切掉。
2. ``SetParent`` 之后坐标变成相对父窗口，要用 ``ClientToScreen`` 求偏移换算。

全部用 ctypes，不引入 pywin32。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_SMTO_NORMAL = 0x0000
_SPAWN_WORKERW = 0x052C     # 未文档化：让 Progman 把壁纸移交给独立的 WorkerW

_PROGMAN = "Progman"
_WORKERW = "WorkerW"
_DEFVIEW = "SHELLDLL_DefView"
_DESKTOP_HOSTS = (_DEFVIEW, _WORKERW, _PROGMAN)

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

_available = sys.platform.startswith("win")
_user32 = ctypes.WinDLL("user32", use_last_error=True) if _available else None

_ENUM_PROC = (
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    if _available
    else None
)


def _prepare() -> None:
    """声明参数与返回类型。

    不声明的话 ctypes 默认把句柄当 32 位 int，64 位系统上句柄会被截断，
    表现是"偶尔能找到、偶尔找到个错的"。
    """
    if not _available:
        return
    u = _user32
    u.FindWindowW.restype = wintypes.HWND
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowExW.restype = wintypes.HWND
    u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.SetParent.restype = wintypes.HWND
    u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    u.GetParent.restype = wintypes.HWND
    u.GetParent.argtypes = [wintypes.HWND]
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.EnumWindows.restype = wintypes.BOOL
    u.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    u.SendMessageTimeoutW.restype = wintypes.LPARAM
    u.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    u.ClientToScreen.restype = wintypes.BOOL
    u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.IsWindow.restype = wintypes.BOOL
    u.IsWindow.argtypes = [wintypes.HWND]
    u.SetWindowPos.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    u.ShowWindow.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


_prepare()

_GA_PARENT = 1


def supported() -> bool:
    """只有 Windows 有这套东西。"""
    return _available


def _class_of(hwnd: int) -> str:
    if not _available or not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
    return buffer.value


def _progman() -> int:
    return int(_user32.FindWindowW(_PROGMAN, None) or 0)


def _defview() -> int:
    """桌面图标视图。壁纸实际是它画的，挂到它下面才看得到。"""
    progman = _progman()
    if not progman:
        return 0
    view = _user32.FindWindowExW(wintypes.HWND(progman), None, _DEFVIEW, None)
    if view:
        return int(view)
    # 某些版本它挂在别的容器下，退一步全局找一次
    return int(_user32.FindWindowW(_DEFVIEW, None) or 0)


def _spawn_workerw() -> None:
    """给 Progman 发那条未文档化消息，让它把壁纸拆到独立 WorkerW（仅老版本有效）。"""
    progman = _progman()
    if not progman:
        return
    result = ctypes.c_size_t()
    _user32.SendMessageTimeoutW(
        wintypes.HWND(progman), _SPAWN_WORKERW, 0, 0, _SMTO_NORMAL, 500, ctypes.byref(result)
    )


def _hosts() -> list[int]:
    """按可靠性排序的候选父窗口。"""
    hosts: list[int] = []
    view = _defview()
    if view:
        hosts.append(view)

    progman = _progman()
    if progman:
        inner = _user32.FindWindowExW(wintypes.HWND(progman), None, _WORKERW, None)
        if inner:
            hosts.append(int(inner))
        hosts.append(progman)

    # 经典 Win10：壁纸在顶层 WorkerW 里，且它是图标视图的下一个兄弟
    found: list[int] = []

    @_ENUM_PROC
    def _callback(hwnd, _lparam) -> bool:
        if _user32.FindWindowExW(wintypes.HWND(hwnd), None, _DEFVIEW, None):
            sibling = _user32.FindWindowExW(None, wintypes.HWND(hwnd), _WORKERW, None)
            if sibling:
                found.append(int(sibling))
        return True

    _user32.EnumWindows(_callback, 0)
    hosts.extend(found)
    return hosts


def parent_of(hwnd: int) -> int:
    """真正的父窗口（``GetParent`` 对非 WS_CHILD 窗口返回 0，这里用 GetAncestor）。"""
    if not _available or not hwnd:
        return 0
    parent = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_PARENT)
    return int(parent or 0)


def is_attached(hwnd: int) -> bool:
    """当前是不是还挂在桌面层上（Explorer 重启后会变 False）。"""
    if not _available or not hwnd:
        return False
    if not _user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    return _class_of(parent_of(hwnd)) in _DESKTOP_HOSTS


def attach(hwnd: int) -> tuple[int, int] | None:
    """把窗口挂到桌面层。

    返回父窗口客户区原点的屏幕坐标——挂上去之后 Qt 的坐标是相对父窗口的，
    调用方要用这个偏移换算。失败返回 ``None``（调用方保持普通窗口）。
    """
    if not _available or not hwnd:
        return None
    if not _user32.IsWindow(wintypes.HWND(hwnd)):
        return None

    _spawn_workerw()

    for host in _hosts():
        _user32.SetParent(wintypes.HWND(hwnd), wintypes.HWND(host))
        if parent_of(hwnd) != host:
            continue
        # 挂上去默认排在兄弟的最下面（会被图标视图盖住），提到最前才能看见
        _user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(0), 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
        _user32.ShowWindow(wintypes.HWND(hwnd), 5)      # SW_SHOW
        point = wintypes.POINT(0, 0)
        _user32.ClientToScreen(wintypes.HWND(host), ctypes.byref(point))
        return (int(point.x), int(point.y))

    return None


def detach(hwnd: int) -> None:
    """放回普通顶层窗口。"""
    if not _available or not hwnd:
        return
    if not _user32.IsWindow(wintypes.HWND(hwnd)):
        return
    if parent_of(hwnd):
        _user32.SetParent(wintypes.HWND(hwnd), None)


def raise_in_parent(hwnd: int) -> None:
    """把窗口提到同层最前（重新挂载、或被图标盖住之后用）。"""
    if not _available or not hwnd or not _user32.IsWindow(wintypes.HWND(hwnd)):
        return
    _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(0), 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )
