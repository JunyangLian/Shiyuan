from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from shiyuan import db, external, indexer, projects, scanner, store
from shiyuan.mcp_server import DEFAULT_PROTOCOL, SERVER_NAME


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "external.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def secured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    monkeypatch.setenv("SHIYUAN_API_AUTH", "on")
    monkeypatch.setenv("SHIYUAN_API_TOKEN", "main-token")
    from shiyuan.api import app

    with TestClient(app) as client:
        yield client


def _token(conn, client_id: str) -> str:
    row = external.get_client(conn, client_id)
    assert row is not None
    return external._reveal(row["token"])


def test_create_list_config_and_delete(conn):
    first = external.create_client(conn, preset="codex")
    second = external.create_client(conn, preset="codex")
    assert first["name"] == "Codex"
    assert second["name"] == "Codex 2"
    assert first["transport"] == "mcp"

    items = external.list_clients(conn)
    assert [item["client_id"] for item in items] == [second["client_id"], first["client_id"]]
    assert "token" not in items[0]

    token = _token(conn, first["client_id"])
    assert token.startswith(external.TOKEN_PREFIX)
    assert external.match_token(conn, token)["client_id"] == first["client_id"]
    assert external.match_token(conn, token + "x") is None

    config = external.client_config(conn, first["client_id"])
    server = json.loads(config["mcp"]["snippet"])["mcpServers"]["shiyuan"]
    assert server["env"]["SHIYUAN_EXT_TOKEN"] == token
    assert config["http"] is None
    assert config["notes"]

    external.record_usage(conn, first["client_id"])
    refreshed = external.get_client(conn, first["client_id"])
    assert refreshed["call_count"] == 1
    assert refreshed["last_call_at"] is not None

    assert external.delete_client(conn, first["client_id"])["deleted"] is True
    assert external.match_token(conn, token) is None
    assert len(external.list_clients(conn)) == 1


def test_transport_and_preset_validation(conn):
    with pytest.raises(ValueError):
        external.create_client(conn, preset="unknown")
    with pytest.raises(ValueError):
        external.create_client(conn, preset="codex", transport="http")

    custom = external.create_client(conn, preset="custom", transport="http")
    config = external.client_config(conn, custom["client_id"])
    assert config["http"]["base_url"].startswith("http://127.0.0.1:")
    assert config["http"]["authorization"].startswith("Bearer syext_")
    assert config["http"]["endpoints"]
    assert config["mcp"] is None


def test_test_client_records_status(conn, monkeypatch):
    client = external.create_client(conn, preset="claude_code")
    monkeypatch.setattr(external, "_test_mcp", lambda token: "ok")
    result = external.test_client(conn, client["client_id"])
    assert result["ok"] is True
    assert result["status"] == "ok"
    row = external.get_client(conn, client["client_id"])
    assert row["last_status"] == "ok"
    assert row["last_test_at"] is not None

    def fail(token):
        raise RuntimeError("握手失败")

    monkeypatch.setattr(external, "_test_mcp", fail)
    result = external.test_client(conn, client["client_id"])
    assert result["ok"] is False
    assert result["message"] == "握手失败"
    row = external.get_client(conn, client["client_id"])
    assert row["last_status"] == "failed"


def test_mcp_process_accepts_only_issued_token(conn, tmp_path):
    client = external.create_client(conn, preset="deepseek")
    token = _token(conn, client["client_id"])
    core = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_file = tmp_path / "external.db"
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": DEFAULT_PROTOCOL, "capabilities": {}},
    }) + "\n"

    def run(env_token: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["SHIYUAN_HOME"] = str(tmp_path)
        env["SHIYUAN_EXT_TOKEN"] = env_token
        return subprocess.run(
            [sys.executable, "-m", "shiyuan.mcp_server", "--once", "--db", str(db_file)],
            input=request, capture_output=True, text=True, encoding="utf-8",
            cwd=core, env=env, timeout=30,
        )

    ok = run(token)
    assert ok.returncode == 0, ok.stderr
    response = json.loads(ok.stdout.strip().splitlines()[0])
    assert response["result"]["serverInfo"]["name"] == SERVER_NAME

    denied = run("syext_not-a-real-token")
    assert denied.returncode == 2
    assert "invalid external token" in denied.stderr


def test_http_token_is_read_only_and_revocable(secured_client):
    main = {"Authorization": "Bearer main-token"}
    created = secured_client.post(
        "/api/external/clients",
        json={"name": "我的 Agent", "preset": "custom", "transport": "http"},
        headers=main,
    )
    assert created.status_code == 201
    assert created.json()["client"]["project_id"] == projects.GLOBAL_PROJECT_ID
    client_id = created.json()["client"]["client_id"]

    config = secured_client.get(
        f"/api/external/clients/{client_id}/config", headers=main
    ).json()
    token = config["http"]["authorization"].removeprefix("Bearer ")
    headers = {"Authorization": f"Bearer {token}"}

    assert secured_client.get("/api/health", headers=headers).status_code == 200
    assert secured_client.get("/api/search", params={"q": "测试"}, headers=headers).status_code == 200
    forbidden = secured_client.get("/api/external/clients", headers=headers)
    assert forbidden.status_code == 403
    assert "只读" in forbidden.json()["detail"]
    assert secured_client.delete("/api/sources/missing", headers=headers).status_code == 403

    listed = secured_client.get("/api/external/clients", headers=main).json()["items"]
    assert listed[0]["call_count"] >= 2

    assert secured_client.delete(
        f"/api/external/clients/{client_id}", headers=main
    ).status_code == 200
    assert secured_client.get("/api/health", headers=headers).status_code == 401


def test_http_token_cannot_escape_bound_project(secured_client, tmp_path):
    main = {"Authorization": "Bearer main-token"}
    conn = db.connect(tmp_path / "home" / "shiyuan.db")
    try:
        for name, text in (("甲库", "甲库专用 样本量 42"), ("乙库", "乙库专用 样本量 99")):
            root = tmp_path / name
            root.mkdir()
            (root / "记录.md").write_text(text, encoding="utf-8")
            source = store.add_source(conn, root)
            scanner.scan_source(conn, source["source_id"])
            indexer.index_pending(conn, source["source_id"])
    finally:
        conn.close()

    project = secured_client.post(
        "/api/projects",
        json={"name": "甲项目", "folders": [str(tmp_path / "甲库")]},
        headers=main,
    ).json()["project"]
    pid = project["project_id"]
    created = secured_client.post(
        "/api/external/clients",
        json={"preset": "custom", "transport": "http", "project_id": pid},
        headers=main,
    ).json()["client"]
    assert created["project_id"] == pid
    config = secured_client.get(
        f"/api/external/clients/{created['client_id']}/config", headers=main
    ).json()
    headers = {"Authorization": config["http"]["authorization"]}

    payload = secured_client.get(
        "/api/search", params={"q": "专用"}, headers=headers).json()
    assert payload["scope"]["project_id"] == pid
    assert payload["scope"]["used"] == "expanded"
    assert payload["items"] and all("甲库" in item["path"] for item in payload["items"])

    escaped = secured_client.get(
        "/api/search",
        params={"q": "专用", "project_id": projects.GLOBAL_PROJECT_ID},
        headers=headers,
    )
    assert escaped.status_code == 403
    wider = secured_client.get(
        "/api/search", params={"q": "专用", "scope": "global"}, headers=headers).json()
    assert wider["scope"]["used"] == "expanded"
    assert all("乙库" not in item["path"] for item in wider["items"])

    sources = secured_client.get("/api/sources", headers=headers).json()["items"]
    assert [item["label"] for item in sources] == ["甲库"]
    stats = secured_client.get("/api/stats", headers=headers).json()
    assert stats["scope"]["project_id"] == pid
    assert stats["files"]["available"] == 1


def test_external_endpoints_validate_presets(secured_client):
    main = {"Authorization": "Bearer main-token"}
    bad = secured_client.post(
        "/api/external/clients", json={"preset": "nope"}, headers=main
    )
    assert bad.status_code == 400
    assert secured_client.get(
        "/api/external/clients/missing/config", headers=main
    ).status_code == 404
