from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def secured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    monkeypatch.setenv("SHIYUAN_API_AUTH", "on")
    monkeypatch.setenv("SHIYUAN_API_TOKEN", "test-token")
    from shiyuan.api import app

    with TestClient(app) as client:
        yield client, tmp_path / "home" / "api-token"


def test_api_requires_token(secured_client):
    client, token_file = secured_client
    missing = client.get("/api/health")
    assert missing.status_code == 401
    assert "未授权" in missing.json()["detail"]

    wrong = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401

    bearer = client.get("/api/health", headers={"Authorization": "Bearer test-token"})
    assert bearer.status_code == 200
    assert bearer.json()["status"] == "ok"

    header = client.get("/api/health", headers={"X-Shiyuan-Token": "test-token"})
    assert header.status_code == 200

    assert token_file.read_text(encoding="utf-8").strip() == "test-token"


def test_cors_allows_only_desktop_origins(secured_client):
    client, _ = secured_client
    preflight = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:1420",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:1420"
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()

    unauthorized = client.get("/api/health", headers={"Origin": "http://localhost:1420"})
    assert unauthorized.status_code == 401
    # CORS 在最外层，401 也应带跨域头，前端才能读到错误。
    assert unauthorized.headers["access-control-allow-origin"] == "http://localhost:1420"

    foreign = client.get(
        "/api/health",
        headers={"Origin": "http://evil.example.com", "Authorization": "Bearer test-token"},
    )
    assert foreign.status_code == 200
    assert "access-control-allow-origin" not in foreign.headers


def test_generated_token_is_written_to_app_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    monkeypatch.setenv("SHIYUAN_API_AUTH", "on")
    monkeypatch.delenv("SHIYUAN_API_TOKEN", raising=False)
    from shiyuan.api import app

    with TestClient(app) as client:
        token_file = tmp_path / "home" / "api-token"
        token = token_file.read_text(encoding="utf-8").strip()
        assert len(token) >= 20
        response = client.get(
            "/api/health", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
