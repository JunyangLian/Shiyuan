"""检索：文件名 / 路径（L0）+ 正文片段（L1/L2）混合召回。

正文召回统一交给 retrieval.py：查询先经过 query.analyze（去疑问词、n-gram、同义词），
再按概念命中评分；短于 3 个字符的关键词退化为主键扫描。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import embeddings, families, indexer, ingest, maintenance, query, retrieval

MAX_CANDIDATES = 5000
SNIPPET_WIDTH = 120


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
            "concepts": hit.concepts,
            "variants": hit.variants,
        }
    return hits


def _to_item(row: sqlite3.Row, *, match: str, content: dict | None,
             family: dict | None = None, matched_folder: str | None = None) -> dict:
    path = row["path"]
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    directory = path[: len(path) - len(name)].rstrip("\\/")
    item = {
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
        "matched_folder": matched_folder,
    }
    if family:
        item.update({
            "family_id": family["family_id"],
            "family_size": family["family_size"],
            "family_title": family["family_title"],
            "version_label": family["version_label"],
        })
    return item


def _fetch_locations(conn: sqlite3.Connection, location_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not location_ids:
        return {}
    rows: dict[str, sqlite3.Row] = {}
    for start in range(0, len(location_ids), 500):
        batch = location_ids[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT l.*, s.label AS source_label FROM asset_locations l"
            f" JOIN sources s ON s.source_id = l.source_id"
            f" WHERE l.location_id IN ({placeholders})",
            batch,
        ):
            rows[row["location_id"]] = row
    return rows


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
    # 路径（文件夹名 + 文件名）和正文使用同一套查询概念，避免只搜正文漏掉目录信息。
    path_index = retrieval.path_matches(conn, analysis, source_id=source_id, ext=ext)
    name_rows = _fetch_locations(conn, list(path_index))

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
        path_match = path_index.get(location_id)
        score = path_match.score if path_match else 0
        content = content_hits.get(location_id)
        semantic = vector_hits.get(location_id)
        name_matched = path_match is not None
        if content:
            # 正文候选必须保留 retrieval 的概念/变体命中数，否则同分后会被
            # mtime 排序淹没（例如唯一编号被近期文件挤出前 5）。
            score += 150
            score += 30 * int(content.get("concepts", 0))
            score += 4 * int(content.get("variants", 0))
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
    family_info = families.family_map(conn)
    if family_info:
        grouped: dict[str, list[tuple]] = {}
        passthrough: list[tuple] = []
        for entry in scored:
            info = family_info.get(entry[1]["asset_id"])
            if info is None:
                passthrough.append(entry)
            else:
                grouped.setdefault(info["family_id"], []).append(entry)
        aggregated = list(passthrough)
        for family_id, entries in grouped.items():
            entries.sort(key=lambda item: (-item[0], -item[1]["mtime_ns"]))
            # 代表项只是本次匹配度最高的一条，不代表“推荐版本”。
            aggregated.append((*entries[0], family_id))
        scored = sorted(
            aggregated, key=lambda item: (-item[0], -item[1]["mtime_ns"]))
    window = scored[offset: offset + limit]
    items = []
    for entry in window:
        _, row, match, content = entry[:4]
        family = family_info.get(row["asset_id"])
        path_match = path_index.get(row["location_id"])
        item = _to_item(
            row, match=match, content=content, family=family,
            matched_folder=path_match.folder if path_match else None,
        )
        if len(entry) == 5:
            item["family_versions"] = families.family_members(conn, entry[4])
        items.append(item)
    return {
        "query": text,
        "total": len(scored),
        "items": items,
        "index_level": "L1",
        "content_hits": len(content_hits),
        "truncated": len(path_index) >= MAX_CANDIDATES,
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
            "l0_completed_at": row["l0_completed_at"],
            "l1_completed_at": row["l1_completed_at"],
            "l2_completed_at": row["l2_completed_at"],
            "full_coverage_at": row["full_coverage_at"],
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
            "content_locked": content["locked"],
            "content_unsupported": content["unsupported"],
            "low_quality": content["low_quality"],
            "ocr_indexed": content["ocr_indexed"],
            "ocr_empty": content["ocr_empty"],
            "ocr_failed": content["ocr_failed"],
            "ocr_locked": content["ocr_locked"],
            "ocr_pending": content["ocr_pending"],
            "mixed_pdf": content["mixed_pdf"],
            "ocr_policy": content["ocr_policy"],
            "ocr_available": content["ocr_available"],
            "ocr_engine": content["ocr_engine"],
            "ocr_languages": content["ocr_languages"],
            "ocr_message": content["ocr_message"],
        },
        "maintenance": maintenance.stats(conn),
        "ingest": ingest.public_settings(conn),
        "full_coverage": indexer.full_coverage(conn),
    }
