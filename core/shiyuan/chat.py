"""对话管理：会话与消息持久化。

会话本身只存标题与时间；助手消息把完整回答结果（引用、覆盖、警告等）
以 JSON 存在 payload 里，重新打开对话时可以直接还原界面。
"""

from __future__ import annotations

import json
import sqlite3

from . import store

DEFAULT_TITLE = "新对话"
MAX_TITLE_CHARS = 60


def _clean_title(title: str | None, *, fallback: str = DEFAULT_TITLE) -> str:
    value = " ".join((title or "").split())
    if not value:
        value = fallback
    return value[:MAX_TITLE_CHARS]


def _title_from_question(question: str) -> str:
    text = " ".join(question.split())
    return (text[:24] + "…") if len(text) > 24 else (text or DEFAULT_TITLE)


def create_session(conn: sqlite3.Connection, title: str | None = None) -> dict:
    session_id = store.new_id("chat")
    now = store.now_iso()
    conn.execute(
        "INSERT INTO chat_sessions (session_id, title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (session_id, _clean_title(title), now, now),
    )
    conn.commit()
    return get_session(conn, session_id)  # type: ignore[return-value]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT s.session_id, s.title, s.created_at, s.updated_at,"
        " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.session_id)"
        " AS message_count"
        " FROM chat_sessions s WHERE s.session_id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT s.session_id, s.title, s.created_at, s.updated_at,"
        " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.session_id)"
        " AS message_count"
        " FROM chat_sessions s ORDER BY s.updated_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def rename_session(conn: sqlite3.Connection, session_id: str, title: str) -> dict | None:
    value = _clean_title(title)
    if not value:
        raise ValueError("会话标题不能为空")
    cursor = conn.execute(
        "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
        (value, store.now_iso(), session_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return get_session(conn, session_id)


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cursor = conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT message_id, role, content, payload, created_at"
        " FROM chat_messages WHERE session_id = ?"
        " ORDER BY created_at, rowid",
        (session_id,),
    ).fetchall()
    messages = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"]) if item["payload"] else None
        except (TypeError, ValueError):
            item["payload"] = None
        messages.append(item)
    return messages


def append_exchange(conn: sqlite3.Connection, session_id: str, question: str,
                    result: dict) -> None:
    """保存一轮问答：用户问题 + 助手完整结果。"""
    now = store.now_iso()
    conn.execute(
        "INSERT INTO chat_messages (message_id, session_id, role, content, payload, created_at)"
        " VALUES (?, ?, 'user', ?, NULL, ?)",
        (store.new_id("msg"), session_id, question, now),
    )
    conn.execute(
        "INSERT INTO chat_messages (message_id, session_id, role, content, payload, created_at)"
        " VALUES (?, ?, 'assistant', ?, ?, ?)",
        (store.new_id("msg"), session_id, result.get("answer", ""),
         json.dumps(result, ensure_ascii=False), store.now_iso()),
    )
    session = conn.execute(
        "SELECT title FROM chat_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if session is not None and session["title"] == DEFAULT_TITLE:
        conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
            (_title_from_question(question), now, session_id),
        )
    else:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
    conn.commit()
