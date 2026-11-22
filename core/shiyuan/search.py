"""检索：文件名 / 路径（L0）+ 正文片段（L1/L2）混合召回。

正文召回统一交给 retrieval.py：查询先经过 query.analyze（去疑问词、n-gram、同义词），
再按概念命中评分；短于 3 个字符的关键词退化为主键扫描。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import embeddings, indexer, maintenance, query, retrieval

MAX_CANDIDATES = 5000
SNIPPET_WIDTH = 120


def _score(name_l: str, path_l: str, tokens: list[str], query_l: str) -> int:
    score = 0
    if name_l == query_l:
        score += 1000
    elif name_l.startswith(query_l):
        score += 500
    elif query_l in name_l:
        score += 300
    if query_l in path_l:
        score += 60
    for token in tokens:
        if token in name_l:
            score += 30
        elif token in path_l:
            score += 5
    return score


def _snippet(text: str, tokens: list[str]) -> str:
    lower = text.lower()
    at = -1
    for token in tokens:
        found = lower.find(token)
        if found >= 0 and (at < 0 or found < at):
            at = found
    if at < 0:
        at = 0
    start = max(0, at - SNIPPET_WIDTH // 2)
    end = min(len(text), start + SNIPPET_WIDTH)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


def _content_hits(conn: sqlite3.Connection, text: str, analysis: dict, *,
                  source_id: str | None, ext: str | None) -> dict[str, dict]:
    hits: dict[str, dict] = {}
    for hit in retrieval.find_chunks(conn, text, analysis=analysis,
                                     source_id=source_id, ext=ext):
        if hit.location_id in hits:
            continue
        hits[hit.location_id] = {
            "snippet": _snippet(hit.text, analysis["variants"]),
            "page": hit.page,
            "sheet": hit.sheet,
            "level": "L2" if hit.ocr else "L1",
            "bbox": hit.bbox,
        }
    return hits


def _to_item(row: sqlite3.Row, *, match: str, content: dict | None) -> dict:
    path = row["path"]
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    directory = path[: len(path) - len(name)].rstrip("\\/")
    return {
        "location_id": row["location_id"],
        "asset_id": row["asset_id"],
        "name": name,
        "path": path,
        "directory": directory,
        "rel_path": row["rel_path"],
        "ext": row["ext"],
        "size": row["size"],
        "mtime": datetime.fromtimestamp(row["mtime_ns"] / 1e9).astimezone().isoformat(timespec="seconds")
        if row["mtime_ns"] else None,
        "availability": row["availability"],
        "index_level": (content["level"] if content else "L0"),
        "semantic": bool(content and content.get("semantic")),
        "match": match,
        "snippet": content["snippet"] if content else None,
        "page": content["page"] if content else None,
        "sheet": content["sheet"] if content else None,
        "bbox": content.get("bbox") if content else None,
        "source_id": row["source_id"],
        "source_label": row["source_label"],
    }


def _fetch_locations(conn: sqlite3.Connection, location_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not location_ids:
        return {}
    placeholders = ",".join("?" for _ in location_ids)
    rows = conn.execute(
        f"SELECT l.*, s.label AS source_label FROM asset_locations l"
        f" JOIN sources s ON s.source_id = l.source_id"
        f" WHERE l.location_id IN ({placeholders})",
        location_ids,
    ).fetchall()
    return {row["location_id"]: row for row in rows}


def search(conn: sqlite3.Connection, q: str, *, limit: int = 50, offset: int = 0,
           source_id: str | None = None, ext: str | None = None) -> dict:
    text = (q or "").strip()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if not text:
        return {"query": text, "total": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}

    analysis = query.analyze(text, synonyms=query.synonym_map(conn))
    rewritten = analysis["core"] if analysis["core"] and analysis["core"] != text.lower() else None
    tokens = [token.lower() for token in text.split() if token]
    query_l = " ".join(tokens)
    sql = [
        "SELECT l.*, s.label AS source_label FROM asset_locations l",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE s.state = 'active' AND l.availability = 'AVAILABLE'",
    ]
    params: list[str] = []
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    if ext:
        sql.append("AND l.ext = ?")
        params.append(ext.lower().lstrip("."))
    for token in tokens:
        sql.append("AND instr(lower(l.path), ?) > 0")
        params.append(token)
    sql.append(f"LIMIT {MAX_CANDIDATES}")
    name_rows = {row["location_id"]: row for row in conn.execute(" ".join(sql), params)}

    content_hits = _content_hits(conn, text, analysis, source_id=source_id, ext=ext)

    vector_hits: dict[str, dict] = {}
    vector_used = False
    vector_model: str | None = None
    vector_error: str | None = None
    embed_settings = embeddings.load_settings(conn)
    if embeddings.enabled(embed_settings):
        try:
            hits = embeddings.semantic_chunks(
                conn, analysis["core"] or text, source_id=source_id, ext=ext,
                top_k=max(10, embed_settings["top_k"] * 4))
            vector_used = True
            vector_model = embed_settings["model"]
            for hit in hits:
                current = vector_hits.get(hit["location_id"])
                if current and current["similarity"] >= hit["similarity"]:
                    continue
                vector_hits[hit["location_id"]] = {
                    "snippet": _snippet(hit["text"], analysis["variants"]),
                    "page": hit["page"],
                    "sheet": hit["sheet"],
                    "level": "L2" if hit["ocr"] else "L1",
                    "semantic": True,
                    "similarity": hit["similarity"],
                    "bbox": hit.get("bbox"),
                }
        except embeddings.EmbeddingError as exc:
            vector_error = str(exc)

    extra_ids = [location_id for location_id in {**content_hits, **vector_hits}
                 if location_id not in name_rows]
    name_rows.update(_fetch_locations(conn, extra_ids))

    scored = []
    for location_id, row in name_rows.items():
        path_l = row["path"].lower()
        name_l = path_l.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        score = _score(name_l, path_l, tokens, query_l)
        content = content_hits.get(location_id)
        semantic = vector_hits.get(location_id)
        name_matched = score > 0
        if content:
            score += 150 + 10 * min(len(tokens), 5)
        if semantic:
            score += 120 + int(semantic["similarity"] * 100)
        merged = content
        if semantic:
            merged = dict(content or semantic)
            merged["semantic"] = True
        match = "both" if (content and name_matched) else "content" if content \
            else "semantic" if semantic else "name"
        scored.append((score, row, match, merged))
    scored.sort(key=lambda item: (-item[0], -item[1]["mtime_ns"]))
    window = scored[offset: offset + limit]
    return {
        "query": text,
        "total": len(scored),
        "items": [_to_item(row, match=match, content=content)
                  for _, row, match, content in window],
        "index_level": "L1",
        "content_hits": len(content_hits),
        "truncated": len(name_rows) >= MAX_CANDIDATES,
        "rewritten": rewritten,
        "vector_used": vector_used,
        "vector_model": vector_model,
        "vector_error": vector_error,
    }


def stats(conn: sqlite3.Connection) -> dict:
    availability = {
        row["availability"]: row["n"]
        for row in conn.execute(
            "SELECT availability, COUNT(*) AS n FROM asset_locations GROUP BY availability"
        )
    }
    by_ext = [
        {"ext": row["ext"], "count": row["n"]}
        for row in conn.execute(
            "SELECT ext, COUNT(*) AS n FROM asset_locations"
            " WHERE availability = 'AVAILABLE' GROUP BY ext ORDER BY n DESC LIMIT 8"
        )
    ]
    sources = []
    for row in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM asset_locations l"
        "  WHERE l.source_id = s.source_id AND l.availability = 'AVAILABLE') AS available,"
        " (SELECT COUNT(*) FROM asset_locations l"
        "  WHERE l.source_id = s.source_id AND l.availability = 'STALE') AS stale"
        " FROM sources s ORDER BY s.created_at"
    ):
        sources.append({
            "source_id": row["source_id"],
            "label": row["label"],
            "root_uri": row["root_uri"],
            "state": row["state"],
            "health": row["health"],
            "last_scan_at": row["last_scan_at"],
            "available": row["available"],
            "stale": row["stale"],
        })
    available = availability.get("AVAILABLE", 0)
    content = indexer.coverage(conn)
    return {
        "files": {
            "available": available,
            "stale": availability.get("STALE", 0),
            "missing": availability.get("MISSING", 0),
            "offline": availability.get("OFFLINE", 0),
        },
        "by_ext": by_ext,
        "sources": sources,
        "coverage": {
            "directory_level": "L0",
            "directory_indexed": available,
            "content_level": "L1",
            "content_indexed": content["indexed"],
            "content_pending": content["pending"],
            "content_empty": content["empty"],
            "content_failed": content["failed"],
            "content_unsupported": content["unsupported"],
            "low_quality": content["low_quality"],
            "ocr_indexed": content["ocr_indexed"],
            "ocr_empty": content["ocr_empty"],
            "ocr_failed": content["ocr_failed"],
            "ocr_pending": content["ocr_pending"],
            "mixed_pdf": content["mixed_pdf"],
            "ocr_policy": content["ocr_policy"],
            "ocr_available": content["ocr_available"],
            "ocr_engine": content["ocr_engine"],
            "ocr_languages": content["ocr_languages"],
            "ocr_message": content["ocr_message"],
        },
        "maintenance": maintenance.stats(conn),
    }
