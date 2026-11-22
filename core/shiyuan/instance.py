"""HTTP 核心单实例守卫。

桌面端启动时会先探测端口；如果核心还在 onefile 解包、尚未监听，可能重复拉起。
两个核心都会写 api-token，后写入的令牌会让先绑端口的实例全部 401。
这里用 Windows 命名互斥量保证同一端口只有一个 HTTP 核心；进程退出自动释放。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
_handle: int | None = None


def acquire_http_instance(port: int = 8737) -> bool:
    global _handle
    if _handle is not None:
        return True
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, f"Local\\ShiyuanHttpCore-{port}")
    if not handle:
        return True  # 创建失败时不要阻塞正常启动
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True
