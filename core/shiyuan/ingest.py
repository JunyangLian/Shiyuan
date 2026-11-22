"""首次建库策略：安静 / 平衡 / 快速，以及暂停与恢复。

这里只保存用户偏好，不直接调度扫描；扫描线程会读取这些值来调整批大小、
每文件间隔和暂停点。预算不再决定首次建库能否完成，只影响处理节奏。
"""

from __future__ import annotations

import json
import sqlite3

KEY = "ingest"

PROFILES = {
    "quiet": {"label": "安静", "delay": 0.25, "batch": 40},
    "balanced": {"label": "平衡", "delay": 0.05, "batch": 100},
    "fast": {"label": "快速", "delay": 0.0, "batch": 200},
}

DEFAULT = {"profile": "balanced", "paused": False, "auto_parse": True}


def load(conn: sqlite3.Connection) -> dict:
    data = dict(DEFAULT)
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            if stored.get("profile") in PROFILES:
                data["profile"] = stored["profile"]
            if isinstance(stored.get("paused"), bool):
                data["paused"] = stored["paused"]
            if isinstance(stored.get("auto_parse"), bool):
                data["auto_parse"] = stored["auto_parse"]
    return data


def save(conn: sqlite3.Connection, data: dict) -> dict:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KEY, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    return data


def public_settings(conn: sqlite3.Connection) -> dict:
    data = load(conn)
    return {
        "profile": data["profile"],
        "paused": data["paused"],
        "auto_parse": data["auto_parse"],
        "profiles": [
            {"key": key, "label": value["label"]}
            for key, value in PROFILES.items()
        ],
    }


def update(conn: sqlite3.Connection, *, profile: str | None = None,
           paused: bool | None = None, auto_parse: bool | None = None) -> dict:
    data = load(conn)
    if profile is not None:
        if profile not in PROFILES:
            raise ValueError("未知的建库模式")
        data["profile"] = profile
    if paused is not None:
        data["paused"] = bool(paused)
    if auto_parse is not None:
        data["auto_parse"] = bool(auto_parse)
    return save(conn, data)


def delay_for(profile: str) -> float:
    return PROFILES.get(profile, PROFILES["balanced"])["delay"]


def batch_for(profile: str) -> int:
    return PROFILES.get(profile, PROFILES["balanced"])["batch"]


def paused(conn: sqlite3.Connection) -> bool:
    return load(conn)["paused"]
