"""检索：文件名 / 路径（L0）+ 正文片段（L1/L2）混合召回。

正文召回统一交给 retrieval.py：查询先经过 query.analyze（去疑问词、n-gram、同义词），
再按概念命中评分；短于 3 个字符的关键词退化为主键扫描。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import embeddings, families, indexer, ingest, maintenance, query, retrieval

MAX_CANDIDATES = 5000
SNIPPET_WIDTH = 120

# 混合排序权重：词面证据按“查询概念覆盖率”给分，语义证据按本次查询的 z 分数给分。
# 原始相似度跨查询不可比，固定加分会让弱语义命中混进结果。
COVERAGE_WEIGHT = 90
SEMANTIC_BASE = 60
SEMANTIC_Z_WEIGHT = 60
SEMANTIC_Z_CAP = 4.0
STRONG_RATIO = 0.5
CODE_EXTS = {
    "py", "pl", "sh", "bat", "ps1", "js", "ts", "tsx", "jsx", "r", "java",
    "c", "cc", "cpp", "h", "hpp", "go", "rs", "ipynb",
}
_CODE_QUERY = re.compile(
    r"(脚本|代码|程序|函数|源码|命令行|code|script|function|notebook|ipynb)",
    re.IGNORECASE,
)


def _is_lookup(text: str) -> bool:
    """编号、错误码、文件名式查询：词面精确匹配比语义更可靠。"""
    tokens = re.findall(r"[a-z0-9][a-z0-9_.\-]*", (text or "").lower())
    if not tokens:
        return False
    return all(
        any(ch.isdigit() for ch in token)
        and (any(ch.isalpha() for ch in token) or len(token) >= 4)
        for token in tokens
    )


def _compact(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", (value or "").lower())


def _coverage(text: str, maximal: list[dict]) -> float:
    if not maximal:
        return 0.0
    lowered = (text or "").lower()
    matched = sum(
        1 for concept in maximal
        if any(variant in lowered for variant in concept["variants"]))
    return matched / len(maximal)


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
    maximal = query.maximal_terms(analysis)
    total = max(1, len(maximal))
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
            "coverage": hit.maximal / total,
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
        return {"query": text, "total": 0, "strong": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}

    analysis = query.analyze(text, synonyms=query.synonym_map(conn))
    maximal = query.maximal_terms(analysis)
    # 编号/文件名式查询靠精确词面，语义只会引入近义噪声。
    lookup = _is_lookup(text)
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
    if embeddings.enabled(embed_settings) and not lookup:
        try:
            # 向量模型对自然语言整句更敏感；去掉疑问词后的关键词串反而会丢语境。
            hits = embeddings.semantic_chunks(
                conn, text, source_id=source_id, ext=ext,
                top_k=max(10, embed_settings["top_k"] * 4))
            vector_used = True
            vector_model = embed_settings["model"]
            for hit in hits:
                current = vector_hits.get(hit["location_id"])
                if current and current["z"] >= hit.get("z", 0.0):
                    continue
                vector_hits[hit["location_id"]] = {
                    "snippet": _snippet(hit["text"], analysis["variants"]),
                    "page": hit["page"],
                    "sheet": hit["sheet"],
                    "level": "L2" if hit["ocr"] else "L1",
                    "semantic": True,
                    "similarity": hit["similarity"],
                    "z": hit.get("z", 0.0),
                    "coverage": _coverage(hit["text"], maximal),
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
        score += retrieval.name_prior(row["path"], analysis)
        if (path_match is not None and path_match.name_matched
                and path_match.specific and path_match.name_strength >= 3
                and path_match.concepts >= 2):
            # 文件名命中主题词时补分，避免长文档靠正文 gram 数量压过真正的主角文件。
            # 只命中 2 字 gram（如「染色」之于「染色体」）不算，否则染色体论文会抢走
            # 「性染色体」的查询；命中的概念越多，越像“就是这份文件”。
            score += 200 + 80 * min(int(path_match.concepts), 4)
        if (not _CODE_QUERY.search(text)
                and (row["ext"] or "").lower() in CODE_EXTS):
            # 用户没在找代码时，脚本/源码不应压过同名的数据或文档。
            score -= 140
        content = content_hits.get(location_id)
        semantic = vector_hits.get(location_id)
        name_matched = path_match is not None
        if content:
            # 正文候选必须保留 retrieval 的概念/变体命中数，否则同分后会被
            # mtime 排序淹没（例如唯一编号被近期文件挤出前 5）。
            score += 150
            score += 30 * int(content.get("concepts", 0))
            score += 4 * int(content.get("variants", 0))
            # 覆盖率奖励“问题里的概念被答全了”，而不是某个词重复很多次。
            coverage = float(content.get("coverage") or 0.0)
            score += int(COVERAGE_WEIGHT * coverage ** 1.5)
        if semantic:
            z_score = max(0.0, min(float(semantic.get("z") or 0.0), SEMANTIC_Z_CAP))
            score += SEMANTIC_BASE + int(SEMANTIC_Z_WEIGHT * z_score)
        if lookup:
            name = row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            core = _compact(analysis["core"])
            if core and core in _compact(name):
                score += 400
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
    # 与最高分差距过大的结果只是“沾边”，单独标记，前端/外部调用可以折叠。
    strong = len(scored)
    if scored and scored[0][0] > 0:
        top = scored[0][0]
        strong = sum(1 for entry in scored if entry[0] >= top * STRONG_RATIO)
    window = scored[offset: offset + limit]
    items = []
    for index, entry in enumerate(window):
        _, row, match, content = entry[:4]
        family = family_info.get(row["asset_id"])
        path_match = path_index.get(row["location_id"])
        item = _to_item(
            row, match=match, content=content, family=family,
            matched_folder=path_match.folder if path_match else None,
        )
        item["weak"] = offset + index >= strong
        if len(entry) == 5:
            item["family_versions"] = families.family_members(conn, entry[4])
        items.append(item)
    return {
        "query": text,
        "total": len(scored),
        "strong": strong,
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
