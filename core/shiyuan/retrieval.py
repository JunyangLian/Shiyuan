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
    "整理", "系统", "企业",
    "课程", "科目", "作业", "考试", "试卷", "课件", "讲义", "习题", "章节",
    # 英文通用词：工具/教程/工程文件里极常见，仅命中这些不应压过语义命中。
    "mapping", "development", "configuration", "config", "tutorial", "tutorials",
    "sample", "samples", "index", "page", "pages", "content", "extension",
    "project", "main", "test", "tests", "docs", "doc", "readme", "license",
    "changelog", "build", "src", "lib", "assets", "static", "output", "out",
    "sex", "chromosome", "exception", "error", "document", "documents",
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
# 试卷/题库/复习资料是资料库里最大的噪声源之一：除非问题本身在找考试材料，
# 否则不应压过正文文档（「共线性」被「线性代数试卷」抢走就是典型）。
_EXAM_NAME = re.compile(
    r"(试卷|试题|参考答案|评分标准|题库|考集|期末|期中|真题|模拟题|习题集"
    r"|选择题|问答题|考试题|答案|[A-Da-d]卷)")
_EXAM_QUERY = re.compile(r"(试卷|试题|答案|题库|复习|期末|期中|真题|考试题|习题|模拟题)")
# 问“结果/报告/汇总”时，处理报告、汇总表应该排在原始图片和脚本之前。
_RESULT_NAME = re.compile(r"(报告|汇总|统计|结果|明细|report|summary|result)", re.IGNORECASE)
_RESULT_QUERY = re.compile(r"(结果|报告|汇总|统计|概况|处理结果|明细|多少|数量)")
# 问“支持哪些输入格式”时，扫描/读取模块比命令行入口更有回答价值。
_INPUT_QUERY = re.compile(r"(输入格式|支持格式|支持.{0,4}格式|输入文件|input format|supported)",
                          re.IGNORECASE)
_INPUT_NAME = re.compile(r"(scan|reader|loader|ingest|input|format)", re.IGNORECASE)


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
    if (not _EXAM_QUERY.search(analysis.get("normalized") or "")
            and _EXAM_NAME.search(name)):
        score -= 300
    if (_RESULT_QUERY.search(analysis.get("normalized") or "")
            and _RESULT_NAME.search(name)):
        score += 200
    if (_INPUT_QUERY.search(analysis.get("normalized") or "")
            and _INPUT_NAME.search(name)):
        score += 160
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
    # 文件名里命中的最长关键词长度：2 字 gram 太容易巧合（「染色」之于「染色体」），
    # 不足以说明这个文件就是问题的主体。
    name_strength: int = 0


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


def _term_in(value: str, variant: str) -> bool:
    """路径里的词面匹配：两字母缩写只接受独立成词（fe 不应命中 different）。"""
    if len(variant) == 2 and variant.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])",
                         value) is not None
    return variant in value


def _path_score(path: str, analysis: dict) -> tuple[int, int, int, bool, str | None, int, bool, bool, str | None, int, int]:
    """路径匹配评分：文件夹名与文件名都参与，越深的文件夹命中越具体。

    path 应当是数据源内的相对路径；根目录名不参与打分。
    """
    lowered = (path or "").lower()
    parts = [part for part in re.split(r"[\\/]+", lowered) if part]
    if not parts:
        return 0, 0, 0, False, None, -1, False, False, None, -1, 0
    # 文件名里的下划线/连字符按空格处理：「cover_letter」要能被「cover letter」命中。
    keys = [re.sub(r"[_\-]+", " ", part) for part in parts]
    name = keys[-1]
    folders = keys[:-1]
    display_folders = parts[:-1]
    lowered_key = re.sub(r"[_\-]+", " ", lowered)

    score = 0
    matched_concepts = 0
    matched_variants = 0
    name_matched = False
    name_strength = 0
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
            # 与路径同样把下划线/连字符当成空格，否则「pilot_batches」匹配不上自己的目录。
            probe = re.sub(r"[_\-]+", " ", variant)
            if _term_in(name, probe):
                score += 12 + 2 * len(variant)
                matched_variants += 1
                concept_hit = True
                name_matched = True
                if variant not in _GENERIC_PATH_TERMS:
                    specific = True
                    name_strength = max(name_strength, len(variant))
            for depth, folder in enumerate(folders):
                if not _term_in(folder, probe):
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
                        specific_folder = display_folders[depth]
                        specific_folder_index = depth
                if weight > best_folder_score:
                    best_folder_score = weight
                    best_folder = display_folders[depth]
                    best_folder_index = depth
        if concept_hit:
            matched_concepts += 1

    # 多词问题（如「FE RE」）不能把空格去掉拼成 fere 再去匹配 different；
    # 整句核心词奖励只对单个词/短语生效。
    core_text = (analysis.get("core") or "").strip().lower()
    core_key = re.sub(r"[_\-]+", " ", core_text)
    core = "" if re.search(r"\s", core_key) else re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "", core_key)
    if len(core) >= 2:
        if core == name:
            score += 1000
        elif _term_in(name, core):
            score += 300
        elif _term_in(lowered_key, core_key):
            score += 120
    # 「专利的创新点」这类问法，主体词出现在文件名里应该更靠前。
    for part in re.split(r"[的\s]+", core):
        if len(part) >= 2 and part != core and _term_in(name, part):
            score += 60
    return (score, matched_concepts, matched_variants, name_matched,
            best_folder, best_folder_index, specific, folder_specific,
            specific_folder, specific_folder_index, name_strength)


def path_matches(conn: sqlite3.Connection, analysis: dict, *,
                 source_id: str | None = None, ext: str | None = None,
                 limit: int = PATH_MATCH_LIMIT) -> dict[str, PathMatch]:
    """按完整路径召回：文件夹名 + 文件名 + 正文之外的位置信息。"""
    variants = [variant for variant in analysis["variants"] if len(variant) >= 2][:48]
    if not variants:
        return {}
    # 词表里的「cover letter」要同时匹配文件名里的 cover_letter / cover-letter。
    terms: list[str] = []
    for variant in variants:
        for form in (variant, variant.replace(" ", "_"), variant.replace(" ", "-")):
            if form not in terms:
                terms.append(form)

    clauses = ["s.state = 'active'", "l.availability = 'AVAILABLE'"]
    params: list[object] = []
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    clauses.append(
        "(" + " OR ".join("instr(lower(l.path), ?) > 0" for _ in terms) + ")"
    )
    params.extend(terms)
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
         specific_folder, specific_folder_index, name_strength) = _path_score(
            relative, analysis)
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
            name_strength=name_strength,
        )
    return matches


def _path_only_bonus(match: PathMatch | None, path_only: bool) -> int:
    """位置问题里，文件名/文件夹真正命中主题词的候选优先。

    「结果文件放在哪个目录」这类追问补上上下文后，黄姑鱼小课题下所有文件
    都会命中「黄姑鱼」；若只看路径 gram 总分，结果取决于数据库返回顺序。
    因此文件名命中主题词额外加权，命中主题文件夹再补一档。
    """
    if not path_only or match is None:
        return 0
    bonus = 0
    if match.name_matched and match.specific and match.name_strength >= 3:
        bonus += 240
    if match.folder_specific:
        bonus += 120
    return bonus


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

        # 两字母缩写（FE/RE/GO/GC）用 LIKE 会命中英文子串（fe → different），
        # 候选里混满无关文档。这些短词只接受独立成词的出现，中文短词仍按子串。
        ascii_short = [unit for unit in short_units
                       if re.fullmatch(r"[a-z0-9_]{2}", unit)]
        if ascii_short:
            boundary = [re.compile(r"(?<![a-z0-9])" + re.escape(unit) + r"(?![a-z0-9])")
                        for unit in ascii_short]
            other_units = [unit for unit in variants if unit not in ascii_short]

            def row_ok(text_value: str) -> bool:
                lowered = (text_value or "").lower()
                if any(unit in lowered for unit in other_units):
                    return True
                return any(pattern.search(lowered) for pattern in boundary)

            rows = [row for row in rows if row_ok(row["text"])]

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
            path_score=(path_match.score if path_match else 0)
                       + _path_only_bonus(path_match, path_only),
            maximal=maximal_hit,
        ))

    seen = {hit.location_id for hit in hits}
    extra_matches = sorted(
        (match for location_id, match in path_index.items() if location_id not in seen),
        key=lambda match: -(match.score + _path_only_bonus(match, path_only)
                            + name_prior(match.path, analysis)),
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
            path_score=match.score + _path_only_bonus(match, path_only),
        ))
        added += 1

    for hit in hits:
        hit.path_score += name_prior(hit.path, analysis)
        if focus:
            lowered = hit.text.lower()
            hit.focus = min(3, sum(1 for term in focus
                                   if len(term) >= 2 and term in lowered))

    if path_only:
        # 位置问题只关心路径：正文里碰巧出现「结果」不该压过文件名命中。
        hits.sort(key=lambda hit: (
            -(hit.focus * 120 + hit.maximal * 10 + hit.path_score),
            -hit.concepts,
            -hit.variants,
            -len(hit.text),
        ))
    else:
        hits.sort(key=lambda hit: (
            -(hit.focus * 120 + hit.maximal * 10 + min(hit.path_score, 300)),
            -hit.concepts,
            -hit.variants,
            -len(hit.text),
        ))
    # 重复段落合并为一条并保留全部来源；各版本独有段落继续保留。
    return families.dedupe_chunk_hits(conn, hits, path_only=path_only)