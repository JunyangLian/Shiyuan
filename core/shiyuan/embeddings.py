"""可选语义检索：调用 OpenAI 兼容或 Ollama 的 embedding 接口，本地做余弦相似度。

默认关闭（provider = none）。云端 provider 必须先在设置里显式允许上传文本片段；
向量按 chunk 存在本机 SQLite，检索时不联网，只有"生成向量"这一步可能调用模型。
"""

from __future__ import annotations

import array
import json
import math
import sqlite3
import urllib.error
import urllib.request

from . import db, store

SETTINGS_KEY = "embedding"
PROVIDERS = {"none", "openai", "ollama"}
DEFAULT_SETTINGS = {
    "provider": "none",
    "base_url": "",
    "model": "",
    "api_key": "",
    "allow_cloud": False,
    "top_k": 8,
}
BATCH = 32
MAX_TEXT_CHARS = 1200


class EmbeddingError(RuntimeError):
    pass


# ---------------------------------------------------------------- 设置

def load_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            settings.update({key: stored[key] for key in DEFAULT_SETTINGS if key in stored})
    if settings["provider"] not in PROVIDERS:
        settings["provider"] = "none"
    settings["allow_cloud"] = bool(settings["allow_cloud"])
    try:
        settings["top_k"] = max(1, min(int(settings["top_k"]), 50))
    except (TypeError, ValueError):
        settings["top_k"] = 8
    return settings


def public_settings(conn: sqlite3.Connection) -> dict:
    settings = load_settings(conn)
    has_key = bool(settings.pop("api_key", ""))
    settings["has_key"] = has_key
    return settings


def update_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    settings = load_settings(conn)
    provider = payload.get("provider")
    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError("未知的向量提供商")
        settings["provider"] = provider
    for key in ("base_url", "model"):
        if key in payload and payload[key] is not None:
            settings[key] = str(payload[key]).strip()
    if payload.get("allow_cloud") is not None:
        settings["allow_cloud"] = bool(payload["allow_cloud"])
    if payload.get("top_k") is not None:
        try:
            settings["top_k"] = max(1, min(int(payload["top_k"]), 50))
        except (TypeError, ValueError) as exc:
            raise ValueError("top_k 必须是数字") from exc
    if payload.get("clear_key"):
        settings["api_key"] = ""
    elif payload.get("api_key"):
        settings["api_key"] = str(payload["api_key"]).strip()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


def enabled(settings: dict) -> bool:
    if settings["provider"] == "none":
        return False
    if not settings["model"]:
        return False
    if settings["provider"] == "openai":
        return settings["allow_cloud"] and bool(settings["api_key"])
    return True


# ---------------------------------------------------------------- 向量生成

def _post_json(url: str, payload: dict, headers: dict, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise EmbeddingError(f"向量接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EmbeddingError(f"无法连接向量接口：{exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise EmbeddingError("向量接口返回了非 JSON 内容") from exc


def embed_texts(settings: dict, texts: list[str]) -> list[list[float]]:
    if not enabled(settings):
        raise EmbeddingError("语义检索尚未启用或缺少配置")
    if not texts:
        return []
    trimmed = [text[:MAX_TEXT_CHARS] for text in texts]
    if settings["provider"] == "openai":
        base = (settings["base_url"] or "https://api.openai.com/v1").rstrip("/")
        data = _post_json(f"{base}/embeddings",
                          {"model": settings["model"], "input": trimmed},
                          {"Authorization": f"Bearer {settings['api_key']}"})
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            return [list(item["embedding"]) for item in items]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("向量接口返回结构不符合 OpenAI 兼容格式") from exc

    base = (settings["base_url"] or "http://127.0.0.1:11434").rstrip("/")
    data = _post_json(f"{base}/api/embed", {"model": settings["model"], "input": trimmed}, {})
    if isinstance(data.get("embeddings"), list):
        return [list(vector) for vector in data["embeddings"]]
    # 兼容旧版 Ollama：逐条 /api/embeddings
    vectors = []
    for text in trimmed:
        single = _post_json(f"{base}/api/embeddings",
                            {"model": settings["model"], "prompt": text}, {})
        if "embedding" not in single:
            raise EmbeddingError("Ollama 返回结构不符合预期")
        vectors.append(list(single["embedding"]))
    return vectors


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def _cosine(left, right) -> float:
    if len(left) != len(right):
        return -1.0
    dot = norm_left = norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left == 0 or norm_right == 0:
        return -1.0
    return dot / (math.sqrt(norm_left) * math.sqrt(norm_right))


# ---------------------------------------------------------------- 索引

def _pending_chunks(conn: sqlite3.Connection, model: str, source_id: str | None,
                    limit: int) -> list[sqlite3.Row]:
    sql = [
        "SELECT c.chunk_id, c.text FROM chunks c",
        "JOIN content_versions v ON v.version_id = c.version_id",
        "JOIN asset_locations l ON l.asset_id = c.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e"
        " WHERE e.chunk_id = c.chunk_id AND e.model = ?)",
    ]
    params: list = [model]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    sql.append(f"LIMIT {limit}")
    return conn.execute(" ".join(sql), params).fetchall()


def index_pending(conn: sqlite3.Connection, source_id: str | None = None,
                  limit: int = 400) -> dict:
    settings = load_settings(conn)
    counters = {"available": False, "indexed": 0, "remaining": 0,
                "model": settings["model"] or None, "message": None}
    if not enabled(settings):
        counters["message"] = "语义检索未启用"
        return counters
    counters["available"] = True

    processed = 0
    while processed < limit:
        rows = _pending_chunks(conn, settings["model"], source_id,
                               min(BATCH, limit - processed))
        if not rows:
            break
        vectors = embed_texts(settings, [row["text"] for row in rows])
        if len(vectors) != len(rows):
            raise EmbeddingError("向量数量与文本数量不一致")
        for row, vector in zip(rows, vectors):
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, model, dim, vector, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(chunk_id) DO UPDATE SET model = excluded.model,"
                " dim = excluded.dim, vector = excluded.vector, updated_at = excluded.updated_at",
                (row["chunk_id"], settings["model"], len(vector), _pack(vector),
                 store.now_iso()),
            )
        conn.commit()
        processed += len(rows)

    counters["indexed"] = processed
    counters["remaining"] = len(_pending_chunks(conn, settings["model"], source_id, 1))
    return counters


def index_in_new_connection(source_id: str | None = None,
                            store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return index_pending(conn, source_id, limit=200)
    finally:
        conn.close()


def clear(conn: sqlite3.Connection, model: str | None = None) -> int:
    if model:
        cursor = conn.execute("DELETE FROM chunk_embeddings WHERE model = ?", (model,))
    else:
        cursor = conn.execute("DELETE FROM chunk_embeddings")
    conn.commit()
    return cursor.rowcount


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]


# ---------------------------------------------------------------- 语义召回

def semantic_chunks(conn: sqlite3.Connection, text: str, *, source_id: str | None = None,
                    ext: str | None = None, top_k: int = 30) -> list[dict]:
    settings = load_settings(conn)
    if not enabled(settings):
        return []
    query_vector = embed_texts(settings, [text])[0]

    sql = [
        "SELECT e.chunk_id, e.vector, c.text, c.page, c.sheet, c.bbox, c.asset_id, c.version_id,"
        " l.location_id, l.path, l.mtime_ns, a.parser_signature",
        "FROM chunk_embeddings e",
        "JOIN chunks c ON c.chunk_id = e.chunk_id",
        "LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id",
        "JOIN content_versions v ON v.version_id = c.version_id",
        "JOIN asset_locations l ON l.asset_id = c.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE e.model = ? AND v.is_current = 1"
        " AND l.availability = 'AVAILABLE' AND s.state = 'active'",
    ]
    params: list = [settings["model"]]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    if ext:
        sql.append("AND l.ext = ?")
        params.append(ext.lower().lstrip("."))

    scored = []
    for row in conn.execute(" ".join(sql), params):
        similarity = _cosine(query_vector, _unpack(row["vector"]))
        if similarity <= 0:
            continue
        scored.append((similarity, row))
    scored.sort(key=lambda item: -item[0])

    hits = []
    for similarity, row in scored[:top_k]:
        try:
            bbox = json.loads(row["bbox"]) if row["bbox"] else None
        except (TypeError, ValueError):
            bbox = None
        hits.append({
            "chunk_id": row["chunk_id"],
            "asset_id": row["asset_id"],
            "location_id": row["location_id"],
            "path": row["path"],
            "text": row["text"] or "",
            "page": row["page"],
            "sheet": row["sheet"],
            "parser_signature": row["parser_signature"] or "",
            "similarity": similarity,
            "version_id": row["version_id"] or "",
            "mtime_ns": row["mtime_ns"] or 0,
            "bbox": bbox,
            "ocr": (row["parser_signature"] or "").startswith("l2."),
        })
    return hits
