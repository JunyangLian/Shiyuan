"""运行时路径与配置：所有派生数据只写入应用自有目录，原文件目录只读。"""

from __future__ import annotations

import os
from pathlib import Path

# 不能与安装目录同名：NSIS 默认装到 %LOCALAPPDATA%\Shiyuan，卸载会递归删除该目录，
# 若知识库也在其中，用户数据会随卸载一起丢失。
APP_DIR_NAME = "ShiyuanData"


def app_home() -> Path:
    """应用数据目录。测试通过 SHIYUAN_HOME 覆盖，避免污染真实用户目录。"""
    override = os.environ.get("SHIYUAN_HOME")
    if override:
        base = Path(override)
    else:
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) / APP_DIR_NAME if local_appdata else Path.home() / f".{APP_DIR_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return app_home() / "shiyuan.db"


def api_token_path() -> Path:
    """本机 API 的进程级访问令牌；桌面端通过 Tauri 命令读取。"""
    return app_home() / "api-token"


def api_auth_enabled() -> bool:
    """默认开启；测试或显式排障时可设 SHIYUAN_API_AUTH=0。"""
    return os.environ.get("SHIYUAN_API_AUTH", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def watch_enabled() -> bool:
    """实时监听开关；测试与排障可用 SHIYUAN_WATCH=0 关闭。"""
    return os.environ.get("SHIYUAN_WATCH", "1").strip().lower() not in {"0", "false", "off", "no"}


def reconcile_seconds() -> int:
    """周期对账间隔，兜住监听丢失、休眠与网络盘事件缺失。"""
    try:
        value = int(os.environ.get("SHIYUAN_RECONCILE_SECONDS", "600"))
    except ValueError:
        value = 600
    return max(30, value)


def daily_parse_budget() -> int:
    """每日正文解析文件数上限；0 表示不限制。"""
    try:
        value = int(os.environ.get("SHIYUAN_DAILY_PARSE_BUDGET", "500"))
    except ValueError:
        value = 500
    return value if value > 0 else 10 ** 9


def daily_ocr_budget() -> int:
    """每日 OCR 文件数上限；0 表示不限制。"""
    try:
        value = int(os.environ.get("SHIYUAN_DAILY_OCR_BUDGET", "200"))
    except ValueError:
        value = 200
    return value if value > 0 else 10 ** 9
