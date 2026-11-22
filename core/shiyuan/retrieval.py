"""统一召回层：查询理解后从 chunk_fts / chunks 取候选，并按概念命中评分。

search 与 answer 共用这一层，保证"搜索"和"问答依据"看到的是同一批证据。
向量召回在 embeddings.py 中实现，由上层按 RRF 与这里的结果合并。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import families, query

CANDIDATE_LIMIT = 600
PATH_MATCH_LIMIT = 20000
PATH_ONLY_LIMIT = 8

# 这些词出现在文件夹/文件名里太常见，单独命中不足以构成证据。
_GENERIC_PATH_TERMS = {
    "实验", "数据", "记录", "报告", "总结", "笔记", "论文", "文档", "结果",
    "处理", "文件", "资料", "表格", "分析", "研究", "内容", "项目", "工作",
    "附件", "备份", "副本", "新建", "未命名", "图片", "照片",
}

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
    family_id: str | None = None
    family_size: int = 1
    family_title: str = ""
    version_label: str = ""
    sources: list[dict] = field(default_factory=list)
    path_score: int = 0

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


@dataclass
class PathMatch:
    location_id: str
    asset_id: str
    path: str
    score: int
    concepts: int
    variants: int
    name_matched: bool
    folder: str | None
    specific: bool


def _path_score(path: str, analysis: dict) -> tuple[int, int, int, bool, str | None, bool]:
    """路径匹配评分：文件夹名与文件名都参与，越深的文件夹命中越具体。"""
    lowered = (path or "").lower()
    parts = [part for part in re.split(r"[\\/]+", lowered) if part]
    if not parts:
        return 0, 0, 0, False, None, False
    name = parts[-1]
    folders = parts[:-1]

    score = 0
    matched_concepts = 0
    matched_variants = 0
    name_matched = False
    specific = False
    best_folder: str | None = None
    best_folder_score = 0.0

    for concept in analysis["concepts"]:
        concept_hit = False
        for variant in concept["variants"]:
            if len(variant) < 2:
                continue
            if variant in name:
                score += 12 + 2 * len(variant)
                matched_variants += 1
                concept_hit = True
                name_matched = True
                if variant not in _GENERIC_PATH_TERMS:
                    specific = True
            for depth, folder in enumerate(folders):
                if variant not in folder:
                    continue
                weight = (4 + 2 * len(variant)) * (1 + min(depth, 4) / 4)
                score += int(weight)
                matched_variants += 1
                concept_hit = True
                if variant not in _GENERIC_PATH_TERMS:
                    specific = True
                if weight > best_folder_score:
                    best_folder_score = weight
                    best_folder = folder
        if concept_hit:
            matched_concepts += 1

    core = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (analysis.get("core") or "").lower())
    if len(core) >= 2:
        if core == name:
            score += 1000
        elif core in name:
            score += 300
        elif core in lowered:
            score += 120
    return score, matched_concepts, matched_variants, name_matched, best_folder, specific


def path_matches(conn: sqlite3.Connection, analysis: dict, *,
                 source_id: str | None = None, ext: str | None = None,
                 limit: int = PATH_MATCH_LIMIT) -> dict[str, PathMatch]:
    """按完整路径召回：文件夹名 + 文件名 + 正文之外的位置信息。"""
    variants = [variant for variant in analysis["variants"] if len(variant) >= 2][:48]
    if not variants:
        return {}

    clauses = ["s.state = 'active'", "l.availability = 'AVAILABLE'"]
    params: list[object] = []
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    clauses.append(
        "(" + " OR ".join("instr(lower(l.path), ?) > 0" for _ in variants) + ")"
    )
    params.extend(variants)
    rows = conn.execute(
        "SELECT l.location_id, l.asset_id, l.path FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE " + " AND ".join(clauses) + " LIMIT ?",
        [*params, max(1, min(limit, PATH_MATCH_LIMIT))],
    ).fetchall()

    matches: dict[str, PathMatch] = {}
    for row in rows:
        score, concepts, matched_variants, name_matched, folder, specific = _path_score(
            row["path"], analysis)
        if score <= 0:
            continue
        matches[row["location_id"]] = PathMatch(
            location_id=row["location_id"],
            asset_id=row["asset_id"],
            path=row["path"],
            score=score,
            concepts=concepts,
            variants=matched_variants,
            name_matched=name_matched,
            folder=folder,
            specific=specific,
        )
    return matches


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
    path_index = path_matches(conn, analysis, source_id=source_id, ext=ext)

    hits: list[ChunkHit] = []
    for row in rows:
        text_value = row["text"] or ""
        concepts, matched = query.rank(text_value, analysis)
        path_match = path_index.get(row["location_id"])
        path_ok = bool(path_match and (
            path_match.concepts >= threshold
            or (path_match.name_matched and path_match.specific)
        ))
        if concepts < threshold and not path_ok:
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
            concepts=concepts + (path_match.concepts if path_match else 0),
            variants=matched + (path_match.variants if path_match else 0),
            version_id=row["version_id"] or "",
            mtime_ns=row["mtime_ns"] or 0,
            bbox=_parse_bbox(row["bbox"]),
            path_score=path_match.score if path_match else 0,
        ))

    seen = {hit.location_id for hit in hits}
    path_only = sorted(
        (match for location_id, match in path_index.items() if location_id not in seen),
        key=lambda match: -match.score,
    )
    added = 0
    for match in path_only:
        if added >= PATH_ONLY_LIMIT:
            break
        if match.concepts < threshold and not (
                match.name_matched and match.specific):
            continue
        row = conn.execute(
            "SELECT c.chunk_id, c.version_id, c.text, c.page, c.sheet, c.bbox,"
            " l.mtime_ns, a.parser_signature"
            " FROM chunks c"
            " JOIN asset_locations l ON l.location_id = ?"
            " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
            " WHERE c.asset_id = ? ORDER BY c.ordinal LIMIT 1",
            (match.location_id, match.asset_id),
        ).fetchone()
        if row is None or not (row["text"] or "").strip():
            continue
        hits.append(ChunkHit(
            chunk_id=row["chunk_id"],
            asset_id=match.asset_id,
            location_id=match.location_id,
            path=match.path,
            text=row["text"],
            page=row["page"],
            sheet=row["sheet"],
            parser_signature=row["parser_signature"] or "",
            concepts=match.concepts,
            variants=match.variants,
            version_id=row["version_id"] or "",
            mtime_ns=row["mtime_ns"] or 0,
            bbox=_parse_bbox(row["bbox"]),
            path_score=match.score,
        ))
        added += 1

    hits.sort(key=lambda hit: (
        -(hit.concepts * 10 + min(hit.path_score, 300)),
        -hit.variants,
        -len(hit.text),
    ))
    # 重复段落合并为一条并保留全部来源；各版本独有段落继续保留。
    return families.dedupe_chunk_hits(conn, hits)
