"""外部 Agent 连接：让 Codex、Claude Code、DeepSeek Harness 等本机客户端
以只读方式调用拾源知识库。

两种连接方式：
- MCP（stdio）：生成 MCP 客户端配置，直接启动 shiyuan.mcp_server；
- HTTP API：生成本机 API 地址与 Bearer 令牌。

每个连接有独立令牌，可单独撤销；令牌用 DPAPI 保护，列表接口不回显明文。
HTTP 令牌只允许访问只读检索、问答与统计接口。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import config, db, secrets_store, store

TOKEN_PREFIX = "syext_"
PRESETS: dict[str, dict[str, Any]] = {
    "codex": {
        "label": "Codex",
        "hint": "OpenAI Codex CLI",
        "transports": ["mcp"],
    },
    "claude_code": {
        "label": "Claude Code",
        "hint": "Anthropic Claude Code",
        "transports": ["mcp"],
    },
    "deepseek": {
        "label": "DeepSeek Harness",
        "hint": "DeepSeek 本地 Agent",
        "transports": ["mcp"],
    },
    "custom": {
        "label": "自定义 Agent",
        "hint": "通用 MCP 客户端或 HTTP Agent",
        "transports": ["mcp", "http"],
    },
}
TRANSPORT_LABEL = {"mcp": "MCP · stdio", "http": "HTTP API"}
HTTP_ENDPOINTS = [
    {"method": "GET", "path": "/api/search?q=...", "description": "检索文件名、路径与正文"},
    {"method": "POST", "path": "/api/ask", "description": "基于已建库内容回答问题"},
    {"method": "GET", "path": "/api/sources", "description": "列出授权数据源"},
    {"method": "GET", "path": "/api/stats", "description": "知识库覆盖率与维护状态"},
]


def _protect(token: str) -> str:
    try:
        return secrets_store.protect(token)
    except Exception:
        return token


def _reveal(value: str) -> str:
    try:
        return secrets_store.unprotect(value)
    except Exception:
        return ""


def _client_dict(row: sqlite3.Row) -> dict:
    preset = PRESETS.get(row["preset"], {})
    return {
        "client_id": row["client_id"],
        "name": row["name"],
        "preset": row["preset"],
        "preset_label": preset.get("label", row["preset"]),
        "preset_hint": preset.get("hint", ""),
        "transport": row["transport"],
        "transport_label": TRANSPORT_LABEL.get(row["transport"], row["transport"]),
        "state": row["state"],
        "created_at": row["created_at"],
        "last_test_at": row["last_test_at"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "call_count": row["call_count"],
        "last_call_at": row["last_call_at"],
    }


def get_client(conn: sqlite3.Connection, client_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM external_clients WHERE client_id = ?", (client_id,)
    ).fetchone()


def list_clients(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM external_clients ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    return [_client_dict(row) for row in rows]


def create_client(conn: sqlite3.Connection, *, preset: str, transport: str = "mcp",
                  name: str | None = None) -> dict:
    preset = (preset or "").strip()
    if preset not in PRESETS:
        raise ValueError("未知的 Agent 品牌")
    transport = (transport or "mcp").strip().lower()
    if transport not in PRESETS[preset]["transports"]:
        raise ValueError("该品牌不支持这种连接方式")

    base = (name or "").strip()[:40] or str(PRESETS[preset]["label"])
    existing = {
        row["name"] for row in conn.execute("SELECT name FROM external_clients").fetchall()
    }
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base} {suffix}"
        suffix += 1

    client_id = store.new_id("ext")
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO external_clients"
        " (client_id, name, preset, transport, token, state, created_at, call_count)"
        " VALUES (?, ?, ?, ?, ?, 'active', ?, 0)",
        (client_id, candidate, preset, transport, _protect(token), store.now_iso()),
    )
    conn.commit()
    row = get_client(conn, client_id)
    assert row is not None
    return _client_dict(row)


def delete_client(conn: sqlite3.Connection, client_id: str) -> dict:
    row = get_client(conn, client_id)
    if row is None:
        raise KeyError(client_id)
    conn.execute("DELETE FROM external_clients WHERE client_id = ?", (client_id,))
    conn.commit()
    return {"client_id": client_id, "deleted": True}


def match_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """按明文令牌匹配一个启用中的连接；用于 HTTP 与 MCP 鉴权。"""
    if not token:
        return None
    rows = conn.execute(
        "SELECT * FROM external_clients WHERE state = 'active'"
    ).fetchall()
    for row in rows:
        stored = _reveal(row["token"])
        if stored and hmac.compare_digest(stored, token):
            return row
    return None


def record_usage(conn: sqlite3.Connection, client_id: str) -> None:
    conn.execute(
        "UPDATE external_clients SET call_count = call_count + 1, last_call_at = ?"
        " WHERE client_id = ?",
        (store.now_iso(), client_id),
    )
    conn.commit()


def authenticate(token: str) -> dict | None:
    """HTTP 中间件使用：校验外部令牌并记录一次调用。"""
    if not token:
        return None
    conn = db.connect()
    try:
        row = match_token(conn, token)
        if row is None:
            return None
        record_usage(conn, row["client_id"])
        return _client_dict(row)
    finally:
        conn.close()


def client_config(conn: sqlite3.Connection, client_id: str) -> dict:
    row = get_client(conn, client_id)
    if row is None:
        raise KeyError(client_id)
    token = _reveal(row["token"])
    payload: dict[str, Any] = {
        "client_id": client_id,
        "name": row["name"],
        "preset": row["preset"],
        "transport": row["transport"],
        "mcp": None,
        "http": None,
        "notes": [],
    }
    if row["transport"] == "mcp":
        from . import mcp_server

        snippet = mcp_server.config_snippet(token)
        server = snippet["mcpServers"]["shiyuan"]
        payload["mcp"] = {
            "command": server["command"],
            "args": server["args"],
            "cwd": server.get("cwd"),
            "env": server.get("env", {}),
            "snippet": json.dumps(snippet, ensure_ascii=True, indent=2),
        }
        payload["notes"] = [
            "把这段 mcpServers 配置放进对应 Agent 的 MCP 设置中即可。",
            "MCP 为只读访问：可以检索、读取正文与统计，不会扫描或修改原文件。",
            "删除该连接后，令牌立即失效。",
        ]
    else:
        port = os.environ.get("SHIYUAN_PORT", "8737")
        payload["http"] = {
            "base_url": f"http://127.0.0.1:{port}",
            "authorization": f"Bearer {token}",
            "endpoints": HTTP_ENDPOINTS,
        }
        payload["notes"] = [
            "在 Agent 的 HTTP 工具配置中填入 Base URL 与 Authorization 请求头。",
            "该令牌只允许只读检索、问答与统计接口，不能修改数据源或设置。",
            "删除该连接后，令牌立即失效。",
        ]
    return payload


def _test_mcp(token: str) -> str:
    from . import mcp_server

    command = mcp_server.server_command() + ["--once", "--db", str(db.db_path())]
    env = os.environ.copy()
    env["SHIYUAN_HOME"] = str(config.app_home())
    env["SHIYUAN_EXT_TOKEN"] = token
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": mcp_server.DEFAULT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "shiyuan-desktop", "version": "test"},
        },
    }) + "\n"
    completed = subprocess.run(
        command,
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "MCP 进程启动失败").strip()
        raise RuntimeError(detail[:300])
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("MCP 进程没有返回握手响应")
    try:
        response = json.loads(lines[0])
    except ValueError as exc:
        raise RuntimeError("MCP 返回内容不是 JSON") from exc
    name = ((response.get("result") or {}).get("serverInfo") or {}).get("name")
    if name != mcp_server.SERVER_NAME:
        raise RuntimeError("MCP 握手响应异常")
    return "MCP 握手成功，知识库可读"


def _test_http(token: str) -> str:
    port = os.environ.get("SHIYUAN_PORT", "8737")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(str(exc)[:300]) from exc
    if payload.get("status") != "ok":
        raise RuntimeError("本机 API 未就绪")
    return "HTTP 接口连通，知识库可读"


def test_client(conn: sqlite3.Connection, client_id: str) -> dict:
    row = get_client(conn, client_id)
    if row is None:
        raise KeyError(client_id)
    token = _reveal(row["token"])
    started = time.monotonic()
    try:
        message = _test_mcp(token) if row["transport"] == "mcp" else _test_http(token)
        ok = True
        status = "ok"
        error = None
    except Exception as exc:
        ok = False
        status = "failed"
        error = str(exc)[:300] or "连接失败"
        message = error
    latency = int((time.monotonic() - started) * 1000)
    conn.execute(
        "UPDATE external_clients SET last_test_at = ?, last_status = ?, last_error = ?"
        " WHERE client_id = ?",
        (store.now_iso(), status, error, client_id),
    )
    conn.commit()
    return {
        "client_id": client_id,
        "ok": ok,
        "status": status,
        "message": message,
        "latency_ms": latency,
    }
