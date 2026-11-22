"""统一召回层：查询理解后从 chunk_fts / chunks 取候选，并按概念命中评分。

search 与 answer 共用这一层，保证"搜索"和"问答依据"看到的是同一批证据。
向量召回在 embeddings.py 中实现，由上层按 RRF 与这里的结果合并。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import query

CANDIDATE_LIMIT = 600
NAME_LOCATION_LIMIT = 8

_SELECT = (
    "SELECT c.chunk_id, c.asset_id, c.version_id, c.text, c.page, c.sheet, c.bbox,"
    " l.location_id, l.path, l.mtime_ns, a.parser_signature"
)
_JOINS = (
    " JOIN chunks c ON c.rowid = chunk_fts.rowid"
    " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
    " JOIN asset_locations l ON l.asset_id = c.asset_id"
    " JOIN sources s ON s.source_id = l.source_id"
)
_JOINS_NO_FTS = (
    " FROM chunks c"
    " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
    " JOIN asset_locations l ON l.asset_id = c.asset_id"
    " JOIN sources s ON s.source_id = l.source_id"
)


@dataclass
class ChunkHit:
    chunk_id: str
    asset_id: str
    location_id: str
    path: str
    text: str
    page: int | None
    sheet: str | None
    parser_signature: str
    concepts: int
    variants: int
    version_id: str = ""
    mtime_ns: int = 0
    bbox: list[list[int]] | None = None

    @property
    def ocr(self) -> bool:
        return (self.parser_signature or "").startswith("l2.")


def _parse_bbox(raw: str | None) -> list[list[int]] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) and value else None


def _name_meta(conn: sqlite3.Connection, analysis: dict, *,
               source_id: str | None, ext: str | None) -> dict[str, tuple[int, int]]:
    """文件名/路径命中的概念数，用于给正文候选加分。

    只在命中文件较少时启用；命中过多（如"报告"）时返回空，避免把无关文件
    拉进问答证据。问答仍然要求正文至少命中 1 个概念，不会仅凭文件名作答。
    """
    terms: list[str] = []
    for concept in analysis["concepts"]:
        for variant in concept["variants"]:
            if len(variant) >= 2 and variant not in terms:
                terms.append(variant)
    if not terms:
        return {}
    terms = terms[:48]

    clauses = ["s.state = 'active'", "l.availability = 'AVAILABLE'",
               "(" + " OR ".join("instr(lower(l.path), ?) > 0" for _ in terms) + ")"]
    params: list[object] = list(terms)
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    sql = (
        "SELECT l.location_id, l.path FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE " + " AND ".join(clauses) + " ORDER BY l.path LIMIT 200"
    )
    meta: dict[str, tuple[int, int]] = {}
    for row in conn.execute(sql, params):
        concepts, variants = query.rank(row["path"], analysis)
        if concepts:
            meta[row["location_id"]] = (concepts, variants)
        if len(meta) > NAME_LOCATION_LIMIT:
            return {}
    return meta


def find_chunks(conn: sqlite3.Connection, text: str, *, analysis: dict | None = None,
                source_id: str | None = None, ext: str | None = None,
                limit: int = CANDIDATE_LIMIT) -> list[ChunkHit]:
    analysis = analysis or query.analyze(text)
    variants: list[str] = analysis["variants"]
    if not variants:
        return []

    where = ["s.state = 'active'", "l.availability = 'AVAILABLE'"]
    params: list[object] = []
    if source_id:
        where.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        where.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))

    fts_units = [variant for variant in variants if len(variant) >= 3]
    short_units = [variant for variant in variants if len(variant) < 3]
    rows: list[sqlite3.Row] = []
    if fts_units:
        match = " OR ".join('"' + unit.replace('"', '""') + '"' for unit in fts_units)
        sql = (
            f"{_SELECT} FROM chunk_fts{_JOINS} WHERE chunk_fts MATCH ? AND "
            + " AND ".join(where) + f" LIMIT {limit}"
        )
        rows = conn.execute(sql, [match, *params]).fetchall()
    if short_units:
        # FTS5 trigram 只索引 ≥3 字符的子串；两字词（如“物种”）需要 LIKE 兜底，
        # 否则同时含长短变体的问题会漏掉短词。
        clauses = " OR ".join("instr(lower(c.text), ?) > 0" for _ in short_units)
        sql = (
            f"{_SELECT}{_JOINS_NO_FTS} WHERE "
            + " AND ".join(where + [f"({clauses})"]) + f" LIMIT {limit}"
        )
        extra = conn.execute(sql, [*params, *short_units]).fetchall()
        if not fts_units:
            rows = extra
        else:
            merged = {(row["chunk_id"], row["location_id"]): row for row in rows}
            for row in extra:
                merged.setdefault((row["chunk_id"], row["location_id"]), row)
            rows = list(merged.values())

    threshold = query.minimum_concepts(analysis)
    name_meta = _name_meta(conn, analysis, source_id=source_id, ext=ext)

    hits: list[ChunkHit] = []
    for row in rows:
        text_value = row["text"] or ""
        concepts, matched = query.rank(text_value, analysis)
        path_concepts, path_variants = name_meta.get(row["location_id"], (0, 0))
        if concepts < threshold and not (concepts >= 1 and path_concepts >= 1):
            continue
        hits.append(ChunkHit(
            chunk_id=row["chunk_id"],
            asset_id=row["asset_id"],
            location_id=row["location_id"],
            path=row["path"],
            text=text_value,
            page=row["page"],
            sheet=row["sheet"],
            parser_signature=row["parser_signature"] or "",
            concepts=concepts + path_concepts,
            variants=matched + path_variants,
            version_id=row["version_id"] or "",
            mtime_ns=row["mtime_ns"] or 0,
            bbox=_parse_bbox(row["bbox"]),
        ))
    hits.sort(key=lambda hit: (-hit.concepts, -hit.variants, -len(hit.text)))
    return hits
