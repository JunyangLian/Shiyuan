from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import chat, db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "chat.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _result(answer: str = "样本量 42 例 [1]") -> dict:
    return {
        "question": "样本量是多少",
        "answer": answer,
        "refused": False,
        "reason": None,
        "provider": "extractive",
        "model": None,
        "citations": [],
        "evidence_count": 1,
        "warning": None,
        "coverage": {
            "content_indexed": 1,
            "content_pending": 0,
            "content_empty": 0,
            "content_failed": 0,
            "ocr_indexed": 0,
            "ocr_pending": 0,
        },
    }


def test_chat_session_crud_and_messages(conn):
    session = chat.create_session(conn)
    assert session["title"] == "新对话"
    assert session["message_count"] == 0

    renamed = chat.rename_session(conn, session["session_id"], "样本量讨论")
    assert renamed["title"] == "样本量讨论"

    chat.append_exchange(conn, session["session_id"], "样本量是多少", _result())
    messages = chat.list_messages(conn, session["session_id"])

    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "样本量是多少"
    assert messages[1]["payload"]["answer"] == "样本量 42 例 [1]"
    assert chat.get_session(conn, session["session_id"])["message_count"] == 2

    assert [item["session_id"] for item in chat.list_sessions(conn)] == [session["session_id"]]
    assert chat.delete_session(conn, session["session_id"]) is True
    assert chat.list_sessions(conn) == []
    assert chat.delete_session(conn, session["session_id"]) is False


def test_chat_title_auto_filled_from_first_question(conn):
    session = chat.create_session(conn)

    chat.append_exchange(conn, session["session_id"], "项目A 的最终样本量是多少？", _result())

    assert chat.get_session(conn, session["session_id"])["title"] == "项目A 的最终样本量是多少？"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def _indexed_source(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("本次样本量 42 例\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    client.post(f"/api/sources/{source_id}/scan")
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)
    return source_id


def test_api_ask_persists_session(client, tmp_path):
    _indexed_source(client, tmp_path)
    session = client.post("/api/chat/sessions", json={}).json()

    asked = client.post("/api/ask", json={
        "question": "样本量是多少", "session_id": session["session_id"],
    })
    assert asked.status_code == 200
    assert asked.json()["session_id"] == session["session_id"]

    detail = client.get(f"/api/chat/sessions/{session['session_id']}").json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["session"]["title"] == "样本量是多少"

    renamed = client.patch(f"/api/chat/sessions/{session['session_id']}", json={"title": "归档"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "归档"

    assert client.delete(f"/api/chat/sessions/{session['session_id']}").status_code == 200
    assert client.get(f"/api/chat/sessions/{session['session_id']}").status_code == 404


def test_api_ask_rejects_unknown_session(client):
    response = client.post("/api/ask", json={"question": "样本量", "session_id": "chat_missing"})
    assert response.status_code == 404
