from __future__ import annotations

import json
import os

import pytest

from shiyuan import answer, db, embeddings, secrets_store

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI 仅在 Windows 可用")


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "secrets.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _stored(conn, key):
    return conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()["value"]


def test_dpapi_roundtrip_and_idempotence():
    cipher = secrets_store.protect("sk-test")
    assert cipher.startswith("dpapi:v1:")
    assert "sk-test" not in cipher
    assert secrets_store.unprotect(cipher) == "sk-test"
    assert secrets_store.protect(cipher) == cipher
    assert secrets_store.unprotect("legacy-plaintext") == "legacy-plaintext"


def test_migration_encrypts_existing_plaintext(conn):
    for key in ("answer", "embedding"):
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (key, json.dumps({"api_key": "sk-plain", "provider": "openai"})),
        )
    conn.commit()

    assert secrets_store.migrate_app_settings(conn) == 2
    for key in ("answer", "embedding"):
        raw = _stored(conn, key)
        assert "sk-plain" not in raw
        stored = json.loads(raw)
        assert stored["api_key"].startswith("dpapi:v1:")
        assert secrets_store.unprotect(stored["api_key"]) == "sk-plain"
    assert secrets_store.migrate_app_settings(conn) == 0


def test_answer_settings_never_store_plaintext(conn):
    saved = answer.update_settings(
        conn, {"provider": "openai", "model": "gpt-test", "api_key": "sk-secret"}
    )
    assert saved["has_key"] is True
    assert "api_key" not in saved

    raw = _stored(conn, "answer")
    assert "sk-secret" not in raw
    assert "dpapi:v1:" in raw
    assert answer.load_settings(conn)["api_key"] == "sk-secret"

    cleared = answer.update_settings(conn, {"clear_key": True})
    assert cleared["has_key"] is False
    assert answer.load_settings(conn)["api_key"] == ""


def test_embedding_settings_never_store_plaintext(conn):
    saved = embeddings.update_settings(
        conn, {"provider": "openai", "model": "text-embedding-3-small",
               "api_key": "sk-embed"}
    )
    assert saved["has_key"] is True
    raw = _stored(conn, "embedding")
    assert "sk-embed" not in raw
    assert "dpapi:v1:" in raw
    assert embeddings.load_settings(conn)["api_key"] == "sk-embed"
