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
_LATEST_QUERY = re.compile(
    r"(最新|当前版本|最新版|目前版本|latest|current version)", re.IGNORECASE)
_VERSION_HISTORY_QUERY = re.compile(
    r"(历史|旧版|以前|之前|早期|旧稿|哪些版本|各版本|版本历史|对比|差异|改了什么|变化)",
    re.IGNORECASE)
_VERSION_MENTION = re.compile(r"(?:v|版本\s*)(\d+(?:\.\d+)?)", re.IGNORECASE)
_VERSION_MENTION_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)\s*版本", re.IGNORECASE)
# 成稿 / 净稿 / 终稿通常比工作笔记更适合作为事实依据。
_AUTHORITY_PATH = re.compile(
    r"(净稿|定稿|终稿|clean|manuscript|final)", re.IGNORECASE)


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
                  source_id: str | None, ext: str | None,
                  allowed_assets: set[str] | None = None,
                  excluded_assets: set[str] | None = None,
                  path_index: dict | None = None) -> dict[str, dict]:
    hits: dict[str, dict] = {}
    maximal = query.maximal_terms(analysis)
    total = max(1, len(maximal))
    for hit in retrieval.find_chunks(
            conn, text, analysis=analysis, source_id=source_id, ext=ext,
            allowed_assets=allowed_assets, excluded_assets=excluded_assets,
            path_index=path_index):
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
        latest_ns = family.get("family_latest_mtime_ns") or 0
        item.update({
            "family_id": family["family_id"],
            "family_size": family["family_size"],
            "family_title": family["family_title"],
            "version_label": family["version_label"],
            # 没有版本标签的重复副本返回 None（未知），不能当作“最新版本”。
            "is_latest_version": family.get("is_latest_version"),
            "family_latest_asset_id": family.get("family_latest_asset_id"),
            "family_latest_version_label": family.get("family_latest_version_label"),
            "family_latest_mtime": (
                datetime.fromtimestamp(latest_ns / 1e9).astimezone()
                .isoformat(timespec="seconds") if latest_ns else None
            ),
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
           source_id: str | None = None, ext: str | None = None,
           allowed_assets: set[str] | None = None,
           excluded_assets: set[str] | None = None,
           priority_assets: set[str] | None = None) -> dict:
    text = (q or "").strip()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if not text:
        return {"query": text, "total": 0, "strong": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}
    if allowed_assets is not None and not allowed_assets:
        # 空项目/全部排除：不必再跑 FTS 与向量召回。
        return {"query": text, "total": 0, "strong": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}

    analysis = query.analyze(text, synonyms=query.synonym_map(conn))
    maximal = query.maximal_terms(analysis)
    # 编号/文件名式查询靠精确词面，语义只会引入近义噪声。
    lookup = _is_lookup(text)
    latest_intent = bool(_LATEST_QUERY.search(text))
    version_mentions = [
        match.group(1) for match in _VERSION_MENTION.finditer(text)
    ] + [
        match.group(1) for match in _VERSION_MENTION_SUFFIX.finditer(text)
    ]
    historical_intent = bool(_VERSION_HISTORY_QUERY.search(text))
    # 默认优先成稿的最新版本；明确问历史 / 指定版本时不这么做。
    prefer_latest = latest_intent or not (historical_intent or version_mentions)
    rewritten = analysis["core"] if analysis["core"] and analysis["core"] != text.lower() else None
    # 路径（文件夹名 + 文件名）和正文使用同一套查询概念，避免只搜正文漏掉目录信息。
    path_index = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)
    name_rows = _fetch_locations(conn, list(path_index))

    content_hits = _content_hits(conn, text, analysis, source_id=source_id, ext=ext,
                                 allowed_assets=allowed_assets,
                                 excluded_assets=excluded_assets,
                                 path_index=path_index)

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
                top_k=max(10, embed_settings["top_k"] * 4),
                allowed_assets=allowed_assets,
                excluded_assets=excluded_assets)
            vector_used = True
            vector_model = embed_settings["model"]
            for hit in hits:
                if allowed_assets is not None and hit["asset_id"] not in allowed_assets:
                    continue
                if excluded_assets and hit["asset_id"] in excluded_assets:
                    continue
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

    if allowed_assets is not None or excluded_assets:
        name_rows = {
            location_id: row for location_id, row in name_rows.items()
            if (allowed_assets is None or row["asset_id"] in allowed_assets)
            and (not excluded_assets or row["asset_id"] not in excluded_assets)
        }
        path_index = {location_id: match for location_id, match in path_index.items()
                      if location_id in name_rows}
        content_hits = {location_id: value for location_id, value
                        in content_hits.items() if location_id in name_rows}
        vector_hits = {location_id: value for location_id, value
                       in vector_hits.items() if location_id in name_rows}

    family_info = families.family_map(conn)
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
        if priority_assets and row["asset_id"] in priority_assets:
            # 项目主资料在「扩展检索」里优先于关联资料与全局资料。
            score += 120
        family = family_info.get(row["asset_id"])
        if family:
            if family.get("family_size", 1) > 1:
                # 同一份知识有多个版本时，成稿比零散工作笔记更可能是答案来源。
                score += 25
                label = (family.get("version_label") or "").strip()
                if version_mentions and any(
                        label == mention or label.endswith("." + mention)
                        for mention in version_mentions):
                    score += 300
            if prefer_latest and family.get("is_latest_version"):
                score += 150
                if family.get("family_size", 1) >= 5:
                    # 版本数量多的家族通常是持续维护的主文档。
                    score += 100
                if family.get("family_size", 1) >= 20:
                    score += 80
                if latest_intent:
                    score += 80
        if _AUTHORITY_PATH.search(row["path"] or ""):
            score += 55
        merged = content
        if semantic:
            merged = dict(content or semantic)
            merged["semantic"] = True
        match = "both" if (content and name_matched) else "content" if content \
            else "semantic" if semantic else "name"
        scored.append((score, row, match, merged))
    def _inject(targets: dict[str, dict]) -> None:
        """把漏召回的目标资产补进候选，已作为路径候选出现的则提分。"""
        index_by_asset: dict[str, list[int]] = {}
        for index, entry in enumerate(scored):
            index_by_asset.setdefault(entry[1]["asset_id"], []).append(index)
        for asset_id, info in targets.items():
            bonus = 700 + 10 * min(int(info.get("family_size", 1)), 30)
            for index in index_by_asset.get(asset_id, []):
                score, row, match, content = scored[index]
                if score < bonus:
                    scored[index] = (bonus, row, match, content)
        missing = [asset_id for asset_id in targets if asset_id not in index_by_asset]
        if not missing:
            return
        marks = ",".join("?" for _ in missing)
        location_ids = [
            row["location_id"] for row in conn.execute(
                f"SELECT MIN(location_id) AS location_id FROM asset_locations"
                f" WHERE asset_id IN ({marks}) AND availability = 'AVAILABLE'"
                " GROUP BY asset_id", missing)
        ]
        ext_filter = ext.lower().lstrip(".") if ext else None
        for row in _fetch_locations(conn, location_ids).values():
            if source_id and row["source_id"] != source_id:
                continue
            if ext_filter and (row["ext"] or "").lower() != ext_filter:
                continue
            info = targets.get(row["asset_id"]) or {}
            bonus = 700 + 10 * min(int(info.get("family_size", 1)), 30)
            scored.append((bonus, row, "name", None))

    if latest_intent and not version_mentions and family_info:
        # “最新版本是哪个”往往没有具体词面命中；直接补上各大家族的最新版本。
        latest_by_asset: dict[str, dict] = {}
        for asset_id, info in family_info.items():
            if allowed_assets is not None and asset_id not in allowed_assets:
                continue
            if excluded_assets and asset_id in excluded_assets:
                continue
            latest_id = info.get("family_latest_asset_id")
            if not latest_id or latest_id in latest_by_asset:
                continue
            # 没有版本标签的重复副本不算“最新版本”，不能补进结果。
            if info.get("family_size", 1) < 2 or not info.get("family_latest_version_label"):
                continue
            latest_by_asset[latest_id] = info
        ranked = sorted(
            latest_by_asset.items(),
            key=lambda item: (
                -item[1].get("family_size", 1),
                -(item[1].get("family_latest_mtime_ns") or 0),
            ),
        )[:5]
        _inject(dict(ranked))

    if version_mentions and family_info:
        # 明确点名版本时，词面候选容易被大量同名工作笔记挤出；
        # 直接补上该版本成员，并保留 is_latest_version=false 的历史标记。
        wanted = {
            mention.strip().lower().lstrip("v") for mention in version_mentions
        }
        targets: dict[str, dict] = {}
        for asset_id, info in family_info.items():
            if allowed_assets is not None and asset_id not in allowed_assets:
                continue
            if excluded_assets and asset_id in excluded_assets:
                continue
            label = (info.get("version_label") or "").strip().lower()
            if not label or info.get("family_size", 1) < 2:
                continue
            if any(label == mention or label.endswith("." + mention)
                   for mention in wanted):
                targets[asset_id] = info
        _inject(targets)
    scored.sort(key=lambda item: (-item[0], -item[1]["mtime_ns"]))
    if family_info:
        grouped: dict[str, list[tuple]] = {}
        passthrough: list[tuple] = []
        for entry in scored:
            info = family_info.get(entry[1]["asset_id"])
            if info is None:
                passthrough.append(entry)
            else:
                # 问“v0.12 到 v0.13 改了什么”时按版本分开，不能只留一个代表。
                label = (info.get("version_label") or "").strip()
                key = f"{info['family_id']}::{label}" if version_mentions else info["family_id"]
                grouped.setdefault(key, []).append(entry)
        aggregated = list(passthrough)
        for entries in grouped.values():
            entries.sort(key=lambda item: (-item[0], -item[1]["mtime_ns"]))
            representative = entries[0]
            if prefer_latest:
                latest = [
                    entry for entry in entries
                    if (family_info.get(entry[1]["asset_id"]) or {}).get(
                        "is_latest_version")
                ]
                # 最新版本分数接近时优先展示；明确问“最新”时无条件优先。
                if latest and (latest_intent or latest[0][0] >= representative[0] * 0.6):
                    representative = latest[0]
            # 代表项优先最新版本；家族全部版本仍可在 family_versions 里展开。
            family_id = (
                family_info.get(representative[1]["asset_id"]) or {}
            ).get("family_id")
            aggregated.append((*representative, family_id))
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
