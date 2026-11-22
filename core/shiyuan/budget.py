"""每日解析预算：按自然日统计 L1 / L2 处理文件数，超额后把任务留给下一天。

预算存在 app_settings 的 `budget` 键里（日期 + 两个计数），跨天自动归零。
0 表示不限制，便于本地长期使用或测试。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import config

KEY = "budget"


def _load(conn: sqlite3.Connection) -> dict:
    today = date.today().isoformat()
    data = {"date": today, "l1": 0, "l2": 0}
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict) and stored.get("date") == today:
            data["l1"] = max(0, int(stored.get("l1", 0)))
            data["l2"] = max(0, int(stored.get("l2", 0)))
    return data


def _save(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KEY, json.dumps(data)),
    )
    conn.commit()


def remaining(conn: sqlite3.Connection, stage: str) -> int:
    data = _load(conn)
    if stage == "l1":
        return max(0, config.daily_parse_budget() - data["l1"])
    return max(0, config.daily_ocr_budget() - data["l2"])


def add(conn: sqlite3.Connection, stage: str, count: int) -> None:
    if count <= 0:
        return
    data = _load(conn)
    data["l1" if stage == "l1" else "l2"] += count
    _save(conn, data)


def info(conn: sqlite3.Connection) -> dict:
    data = _load(conn)
    return {
        "date": data["date"],
        "l1_used": data["l1"],
        "l1_limit": config.daily_parse_budget(),
        "l2_used": data["l2"],
        "l2_limit": config.daily_ocr_budget(),
    }
