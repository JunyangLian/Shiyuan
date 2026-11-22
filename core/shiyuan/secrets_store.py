"""Windows DPAPI 封装：API Key 不以明文写入 SQLite。

- 密文带 `dpapi:v1:` 前缀，绑定当前 Windows 用户与固定 entropy；
- 旧版本留下的明文在启动时自动加密，并用 secure_delete + VACUUM 清理旧页；
- 非 Windows 环境不支持写入（本项目只发布 Windows 桌面端）。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
from ctypes import wintypes

PREFIX = "dpapi:v1:"
ENTROPY = b"shiyuan.api-key.v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise SecretError("DPAPI 仅在 Windows 可用")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(ENTROPY)
    output_blob = _DataBlob()
    if decrypt:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "shiyuan",
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise SecretError(f"DPAPI 调用失败：{ctypes.WinError()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def is_protected(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def protect(value: str | None) -> str:
    text = (value or "").strip()
    if not text or is_protected(text):
        return text
    encrypted = _dpapi(text.encode("utf-8"), decrypt=False)
    return PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if not is_protected(text):
        return text  # 兼容迁移前的明文
    try:
        encrypted = base64.b64decode(text[len(PREFIX):].encode("ascii"), validate=True)
        return _dpapi(encrypted, decrypt=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, SecretError) as exc:
        raise SecretError(f"API Key 解密失败：{exc}") from exc


def migrate_app_settings(conn: sqlite3.Connection) -> int:
    """把 answer/embedding 设置里的明文 api_key 就地加密，并清理旧页。"""
    if os.name != "nt":
        return 0
    conn.execute("PRAGMA secure_delete = ON")
    migrated = 0
    for key in ("answer", "embedding"):
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            continue
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(stored, dict):
            continue
        api_key = stored.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip() or is_protected(api_key):
            continue
        stored["api_key"] = protect(api_key)
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = ?",
            (json.dumps(stored, ensure_ascii=False), key),
        )
        migrated += 1
    conn.commit()
    if migrated:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        conn.commit()
    return migrated
