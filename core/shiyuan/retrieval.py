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
    "课程", "科目", "作业", "考试", "试卷", "课件", "讲义", "习题", "章节",
}


def is_generic_term(term: str) -> bool:
    return term in _GENERIC_PATH_TERMS


# 说明类文档（README / 使用说明）在「怎么用」问题里更相关；
# 论文模板/撰写规范这类文件在其他问题里容易挤占正文文档。
_DOC_NAME = re.compile(
    r"(?:^|[\\/_\-\s.])(?:readme|readme\.md|quickstart|usage|guide|manual|docs|documentation)"
    r"(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)
_DOC_HINT = re.compile(
    r"(使用说明|操作说明|快速开始|快速上手|使用手册|操作手册|使用指南|上手教程|入门教程)")
_TEMPLATE_NAME = re.compile(r"(模板|规范|封面|样表|格式要求|撰写)")
_FORMAT_QUERY = re.compile(r"(模板|规范|格式|封面|排版|撰写)")
# 查重/比对报告与正文主题高度重合，但通常不是用户要找的内容。
_DUP_NAME = re.compile(
    r"(查重|重复率|相似度检测|比对报告|对照报告|片段对照|原文对照|全文对照|简洁报告"
    r"|去除引用|报告单|检测结果|格式分析|AIGC|检测报告|降重)")
_DUP_QUERY = re.compile(r"(查重|重复率|相似度|降重|比对|对照|检测报告|格式分析)")


def name_prior(path: str, analysis: dict) -> int:
    name = (path or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
    score = 0
    if (analysis.get("howto") or analysis.get("intro")) and (
            _DOC_NAME.search(name) or _DOC_HINT.search(name)):
        score += 240
    if (not _FORMAT_QUERY.search(analysis.get("normalized") or "")
            and _TEMPLATE_NAME.search(name)):
        score -= 200
    if (not _DUP_QUERY.search(analysis.get("normalized") or "")
            and _DUP_NAME.search(name)):
        score -= 260
    return score

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
    focus: int = 0
    maximal: int = 0

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
    folder_path: str | None = None
    folder_specific: bool = False
    specific_folder: str | None = None
    specific_folder_path: str | None = None


def _relative_path(path: str, root: str | None) -> str:
    """去掉数据源根目录，避免根目录名把所有文件都算成路径命中。"""
    root = (root or "").rstrip("\\/")
    if not root:
        return path or ""
    for separator in ("\\", "/"):
        prefix = (root + separator).lower()
        if (path or "").lower().startswith(prefix):
            return path[len(root) + 1:]
    return path or ""


def _path_score(path: str, analysis: dict) -> tuple[int, int, int, bool, str | None, int, bool, bool, str | None, int]:
    """路径匹配评分：文件夹名与文件名都参与，越深的文件夹命中越具体。

    path 应当是数据源内的相对路径；根目录名不参与打分。
    """
    lowered = (path or "").lower()
    parts = [part for part in re.split(r"[\\/]+", lowered) if part]
    if not parts:
        return 0, 0, 0, False, None, -1, False, False, None, -1
    name = parts[-1]
    folders = parts[:-1]

    score = 0
    matched_concepts = 0
    matched_variants = 0
    name_matched = False
    specific = False
    folder_specific = False
    best_folder: str | None = None
    best_folder_index = -1
    best_folder_score = 0.0
    specific_folder: str | None = None
    specific_folder_index = -1
    specific_folder_score = 0.0

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
                    folder_specific = True
                    if weight > specific_folder_score:
                        specific_folder_score = weight
                        specific_folder = folder
                        specific_folder_index = depth
                if weight > best_folder_score:
                    best_folder_score = weight
                    best_folder = folder
                    best_folder_index = depth
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
    # 「专利的创新点」这类问法，主体词出现在文件名里应该更靠前。
    for part in re.split(r"[的\s]+", core):
        if len(part) >= 2 and part != core and part in name:
            score += 60
    return (score, matched_concepts, matched_variants, name_matched,
            best_folder, best_folder_index, specific, folder_specific,
            specific_folder, specific_folder_index)


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
        "SELECT l.location_id, l.asset_id, l.path, s.root_uri FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE " + " AND ".join(clauses) + " LIMIT ?",
        [*params, max(1, min(limit, PATH_MATCH_LIMIT))],
    ).fetchall()

    matches: dict[str, PathMatch] = {}
    for row in rows:
        root = row["root_uri"] or ""
        relative = _relative_path(row["path"], root)
        (score, concepts, matched_variants, name_matched,
         folder, folder_index, specific, folder_specific,
         specific_folder, specific_folder_index) = _path_score(relative, analysis)
        if score <= 0:
            continue
        parts = [part for part in re.split(r"[\\/]+", relative) if part]

        def folder_abs(index: int) -> str | None:
            if index < 0:
                return None
            return root.rstrip("\\/") + "\\" + "\\".join(parts[:index + 1])

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
            folder_path=folder_abs(folder_index),
            folder_specific=folder_specific,
            specific_folder=specific_folder,
            specific_folder_path=folder_abs(specific_folder_index),
        )
    return matches


def find_chunks(conn: sqlite3.Connection, text: str, *, analysis: dict | None = None,
                source_id: str | None = None, ext: str | None = None,
                limit: int = CANDIDATE_LIMIT,
                focus: list[str] | None = None,
                path_only: bool = False) -> list[ChunkHit]:
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
        order = ""
        if analysis.get("howto") or analysis.get("intro"):
            # 说明类文档的候选可能被大量数据文件挤出 600 条上限，优先保留它们。
            order = (" ORDER BY (instr(lower(l.path), 'readme') > 0"
                     " OR instr(lower(l.path), 'quickstart') > 0) DESC")
        sql = (
            f"{_SELECT}{_JOINS_NO_FTS} WHERE "
            + " AND ".join(where + [f"({clauses})"]) + f"{order} LIMIT {limit}"
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
    raw_required = 1 if len(analysis["concepts"]) <= 2 else 2
    maximal = query.maximal_terms(analysis)
    path_index = path_matches(conn, analysis, source_id=source_id, ext=ext)

    hits: list[ChunkHit] = []
    for row in rows:
        text_value = row["text"] or ""
        concepts, matched = query.rank(text_value, analysis)
        path_match = path_index.get(row["location_id"])
        if path_only and path_match is not None and not (
                path_match.specific or path_match.folder_specific):
            path_match = None
        if path_only and path_match is None:
            continue
        path_ok = bool(path_match and (
            path_match.concepts >= threshold
            or (path_match.name_matched and path_match.specific)
        ))
        lowered = text_value.lower()
        matched_terms = [
            concept["term"] for concept in maximal
            if any(variant in lowered for variant in concept["variants"])
        ]
        maximal_hit = len(matched_terms)
        if not path_ok and maximal_hit < 1:
            # 只有 gram 命中时：说明类问题允许命中一个词，其余问题要求至少两个。
            if analysis.get("howto"):
                if concepts < 1:
                    continue
            elif concepts < raw_required:
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
            maximal=maximal_hit,
        ))

    seen = {hit.location_id for hit in hits}
    extra_matches = sorted(
        (match for location_id, match in path_index.items() if location_id not in seen),
        key=lambda match: -(match.score + name_prior(match.path, analysis)),
    )
    added = 0
    for match in extra_matches:
        if added >= PATH_ONLY_LIMIT:
            break
        if path_only and not (match.specific or match.folder_specific):
            continue
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

    for hit in hits:
        hit.path_score += name_prior(hit.path, analysis)
        if focus:
            lowered = hit.text.lower()
            hit.focus = min(3, sum(1 for term in focus
                                   if len(term) >= 2 and term in lowered))

    hits.sort(key=lambda hit: (
        -(hit.focus * 120 + hit.maximal * 10 + min(hit.path_score, 300)),
        -hit.concepts,
        -hit.variants,
        -len(hit.text),
    ))
    # 重复段落合并为一条并保留全部来源；各版本独有段落继续保留。
    return families.dedupe_chunk_hits(conn, hits)