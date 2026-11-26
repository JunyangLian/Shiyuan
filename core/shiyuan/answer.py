"""M4 问答与引用：先用本地检索收集证据，再交给"本地摘录"或可选模型组织回答。

硬约束：
- 没有证据就拒答，不凭文件名校验或常识补全；
- 云端调用必须显式授权，只上传入选的证据片段，不整库上传；
- 回答必须带 [n] 引用，引用越界或缺失会在界面上明确提示。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import (embeddings, families, indexer, knowledge, planner, projects, query,
               rerank, retrieval, secrets_store, store)

SETTINGS_KEY = "answer"
PROVIDERS = {"extractive", "openai", "ollama"}
MAX_EVIDENCE_CHARS = 900
MAX_PROMPT_CHARS = 8000
# 没有任何词面证据时，语义片段要达到这个相似度才单独算证据（bge-m3 实测：
# 相关问题 0.61+，无关问题 0.47~0.60）。
MIN_SEMANTIC_ONLY = 0.62

DEFAULT_SETTINGS = {
    "provider": "extractive",
    "base_url": "",
    "model": "",
    "api_key": "",
    "allow_cloud": False,
    "top_k": 6,
    # 推理强度：low 不产生显式推理链，回答更快；high/max 留给复杂问题。
    "reasoning_effort": "low",
    "max_tokens": 1600,
}


class AnswerError(RuntimeError):
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
        settings["provider"] = "extractive"
    try:
        settings["top_k"] = max(1, min(int(settings["top_k"]), 12))
    except (TypeError, ValueError):
        settings["top_k"] = 6
    if settings.get("reasoning_effort") not in {"low", "high", "max"}:
        settings["reasoning_effort"] = "low"
    try:
        settings["max_tokens"] = max(200, min(int(settings.get("max_tokens") or 1600), 4000))
    except (TypeError, ValueError):
        settings["max_tokens"] = 1600
    settings["allow_cloud"] = bool(settings["allow_cloud"])
    if settings.get("api_key"):
        try:
            settings["api_key"] = secrets_store.unprotect(settings["api_key"])
        except secrets_store.SecretError:
            settings["api_key"] = ""
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
            raise ValueError("未知的回答提供商")
        settings["provider"] = provider
    for key in ("base_url", "model"):
        if key in payload and payload[key] is not None:
            settings[key] = str(payload[key]).strip()
    if payload.get("allow_cloud") is not None:
        settings["allow_cloud"] = bool(payload["allow_cloud"])
    if payload.get("top_k") is not None:
        try:
            settings["top_k"] = max(1, min(int(payload["top_k"]), 12))
        except (TypeError, ValueError) as exc:
            raise ValueError("top_k 必须是数字") from exc
    if payload.get("reasoning_effort") is not None:
        effort = str(payload["reasoning_effort"]).strip().lower()
        if effort not in {"low", "high", "max"}:
            raise ValueError("推理强度必须是 low、high 或 max")
        settings["reasoning_effort"] = effort
    if payload.get("max_tokens") is not None:
        try:
            settings["max_tokens"] = max(200, min(int(payload["max_tokens"]), 4000))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_tokens 必须是数字") from exc
    if payload.get("clear_key"):
        settings["api_key"] = ""
    elif payload.get("api_key"):
        settings["api_key"] = secrets_store.protect(str(payload["api_key"]).strip())
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


# ---------------------------------------------------------------- 检索

@dataclass
class Evidence:
    chunk_id: str
    asset_id: str
    location_id: str
    path: str
    name: str
    text: str
    page: int | None
    sheet: str | None
    matches: int
    ocr: bool = False
    semantic: bool = False
    version_id: str = ""
    mtime: str | None = None
    bbox: list[list[int]] | None = None
    family_id: str | None = None
    family_size: int = 1
    family_title: str = ""
    version_label: str = ""
    sources: list[dict] = field(default_factory=list)
    knowledge: str = ""  # 衍生知识标注：摘要 / 关系 / 问答；空表示原文片段

    def label(self, include_path: bool = False) -> str:
        if self.knowledge:
            prefix = {"摘要": "【摘要】", "关系": "【关系】", "问答": "【问答】"}.get(
                self.knowledge, "【知识】")
            name = self.name
        else:
            prefix = ""
            name = f"文件：{self.name}"
        if include_path and self.path:
            directory = self.path[: len(self.path) - len(self.name)].rstrip("\\/")
            if directory:
                name += f"（目录：{directory}）"
        if self.page:
            name += f"（第 {self.page} 页）"
        if self.sheet:
            name += f"（工作表 {self.sheet}）"
        if self.ocr:
            name += "（OCR 识别）"
        if self.family_size > 1:
            name += f"（同一文档 {self.family_size} 个版本）"
        if self.mtime:
            name += f"（更新于 {self.mtime.replace('T', ' ')[:16]}）"
        return prefix + name

    def citation(self, number: int) -> dict:
        snippet = " ".join(self.text.split())
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        return {
            "n": number,
            "chunk_id": self.chunk_id,
            "asset_id": self.asset_id,
            "location_id": self.location_id,
            "name": self.name,
            "path": self.path,
            "page": self.page,
            "sheet": self.sheet,
            "snippet": snippet,
            "ocr": self.ocr,
            "semantic": self.semantic,
            "version_id": self.version_id,
            "mtime": self.mtime,
            "bbox": self.bbox,
            "family_id": self.family_id,
            "family_size": self.family_size,
            "family_title": self.family_title,
            "version_label": self.version_label,
            "sources": self.sources,
            "knowledge": self.knowledge,
        }


def _snippet(text: str, focus: list[str] | None) -> str:
    """追问时把片段截到关键词附近，避免长文档的目录占了开头。"""
    if focus:
        lowered = (text or "").lower()
        for term in focus:
            index = lowered.find(term)
            if index > 200:
                return "…" + text[max(0, index - 160):]
    return (text or "")[:MAX_EVIDENCE_CHARS]


def _shingles(text: str, size: int = 4) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def _duplicate_of(signature: set[str], chosen: list[set[str]],
                  threshold: float = 0.88) -> bool:
    """短片段被长片段包含时也算重复，避免同一段落换个版本再占一个证据位。"""
    if not signature:
        return False
    for other in chosen:
        if not other:
            continue
        overlap = len(signature & other) / max(1, min(len(signature), len(other)))
        if overlap >= threshold:
            return True
    return False


def _specific_analysis(analysis: dict) -> dict:
    """只保留具体词：衍生知识匹配不能因为「整理/系统/企业」这类泛词命中。"""
    concepts = []
    for concept in analysis.get("concepts", []):
        variants = [variant for variant in concept.get("variants", [])
                    if len(variant) >= 2 and not retrieval.is_generic_term(variant)]
        if variants:
            concepts.append({"term": concept["term"], "variants": variants})
    variants: list[str] = []
    for concept in concepts:
        for variant in concept["variants"]:
            if variant not in variants:
                variants.append(variant)
    return {**analysis, "concepts": concepts, "variants": variants}


def _strong_local_evidence(candidates: list, analysis: dict) -> bool:
    """本地词面证据是否强到可以无视「重排为空」的结论。

    重排模型偶尔会把 README/QUICKSTART 这类明确说明文档判成无关；若本地
    已经通过文件名/文件夹或大量正文概念命中，就不该整题拒答。
    """
    if not candidates:
        return False
    top = candidates[0]
    if top.path_score >= 220:
        return True
    if top.concepts >= 5:
        return True
    return False


def _can_skip_rerank(candidates: list, analysis: dict) -> bool:
    """本地证据很强且问题属于「怎么用/是什么」时，可以跳过模型重排。

    普通问答仍交给重排过滤噪声；只有 README/QUICKSTART 这类操作说明
    最适合本地顺序（文件名先验已把它排到最前），跳过能省 10–30 秒。
    """
    if not (analysis.get("howto") or analysis.get("intro")):
        return False
    return _strong_local_evidence(candidates, analysis)


def retrieve(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
             ext: str | None = None, limit: int = 6,
             focus: list[str] | None = None,
             path_only: bool = False,
             plan: planner.Plan | None = None,
             stats: dict | None = None,
             anchor_prefix: str | None = None,
             allowed_assets: set[str] | None = None,
             excluded_assets: set[str] | None = None,
             priority_assets: set[str] | None = None) -> list[Evidence]:
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    if not analysis["variants"]:
        return []
    if allowed_assets is not None and not allowed_assets:
        # 空项目/全部排除：没有可检索的资料，直接返回空证据。
        return []
    per_asset_limit = plan.mode.per_asset if plan else 1
    per_family_limit = plan.mode.per_family if plan else 2

    evidence: list[Evidence] = []
    chosen_sigs: list[set[str]] = []
    used_chunks: set[str] = set()
    per_asset_count: dict[str, int] = {}
    per_family_count: dict[str, int] = {}

    def family_key(asset_id: str, family_id: str | None) -> str:
        return family_id or f"asset:{asset_id}"

    def take(asset_id: str, family_id: str | None, text: str,
             *, allow_second: bool) -> bool:
        """按计划控制同文件/同家族配额，并跳过与已选内容高度重合的片段。"""
        key = family_key(asset_id, family_id)
        if per_asset_count.get(asset_id, 0) >= per_asset_limit:
            return False
        cap = per_family_limit if allow_second else 1
        if per_family_count.get(key, 0) >= cap:
            return False
        signature = _shingles(text)
        if _duplicate_of(signature, chosen_sigs):
            return False
        per_asset_count[asset_id] = per_asset_count.get(asset_id, 0) + 1
        per_family_count[key] = per_family_count.get(key, 0) + 1
        chosen_sigs.append(signature)
        return True

    def build(hit: retrieval.ChunkHit) -> Evidence:
        name = hit.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return Evidence(
            chunk_id=hit.chunk_id,
            asset_id=hit.asset_id,
            location_id=hit.location_id,
            path=hit.path,
            name=name,
            text=_snippet(hit.text, focus),
            page=hit.page,
            sheet=hit.sheet,
            matches=hit.concepts,
            ocr=hit.ocr,
            version_id=hit.version_id,
            mtime=_iso_mtime(hit.mtime_ns),
            bbox=hit.bbox,
            family_id=hit.family_id,
            family_size=hit.family_size,
            family_title=hit.family_title,
            version_label=hit.version_label,
            sources=hit.sources,
        )

    candidates = retrieval.find_chunks(conn, question, analysis=analysis,
                                       source_id=source_id, ext=ext, focus=focus,
                                       path_only=path_only,
                                       allowed_assets=allowed_assets,
                                       excluded_assets=excluded_assets)
    if anchor_prefix:
        # 追问限定上一轮数据源时在内存里过滤，避免 SQLite 为“MATCH + source”扫描全库。
        candidates = [hit for hit in candidates if hit.path.startswith(anchor_prefix)]
    if priority_assets:
        # 扩展检索里项目主资料优先，但不打乱同一优先级内的检索顺序。
        candidates = sorted(
            candidates, key=lambda hit: 0 if hit.asset_id in priority_assets else 1)
    original_candidates = candidates
    candidate_total = len(candidates)
    # 候选明显多于预算时，先让已授权的生成模型按「能否直接回答」筛选并排序；
    # 未配置、失败或超时都保持本地顺序，不影响问答。精确查找同样要过滤，
    # 否则课程题库这类同词不同义的文档会被当成证据。
    answer_settings = load_settings(conn)
    rerank_stats: dict = {}
    rerank_empty = False
    if (plan is not None and rerank.enabled(answer_settings) and not path_only
            and len(candidates) >= 3):
        if _can_skip_rerank(candidates, analysis):
            # 操作说明类问题：README/QUICKSTART 已被文件名先验排到最前，
            # 再让模型重排只会增加等待。
            rerank_stats["skipped"] = "strong_local"
        else:
            candidates, rerank_stats = rerank.rerank(
                answer_settings, question, candidates)
            rerank_empty = bool(rerank_stats.get("empty"))
            if rerank_empty and _strong_local_evidence(original_candidates, analysis):
                # 本地证据本来就强（文件名/目录或大量概念命中），不接受“全空”结论。
                rerank_stats["fallback"] = "strong_local"
                candidates = original_candidates
                rerank_empty = False
            elif rerank_empty:
                # 模型判断没有任何候选能回答问题：按无证据处理，避免硬凑答案。
                candidates = []

    # 先按家族取样，凑够计划要求的最小覆盖面；再回头补同家族的第二条。
    min_families = plan.mode.min_families if plan else 1
    if min_families > 1:
        families_seen: set[str] = set()
        for hit in candidates:
            if len(evidence) >= limit or len(families_seen) >= min_families:
                break
            key = family_key(hit.asset_id, hit.family_id)
            if key in families_seen:
                continue
            if take(hit.asset_id, hit.family_id, hit.text, allow_second=False):
                evidence.append(build(hit))
                used_chunks.add(hit.chunk_id)
                families_seen.add(key)

    for hit in candidates:
        if len(evidence) >= limit:
            break
        if hit.chunk_id in used_chunks:
            continue
        if take(hit.asset_id, hit.family_id, hit.text, allow_second=True):
            evidence.append(build(hit))
            used_chunks.add(hit.chunk_id)

    embed_settings = embeddings.load_settings(conn)
    if (embeddings.enabled(embed_settings) and len(evidence) < limit
            and not rerank_empty):
        try:
            semantic_hits = embeddings.semantic_chunks(
                conn, question, source_id=source_id, ext=ext,
                top_k=max(10, embed_settings["top_k"] * 3),
                allowed_assets=allowed_assets,
                excluded_assets=excluded_assets)
        except embeddings.EmbeddingError:
            semantic_hits = []
        semantic_hits = families.dedupe_semantic_hits(conn, semantic_hits)
        if anchor_prefix:
            semantic_hits = [hit for hit in semantic_hits
                             if hit["path"].startswith(anchor_prefix)]
        if allowed_assets is not None or excluded_assets:
            semantic_hits = [
                hit for hit in semantic_hits
                if (allowed_assets is None or hit.get("asset_id") in allowed_assets)
                and (not excluded_assets or hit.get("asset_id") not in excluded_assets)
            ]
        if priority_assets:
            semantic_hits = sorted(
                semantic_hits,
                key=lambda hit: 0 if hit.get("asset_id") in priority_assets else 1)
        for hit in semantic_hits:
            if len(evidence) >= limit:
                break
            if hit["chunk_id"] in used_chunks:
                continue
            # 没有任何词面证据时，语义命中必须足够强才算证据；
            # 否则「明天会下雨」这类无关问题也会被弱相似片段硬凑出答案。
            if not evidence and hit["similarity"] < MIN_SEMANTIC_ONLY:
                continue
            if not take(hit["asset_id"], hit.get("family_id"), hit["text"],
                        allow_second=True):
                continue
            name = hit["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            evidence.append(Evidence(
                chunk_id=hit["chunk_id"],
                asset_id=hit["asset_id"],
                location_id=hit["location_id"],
                path=hit["path"],
                name=name,
                text=hit["text"][:MAX_EVIDENCE_CHARS],
                page=hit["page"],
                sheet=hit["sheet"],
                matches=0,
                ocr=hit["ocr"],
                semantic=True,
                version_id=hit["version_id"],
                mtime=_iso_mtime(hit["mtime_ns"]),
                bbox=hit.get("bbox"),
                family_id=hit.get("family_id"),
                family_size=hit.get("family_size", 1),
                family_title=hit.get("family_title", ""),
                version_label=hit.get("version_label", ""),
                sources=hit.get("sources", []),
            ))
            used_chunks.add(hit["chunk_id"])

    # 衍生知识证据：解释/综合模式补充摘要、FAQ，关系类问题叠加知识关系。
    knowledge_used = 0
    relation_sources = 0
    faq_sources = 0
    # 综合类问题允许摘要作为唯一证据；解释/操作类必须先有原文证据，
    # 否则无关问题会被一条弱相关摘要硬凑出答案。
    if (plan is not None and not rerank_empty
            and plan.mode.key in {"explain", "comprehensive"}
            and (evidence or plan.mode.key == "comprehensive")):
        # 有原文证据时只用具体词匹配摘要，避免「整理/系统」这类泛词把
        # 无关文档的摘要拉进来；没有原文证据时退回全部词，让摘要兜底。
        base_evidence = list(evidence)
        knowledge_analysis = _specific_analysis(analysis) if base_evidence else analysis
        try:
            summary_items = knowledge.summary_matches(
                conn, knowledge_analysis, base_evidence, strict=bool(base_evidence))
        except Exception:
            summary_items = []
        for item in summary_items:
            if len(evidence) >= limit:
                break
            evidence.append(Evidence(
                chunk_id=f"summary:{item['summary_id']}",
                asset_id=item["scope_id"],
                location_id="",
                path=item["path"],
                name=item["label"],
                text=item["summary"][:MAX_EVIDENCE_CHARS],
                page=None,
                sheet=None,
                matches=0,
                family_id=item["scope_id"] if item["scope_type"] == "family" else None,
                knowledge="摘要",
            ))
            knowledge_used += 1

        try:
            faq_items = knowledge.faq_matches(
                conn, knowledge_analysis, base_evidence, strict=bool(base_evidence))
        except Exception:
            faq_items = []
        for item in faq_items:
            if len(evidence) >= limit:
                break
            evidence.append(Evidence(
                chunk_id=f"faq:{item['question_id']}",
                asset_id=item["family_id"],
                location_id="",
                path=item["path"],
                name=item["label"],
                text=(f"{item['question']}\n{item['answer']}")[:MAX_EVIDENCE_CHARS],
                page=None,
                sheet=None,
                matches=0,
                family_id=item["family_id"],
                knowledge="问答",
            ))
            knowledge_used += 1
            faq_sources += 1

        if getattr(plan, "intent_hint", None) == "relation":
            try:
                relation_items = knowledge.relation_matches(
                    conn, knowledge_analysis, base_evidence,
                    strict=bool(base_evidence))
            except Exception:
                relation_items = []
            for item in relation_items:
                if len(evidence) >= limit:
                    break
                evidence.append(Evidence(
                    chunk_id=f"relation:{item['relation_id']}",
                    asset_id=item["family_id"],
                    location_id="",
                    path=item["path"],
                    name=item["label"],
                    text=item["text"][:MAX_EVIDENCE_CHARS],
                    page=None,
                    sheet=None,
                    matches=0,
                    family_id=item["family_id"],
                    knowledge="关系",
                ))
                knowledge_used += 1
                relation_sources += 1

    if stats is not None:
        blob = [f"{item.name}\n{item.text}".lower() for item in evidence]
        facets = [
            {"name": facet, "covered": any(facet.lower() in text for text in blob)}
            for facet in (plan.facets if plan else [])
        ]
        try:
            knowledge_counts = knowledge.counts(conn)
        except Exception:
            knowledge_counts = {"pending": 0, "failed": 0}
        stats.update({
            "candidates": candidate_total,
            "used": len(evidence),
            "families": len({item.family_id or item.asset_id for item in evidence}),
            "facets": facets,
            "missing": [item["name"] for item in facets if not item["covered"]],
            "reranked": bool(rerank_stats.get("reranked")),
            "rerank_kept": rerank_stats.get("kept", candidate_total),
            "rerank_empty": bool(rerank_stats.get("empty")),
            "rerank_skipped": rerank_stats.get("skipped"),
            "rerank_fallback": bool(rerank_stats.get("fallback")),
            "rerank_error": rerank_stats.get("error"),
            "knowledge_used": knowledge_used,
            "summary_sources": knowledge_used - relation_sources - faq_sources,
            "relation_sources": relation_sources,
            "faq_sources": faq_sources,
            "knowledge_pending": knowledge_counts.get("pending", 0),
            "knowledge_error": knowledge_counts.get("failed", 0),
        })
    return evidence


def _iso_mtime(mtime_ns: int) -> str | None:
    if not mtime_ns:
        return None
    return datetime.fromtimestamp(mtime_ns / 1e9).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 回答

def _evidence_block(evidence: list[Evidence], include_path: bool = False) -> str:
    parts = []
    total = 0
    for index, item in enumerate(evidence, start=1):
        piece = f"[{index}] {item.label(include_path)}\n{item.text}"
        if total + len(piece) > MAX_PROMPT_CHARS:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _plan_note(plan: planner.Plan, stats: dict) -> str:
    """把检索计划与覆盖情况交给模型，避免它对缺失信息装作完整。"""
    note = (
        f"检索模式：{plan.mode.label}（{plan.reason}）。"
        f"候选 {stats.get('candidates', 0)} 条，采用 {stats.get('used', 0)} 条，"
        f"覆盖 {stats.get('families', 0)} 个文档家族。"
    )
    missing = stats.get("missing") or []
    if missing:
        note += (f"以下关键词未在证据原文中直接出现：{'、'.join(missing)}；"
                 "若问题确实涉及这些方面，请说明资料不足，但不要因此忽略已有的其他证据。")
    return note


def _messages(question: str, evidence: list[Evidence],
              previous: str | None = None,
              plan_note: str | None = None,
              include_path: bool = False,
              previous_answer: str | None = None) -> list[dict]:
    system = (
        "你是本地知识库的检索助手，只能依据用户消息中的<证据>回答。"
        "禁止补充证据之外的事实或推测。每个事实句末用 [数字] 标注对应证据。"
        "证据中的数字、日期、编号和专有名词必须原样引用，不要改写或省略。"
        f"今天是 {datetime.now().strftime('%Y-%m-%d')}（本地时区）。"
        "遇到「上个月/最近/今年」等相对时间，先按今天换算成具体年月再判断证据，"
        "并在回答里说明采用的日期范围。"
        "证据标题带有文件的更新时间；若问题涉及时间条件（如最近、去年、某个日期前后），"
        "要用这些时间判断取舍，并在回答里说明采用的版本时间。"
        "证据可能来自同一份文档的多个版本。不要判断或宣称哪个版本最新、最权威、"
        "最值得采用；不要替用户选定版本。若不同版本内容有差异，请并列说明各自来自"
        "哪个文件或版本标记，不要把它们混成一份内容。"
        "若证据之间数值或结论不一致，请并列呈现，并说明各自来自哪个文件/时间条件。"
        "证据只是本次检索命中的少量片段，不代表知识库的全部文件或全部内容；"
        "不要根据证据条数推断文件总数、知识库规模或用户拥有的资料范围。"
        "不要自行做跨行汇总或算术计算（求和、平均、总计），也不要根据片段推算数字；"
        "若问题需要这类统计，说明拾源当前不做表格汇总，并指出相关文件。"
        "若证据只能回答一部分，先直接回答能确定的部分，再说明还缺什么；"
        "只有完全无法回答时才说：根据现有资料无法确定。"
    )
    if previous_answer:
        system += (
            "追问里的「它、这个、上述、版本N、第N个」要对照上一轮回答确定指代对象；"
            "上一轮回答只用于理解指代，不能当作证据或引用来源。"
        )
    user = f"<问题>\n{question}"
    if previous:
        user += f"\n（对话追问，上一轮问题：{previous}）"
    if previous_answer:
        user += f"\n\n<上一轮回答（只用于理解指代，不作为证据）>\n{previous_answer[:800]}"
    if plan_note:
        user += f"\n\n<检索规划>\n{plan_note}"
    user += f"\n\n<证据>\n{_evidence_block(evidence, include_path)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post_json(url: str, payload: dict, headers: dict, timeout: float | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    if timeout is None:
        timeout = float(os.environ.get("SHIYUAN_MODEL_TIMEOUT", "90"))
    box: dict = {}

    def worker() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                box["body"] = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            box["error"] = AnswerError(f"模型接口返回 {exc.code}：{detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            box["error"] = AnswerError(f"无法连接模型接口：{exc}")
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须带回主线程
            box["error"] = AnswerError(f"模型接口异常：{exc}")

    # urllib 的 timeout 只作用于单次读写；上游慢速滴流时仍可能挂很久。
    # 这里再加一道墙钟上限，保证用户最多等 timeout + 5 秒。
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if thread.is_alive():
        raise AnswerError("模型接口超时（超过墙钟上限）")
    if "error" in box:
        raise box["error"]
    try:
        return json.loads(box.get("body", ""))
    except ValueError as exc:
        raise AnswerError("模型接口返回了非 JSON 内容") from exc


def _stream_sse(url: str, payload: dict, headers: dict, timeout: float,
                on_delta) -> str:
    """OpenAI 兼容接口的流式读取：只转发正文增量，忽略推理内容。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    parts: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    item = json.loads(data)
                except ValueError:
                    continue
                choices = item.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if not piece:
                    continue
                parts.append(piece)
                on_delta(piece)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AnswerError(f"模型接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AnswerError(f"无法连接模型接口：{exc}") from exc
    content = "".join(parts).strip()
    if not content:
        raise AnswerError("模型流式返回了空内容")
    return content


def _stream_ollama(url: str, payload: dict, timeout: float, on_delta) -> str:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    parts: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                piece = (item.get("message") or {}).get("content")
                if piece:
                    parts.append(piece)
                    on_delta(piece)
                if item.get("done"):
                    break
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AnswerError(f"无法连接 Ollama：{exc}") from exc
    content = "".join(parts).strip()
    if not content:
        raise AnswerError("Ollama 流式返回了空内容")
    return content


def _openai_answer(settings: dict, question: str, evidence: list[Evidence],
                   previous: str | None = None,
                   plan_note: str | None = None,
                   include_path: bool = False,
                   on_delta=None,
                   previous_answer: str | None = None) -> str:
    if not settings.get("allow_cloud"):
        raise AnswerError("云端回答未获授权：请先在对话设置里允许上传证据片段")
    if not settings.get("api_key") or not settings.get("model"):
        raise AnswerError("云端回答尚未配置模型名称或 API Key")
    base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "max_tokens": int(settings.get("max_tokens") or 1600),
        "messages": _messages(question, evidence, previous, plan_note, include_path,
                              previous_answer),
    }
    effort = settings.get("reasoning_effort")
    if effort:
        payload["reasoning_effort"] = effort
    if on_delta is not None:
        payload["stream"] = True

    def send(current: dict) -> str:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {settings['api_key']}"}
        if on_delta is not None:
            timeout = float(os.environ.get("SHIYUAN_MODEL_TIMEOUT", "90"))
            return _stream_sse(url, current, headers, timeout, on_delta)
        data = _post_json(url, current, headers)
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise AnswerError("模型接口返回结构不符合 OpenAI 兼容格式") from exc

    try:
        return send(payload)
    except AnswerError as exc:
        # 只对不支持该参数的兼容服务回退；超时等错误不重试，避免等待翻倍。
        message = str(exc)
        if "reasoning_effort" not in message and "1210" not in message and "思考" not in message:
            raise
        payload.pop("reasoning_effort", None)
        return send(payload)


def _ollama_answer(settings: dict, question: str, evidence: list[Evidence],
                   previous: str | None = None,
                   plan_note: str | None = None,
                   include_path: bool = False,
                   on_delta=None,
                   previous_answer: str | None = None) -> str:
    if not settings.get("model"):
        raise AnswerError("Ollama 回答尚未填写模型名称")
    base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": settings["model"], "stream": on_delta is not None,
               "messages": _messages(question, evidence, previous, plan_note,
                                     include_path, previous_answer)}
    if on_delta is not None:
        timeout = float(os.environ.get("SHIYUAN_MODEL_TIMEOUT", "90"))
        return _stream_ollama(f"{base}/api/chat", payload, timeout, on_delta)
    data = _post_json(f"{base}/api/chat", payload, {})
    try:
        return str(data["message"]["content"]).strip()
    except (KeyError, TypeError) as exc:
        raise AnswerError("Ollama 返回结构不符合预期") from exc


def _extractive_answer(evidence: list[Evidence]) -> str:
    lines = ["以下是与问题最相关的证据摘录（本地模式，未调用生成模型）："]
    for index, item in enumerate(evidence, start=1):
        snippet = " ".join(item.text.split())
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        lines.append(f"[{index}] {item.label()}：{snippet}")
    return "\n\n".join(lines)


_REFUSAL_ANSWER = re.compile(
    r"^\s*(?:根据现有资料|根据检索到的证据|根据证据)?[，,\s]*"
    r"(?:无法确定|无法回答|无法从现有资料|没有找到|未找到|没有足够证据|证据不足)")


def _looks_like_refusal(text: str) -> bool:
    """模型虽然返回了正文，但明确表示现有证据不足：标记为拒答。"""
    return bool(_REFUSAL_ANSWER.search((text or "")[:80]))


def _citation_warning(text: str, evidence_count: int) -> str | None:
    numbers = {int(value) for value in re.findall(r"\[(\d+)\]", text)}
    valid = {value for value in numbers if 1 <= value <= evidence_count}
    if not valid:
        return "回答没有引用任何证据，请结合下方来源自行核对，不要直接采信。"
    if numbers - valid:
        extra = "、".join(f"[{value}]" for value in sorted(numbers - valid))
        return f"回答包含不存在的引用 {extra}，已忽略这些编号。"
    return None


def _coverage(conn: sqlite3.Connection) -> dict:
    counts = indexer.coverage(conn)
    return {"content_indexed": counts["indexed"], "content_pending": counts["pending"],
            "content_empty": counts["empty"], "content_failed": counts["failed"],
            "content_metadata_only": counts["metadata_only"],
            "ocr_indexed": counts["ocr_indexed"], "ocr_pending": counts["ocr_pending"]}


_OVERVIEW_PATTERNS = (
    re.compile(
        r"(知识库|资料库|库里|拾源|你)(里|中)?(现在|目前|都|一共|总共)?"
        r"(有|包含|收录|存了)(什么|哪些)(文件|资料|文档)?[？?。！!\s]*$"
    ),
    re.compile(r"(有多少|总共多少|一共多少)(个|份)?(文件|资料|文档)"),
    re.compile(r"(文件|资料|文档)(总数|数量|概况)"),
    re.compile(r"(知识库|资料库|库里)(概况|概览|统计)"),
)


_SOURCE_OVERVIEW = re.compile(r"(数据源|资料源|资料来源|授权目录|授权文件夹)")
_SOURCE_ASK = re.compile(r"(哪些|什么|几个|多少|列表|概况|情况|现在|当前|都有)")


def _is_source_overview(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 40:
        return False
    return bool(_SOURCE_OVERVIEW.search(compact) and _SOURCE_ASK.search(compact))


def _source_overview_answer(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT s.label, s.root_uri, s.state,"
        " (SELECT COUNT(*) FROM asset_locations l"
        "  WHERE l.source_id = s.source_id AND l.availability = 'AVAILABLE') AS available"
        " FROM sources s ORDER BY s.created_at").fetchall()
    if not rows:
        return "当前还没有授权任何数据源。"
    lines = [f"当前共有 {len(rows)} 个数据源："]
    for index, row in enumerate(rows, start=1):
        state = "启用" if row["state"] == "active" else "已暂停"
        lines.append(
            f"{index}. {row['label']}（{row['root_uri']}，{state}，"
            f"可检索 {row['available']} 个文件）")
    lines.append("这是本地数据源列表，不是某一份文档里的内容。")
    return "\n".join(lines)


def _is_library_overview(text: str) -> bool:
    """识别“知识库里有什么/有多少文件”这类元问题，避免用 6 条证据冒充全库。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 40:
        return False
    return any(pattern.search(compact) for pattern in _OVERVIEW_PATTERNS)


_SOURCE_CUE = re.compile(
    r"(记得|授权|数据源|那个|这个|之前|上次|在哪|位于|是什么|有什么)"
)


def _bigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value or "").lower()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


_SOURCE_LOCATIVE = ("里", "中", "内", "数据源", "目录", "文件夹", "下面", "之下", "当中")
_SOURCE_LIST = re.compile(r"(有哪些|有什么|有多少|多少个|多少份|概况|概览|统计|列出|清单)")


def _named_source(conn: sqlite3.Connection, text: str) -> tuple[str, str] | None:
    """问题明确指向某个数据源时，返回 (source_id, 展示名) 用于限定检索范围。

    只认「X 里/中/内/数据源」或「X 有哪些文件」这类指向目录的说法；
    「A 论文有没有提到 B」这种提到项目名、但实际可能在别处找证据的问法不限定。
    """
    compact = re.sub(r"\s+", "", text or "").lower()
    if not compact:
        return None
    best: tuple[str, str] | None = None
    for row in store.list_sources(conn):
        if row["state"] != "active":
            continue
        for name in (row["label"] or "", Path(row["root_uri"]).name):
            candidate = re.sub(r"\s+", "", name).lower()
            if len(candidate) < 2:
                continue
            index = compact.find(candidate)
            if index < 0:
                continue
            tail = compact[index + len(candidate):]
            scoped = any(tail.startswith(mark) for mark in _SOURCE_LOCATIVE)
            if not scoped and index == 0 and _SOURCE_LIST.search(tail):
                scoped = True
            if not scoped:
                continue
            if best is None or len(candidate) > len(best[1]):
                best = (row["source_id"], row["label"] or name)
    return best


def _match_source(conn: sqlite3.Connection, text: str,
                  source_id: str | None = None) -> sqlite3.Row | None:
    """识别“你还记得那个 XX 吗”这类对已授权数据源的追问。"""
    compact = re.sub(r"\s+", "", text or "").lower()
    if not compact or len(compact) > 60 or not _SOURCE_CUE.search(compact):
        return None
    query_grams = _bigrams(compact)
    if len(query_grams) < 2:
        return None

    best: sqlite3.Row | None = None
    best_score = 0
    for row in store.list_sources(conn):
        if source_id and row["source_id"] != source_id:
            continue
        for name in (row["label"] or "", Path(row["root_uri"]).name):
            candidate = re.sub(r"\s+", "", name).lower()
            if not candidate:
                continue
            shared = query_grams & _bigrams(candidate)
            if len(shared) < 2:
                continue
            score = len(shared) + (4 if candidate in compact else 0)
            if score > best_score:
                best_score = score
                best = row
    return best


def _source_recall_answer(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    stats = store.source_stats(conn, row["source_id"])
    available = int(stats["available"] or 0)
    parsed = conn.execute(
        "SELECT COUNT(DISTINCT c.asset_id) FROM chunks c"
        " JOIN asset_locations l ON l.asset_id = c.asset_id"
        " WHERE l.source_id = ? AND l.availability = 'AVAILABLE'",
        (row["source_id"],),
    ).fetchone()[0]
    label = row["label"] or Path(row["root_uri"]).name
    lines = [
        f"记得。你授权的数据源「{label}」在 `{row['root_uri']}`。",
        f"当前有 {available} 个可检索文件，其中 {parsed} 个已解析正文"
        + (f"，{available - parsed} 个待解析。" if available > parsed else "。"),
    ]
    if row["state"] != "active":
        lines.append("这个数据源目前处于暂停状态。")
    lines.append("你可以继续问这个数据源里的具体内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件夹概览

# 「大三学了哪些课程」「pilot_batches 里有什么」这类问题问的是目录结构，
# 用零散正文片段回答反而更差，直接按文件夹汇总。
_FOLDER_INTENT = (
    re.compile(
        r"(?:哪些|什么|几门|几个)"
        r"(?:课程|科目|课|文件夹|子目录|目录|资料|文件|项目|批次|作业|报告|版本)"
    ),
    re.compile(
        r"(?:课程|科目|课|文件夹|子目录|目录|资料|文件|项目|批次|实验|作业|报告|版本)"
        r"(?:里面?|中|内)?(?:都)?(?:有|是|包括|包含|放|存|收录)(?:了)?(?:些)?(?:什么|哪些|啥)"
    ),
    # 「XX 里/中/下有什么」才是目录问题；「专利里提到的软件有哪些」问的是内容，
    # 不能因为句子里出现「里」和「有哪些」就当成目录概览。
    re.compile(
        r"(?:里面?|中|内|下)(?:都)?(?:有|包括|包含|放|存)(?:了)?(?:些)?(?:什么|哪些|啥)"),
)

# 「放在哪个文件夹 / 存在哪里 / 课程表在哪」问的是位置，只按路径命中最稳妥。
# 注意别把「数据是从哪里来的」这类来源问题当成路径问题：后者没有「在/放/存」
# 这类位置动词，应该走正常检索。
_PATH_INTENT = re.compile(
    r"哪(?:个)?(?:文件夹|目录|盘)"
    r"|(?:放在|存放|保存在|存在|放到|存到)(?:了)?哪"
    r"|(?:文件|资料|表格|结果表|汇总表|清单|图片|课件|报告|作业|试卷|单据|记录|数据|课程表|截图|照片|发票|收据|合同)"
    r"[^。？?]{0,4}?(?:在|放|存)(?:了)?哪(?:里|儿)?"
    r"|在\s*哪(?:里|儿)?\s*[？?]?$"
)


def _is_path_question(text: str) -> bool:
    return bool(_PATH_INTENT.search(text or ""))


def _is_folder_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 40:
        return False
    return any(pattern.search(compact) for pattern in _FOLDER_INTENT)


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


_VERSION_INTENT = re.compile(r"(版本|终稿|初稿|定稿|修订版|第[一二三四五六七八九十\d]+版)")
_VERSION_ASK = re.compile(r"(哪些|几个|多少|列出|列表|区别|对比|不同)")


def _is_version_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return bool(_VERSION_INTENT.search(compact) and _VERSION_ASK.search(compact))


def _path_citations(matches: list[retrieval.PathMatch], limit: int) -> list[dict]:
    citations = []
    for index, match in enumerate(matches[:limit], start=1):
        name = match.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        citations.append({
            "n": index,
            "chunk_id": "",
            "asset_id": match.asset_id,
            "location_id": match.location_id,
            "name": name,
            "path": match.path,
            "page": None,
            "sheet": None,
            "snippet": "",
            "ocr": False,
            "semantic": False,
            "version_id": "",
            "mtime": None,
            "bbox": None,
            "family_id": None,
            "family_size": 1,
            "family_title": "",
            "version_label": "",
            "sources": [],
        })
    return citations


def _version_overview(conn: sqlite3.Connection, analysis: dict, *,
                      source_id: str | None, ext: str | None,
                      allowed_assets: set[str] | None = None,
                      excluded_assets: set[str] | None = None
                      ) -> tuple[str, list[retrieval.PathMatch]] | None:
    """按文件名列出同一主题的版本，不判断哪个更好。"""
    core = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (analysis.get("core") or "").lower())
    subject = re.sub(r"版本(?:有哪些|有哪几个|对比|区别|不同)", "", core)
    subject = re.sub(r"(?:有|哪)?(?:哪些|几个|多少)?(?:个)?版本", "", subject)
    subject = re.sub(r"(?:终稿|初稿|定稿|修订版)", "", subject).strip()
    if len(subject) < 2:
        return None
    matches = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)
    selected: list[retrieval.PathMatch] = []
    seen: set[str] = set()
    for match in matches.values():
        name = match.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.lower())
        if subject not in compact or not _VERSION_INTENT.search(name):
            continue
        if match.path in seen:
            continue
        seen.add(match.path)
        selected.append(match)
    if not selected:
        return None

    def version_key(match: retrieval.PathMatch) -> tuple[int, str]:
        name = match.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        numbers = [int(value) for value in re.findall(r"\d+", name)]
        return (numbers[-1] if numbers else 0, name.lower())

    selected.sort(key=version_key)
    lines = [f"文件名包含「{subject}」的版本共 {len(selected)} 个："]
    for index, match in enumerate(selected[:20], start=1):
        lines.append(f"{index}. `{match.path}`")
    if len(selected) > 20:
        lines.append(f"其余 {len(selected) - 20} 个未展开。")
    lines.append("这里只整理文件位置和名称，不判断哪个版本更好。")
    return "\n".join(lines), selected


def _locate_answer(conn: sqlite3.Connection, analysis: dict, *,
                   source_id: str | None, ext: str | None,
                   allowed_assets: set[str] | None = None,
                   excluded_assets: set[str] | None = None
                   ) -> tuple[str, list[retrieval.PathMatch]] | None:
    """位置问题直接列路径，避免让模型从正文里猜文件名。"""
    matches = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)
    candidates = [
        match for match in matches.values()
        if match.name_matched or match.specific or match.folder_specific
    ]
    if not candidates:
        return None
    # 专有名词（MISA、CBPD、课程表）比「脚本/目录」这类通用词更能定位文件。
    anchors = [
        concept["term"] for concept in analysis["concepts"]
        if len(concept["term"]) >= 2
        and not retrieval.is_generic_term(concept["term"])
    ]

    def literal_score(match: retrieval.PathMatch) -> int:
        lowered = match.path.lower()
        return sum(1 for term in anchors if term.lower() in lowered)

    candidates.sort(
        key=lambda match: (
            -literal_score(match),
            -(int(match.name_matched) * 1000 + int(match.specific) * 200
              + int(match.folder_specific) * 80 + match.score),
            match.path,
        ))
    selected: list[retrieval.PathMatch] = []
    seen: set[str] = set()
    for match in candidates:
        if match.path in seen:
            continue
        seen.add(match.path)
        selected.append(match)
        if len(selected) >= 5:
            break
    lines = ["找到以下位置："]
    for match in selected:
        lines.append(f"- `{match.path}`")
    lines.append("以上是检索到的原文件路径，未移动或修改文件。")
    return "\n".join(lines), selected


def _best_folder(conn: sqlite3.Connection, analysis: dict, *,
                 source_id: str | None, ext: str | None,
                 allowed_assets: set[str] | None = None,
                 excluded_assets: set[str] | None = None) -> retrieval.PathMatch | None:
    matches = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)
    core = re.sub(r"\s+", "", (analysis.get("core") or "")).lower()
    best: retrieval.PathMatch | None = None
    best_key: tuple[int, int, int, int] | None = None
    for match in matches.values():
        if not match.specific_folder_path or not match.specific_folder:
            continue
        folder = (match.specific_folder or "").lower()
        hit_len = 0
        for concept in analysis["concepts"]:
            for variant in concept["variants"]:
                if (len(variant) >= 2 and variant in folder
                        and not retrieval.is_generic_term(variant)):
                    hit_len = max(hit_len, len(variant))
        # 「大二有哪些课程」应回到「大二」，而不是名字里恰好连着「大二课程」的子目录。
        in_query = 1 if folder and folder in core else 0
        key = (in_query, hit_len, -len(folder), match.score)
        if best_key is None or key > best_key:
            best_key = key
            best = match
    return best


def _folder_overview(conn: sqlite3.Connection, match: retrieval.PathMatch) -> str | None:
    folder = (match.specific_folder_path or match.folder_path or "").rstrip("\\/")
    if not folder:
        return None
    prefix = folder + "\\"
    rows = conn.execute(
        "SELECT l.path FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        " WHERE l.availability = 'AVAILABLE' AND s.state = 'active'"
        " AND lower(l.path) LIKE lower(?) ESCAPE '\\'",
        (_like_prefix(prefix),),
    ).fetchall()
    total = len(rows)
    if total == 0:
        return None
    children: dict[str, int] = {}
    child_dirs: dict[str, set[str]] = {}
    child_files: dict[str, list[str]] = {}
    direct_names: list[str] = []
    for row in rows:
        relative = row["path"][len(prefix):]
        head, separator, rest = relative.partition("\\")
        if not separator:
            direct_names.append(head)
            continue
        children[head] = children.get(head, 0) + 1
        sub, sub_separator, _tail = rest.partition("\\")
        if sub_separator:
            child_dirs.setdefault(head, set()).add(sub)
        else:
            child_files.setdefault(head, []).append(sub)
    label = Path(folder).name or folder
    lines = [f"「{label}」位于 `{folder}`，共有 {total} 个可检索文件。"]
    if children:
        ordered = sorted(children.items(), key=lambda item: (-item[1], item[0]))
        lines.append(f"包含 {len(children)} 个子目录：")
        # 子目录很少时补一层内容，避免用户再追问一次「里面有什么」。
        expand = len(ordered) <= 8
        for name, count in ordered[:15]:
            lines.append(f"- {name}（{count} 个文件）")
            if not expand:
                continue
            dirs = sorted(child_dirs.get(name, ()))
            if dirs:
                shown = "、".join(dirs[:6])
                more = " 等" if len(dirs) > 6 else ""
                lines.append(f"  - 目录：{shown}{more}")
            files = sorted(child_files.get(name, ()))
            if files:
                shown = "、".join(files[:5])
                more = " 等" if len(files) > 5 else ""
                lines.append(f"  - 文件：{shown}{more}")
        if len(ordered) > 15:
            lines.append(f"- 另有 {len(ordered) - 15} 个子目录未列出")
    if direct_names:
        ordered_names = sorted(direct_names)
        shown = "、".join(ordered_names[:8])
        more = "…" if len(ordered_names) > 8 else ""
        lines.append(f"该目录下还有 {len(ordered_names)} 个直属文件：{shown}{more}")
    lines.append("以上为本地索引统计，可在检索页点开具体文件查看内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 表格统计

_AGG_INTENT = re.compile(
    r"(合计|总计|总额|总和|加总|求和|汇总|一共|总共|平均|均值|最大值|最小值"
    r"|花了|花销|花费|多少钱)")
_TABLE_ENTITY = re.compile(
    r"(单据|收据|发票|账单|明细|台账|流水|报表|报销|金额|支出|收入|成本|工资|费用|成绩|分数)")
_TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}


def _is_stat_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 30:
        return False
    return bool(_AGG_INTENT.search(compact)) and bool(_TABLE_ENTITY.search(compact))


def _sheet_names(conn: sqlite3.Connection, asset_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT sheet FROM chunks"
        " WHERE asset_id = ? AND sheet IS NOT NULL ORDER BY sheet",
        (asset_id,),
    ).fetchall()
    return [row["sheet"] for row in rows if row["sheet"]]


def _stats_answer(conn: sqlite3.Connection, question: str, *,
                  source_id: str | None, ext: str | None,
                  allowed_assets: set[str] | None = None,
                  excluded_assets: set[str] | None = None) -> dict | None:
    """表格统计问句：给出命中的表格与工作表，明确说明不做汇总。"""
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    matches = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)
    tables: list[retrieval.PathMatch] = []
    seen: set[str] = set()
    for match in sorted(matches.values(), key=lambda item: -item.score):
        if Path(match.path).suffix.lower() not in _TABLE_EXTS:
            continue
        key = Path(match.path).name.lower()
        if key in seen:
            continue
        seen.add(key)
        tables.append(match)
        if len(tables) >= 5:
            break
    if not tables:
        return None

    evidence: list[Evidence] = []
    lines = [
        "这是需要跨行汇总的统计问题（合计 / 平均 / 总额）。",
        "拾源目前只做检索与摘录，不会替你求和或求平均，以免给出错误数字。",
        "",
        "命中的表格：",
    ]
    for match in tables:
        name = Path(match.path).name
        sheets = _sheet_names(conn, match.asset_id)
        detail = f"（工作表 {'、'.join(sheets[:3])}）" if sheets else ""
        lines.append(f"- {name}{detail}")
        row = conn.execute(
            "SELECT c.chunk_id, c.text, c.page, c.sheet, c.version_id, l.mtime_ns"
            " FROM chunks c JOIN asset_locations l ON l.asset_id = c.asset_id"
            " WHERE c.asset_id = ? LIMIT 1",
            (match.asset_id,),
        ).fetchone()
        if row is None:
            continue
        evidence.append(Evidence(
            chunk_id=row["chunk_id"],
            asset_id=match.asset_id,
            location_id=match.location_id,
            path=match.path,
            name=name,
            text=(row["text"] or "")[:200],
            page=row["page"],
            sheet=row["sheet"],
            matches=0,
            version_id=row["version_id"] or "",
            mtime=_iso_mtime(row["mtime_ns"]),
        ))
    lines += [
        "",
        "建议：打开表格核对，或换个问法，例如「…里有哪些字段」「哪些…需要人工复核」。",
    ]
    return {
        "answer": "\n".join(lines),
        "citations": [item.citation(index) for index, item in enumerate(evidence, 1)],
    }


# ---------------------------------------------------------------- 多轮追问

_FOLLOWUP = re.compile(
    r"^(?:那|那么|然后|还有|另外|接着|所以|它|它们|这个|那个|该|上述|上面|刚才|前面|之前|继续)"
    r"|第[一二三四五六七八九十0-9]+(?:章|节|部分|页)"
    r"|呢[？?]?$"
)


def _context_base(history: list[str]) -> str:
    """最近一条不是追问的问题，作为追问的上下文锚点。"""
    for previous in history:
        compact = re.sub(r"\s+", "", previous or "")
        if compact and not _FOLLOWUP.search(compact):
            return previous
    return history[0]


_CHAPTER_REF = re.compile(r"第[一二三四五六七八九十百\d]+[章节部分篇]")


def _contextual_query(text: str, history: list[str] | None) -> str:
    """把「那第三章呢」这类追问补上前一轮问题，仅用于检索。"""
    if not history:
        return text
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 16:
        return text
    if _FOLLOWUP.search(compact):
        base = _context_base(history).strip()
        # 「第四章」→「那第三章呢」：替换章节指代，不能把两章混在一起检索。
        latest = _CHAPTER_REF.search(compact)
        if latest and _CHAPTER_REF.search(base):
            return _CHAPTER_REF.sub(latest.group(0), base)
        return base + " " + text
    # 「结果文件放在哪个目录」「数据是从哪里来的」这类短追问自身没有主题，
    # 需要补上一轮问题；否则「数据」会把数据结构教材全部召回。
    if len(compact) <= 12:
        if _is_path_question(text):
            return _context_base(history).strip() + " " + text
        core = re.sub(r"\s+", "", query.analyze(text)["core"] or "")
        if len(core) <= 3:
            return _context_base(history).strip() + " " + text
    return text


def _focus_terms(text: str) -> list[str]:
    """追问里本轮问题自己的关键词，排序时优先。"""
    analysis = query.analyze(text)
    return [term for term in analysis["variants"]
            if len(term) >= 2 and not retrieval.is_generic_term(term)]


# ---------------------------------------------------------------- 版本指代

_ORDINAL_WORDS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL_REF = re.compile(
    r"(?:版本|版|version|v)\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})(?![\d.])"
    r"|第\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*"
    r"(?:个(?!文件|文档|来源|引用)|版|篇|份|条)")
_VERSION_HEAD = re.compile(
    r"版本\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*\*{0,2}\s*[（(]\s*\**\s*"
    r"([^）)\n]{2,200}?)\s*[）)]")
_NUMBERED_LINE = re.compile(r"^\s*(?:(\d{1,2})[.、)）]|[-*•])\s*(.+?)\s*$")
_FILE_IN_TEXT = re.compile(
    r"([^\s`\"'（）()：:，,、]+?\.(?:docx?|pdf|pptx?|xlsx?|xls|md|txt|tsv|csv|py|zip|rar|7z))",
    re.IGNORECASE)
_SELECT_VERB = re.compile(r"(看|打开|读|选择|切换|就用|用一下|采用)")
_NOT_SELECT = re.compile(r"(区别|对比|不同|为什么|怎么|如何|多少|几个|哪些|什么|多大|大小|在哪|哪里)")


def _to_number(value: str) -> int | None:
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    if not value:
        return None
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _ORDINAL_WORDS.get(left, 1) if left else 1
        ones = _ORDINAL_WORDS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _ORDINAL_WORDS.get(value)


def _ordinal_ref(text: str) -> int | None:
    compact = re.sub(r"\s+", "", text or "")
    match = _ORDINAL_REF.search(compact)
    if match is None:
        return None
    return _to_number(match.group(1) or match.group(2))


def _is_selection_followup(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 14 or _NOT_SELECT.search(compact):
        return False
    return bool(_SELECT_VERB.search(compact)) or len(compact) <= 8


def _resolve_reference(text: str, previous_answer: str | None) -> str | None:
    """把「我们看版本2吧」解析成上一轮回答里列出的文件名或路径。"""
    if not previous_answer:
        return None
    ordinal = _ordinal_ref(text)
    if ordinal is None or not _is_selection_followup(text):
        return None

    # 标题式写法：版本二（Siganus_fuscescens_CBPD_clean_v0.12.docx）
    for match in _VERSION_HEAD.finditer(previous_answer):
        if _to_number(match.group(1)) != ordinal:
            continue
        body = match.group(2)
        ticked = re.search(r"`([^`]+)`", body)
        if ticked:
            return ticked.group(1).strip()
        found = _FILE_IN_TEXT.search(body)
        if found:
            return found.group(1)
        return body.strip()

    # 编号或项目符号列表：取第 N 项
    numbered: dict[int, str] = {}
    bullets: list[str] = []
    for line in previous_answer.splitlines():
        match = _NUMBERED_LINE.match(line)
        if match is None:
            continue
        if match.group(1):
            numbered.setdefault(int(match.group(1)), match.group(2))
        else:
            bullets.append(match.group(2))
    target = numbered.get(ordinal)
    if target is None and 0 < ordinal <= len(bullets):
        target = bullets[ordinal - 1]
    if not target:
        return None
    ticked = re.search(r"`([^`]+)`", target)
    if ticked:
        return ticked.group(1).strip()
    found = _FILE_IN_TEXT.search(target)
    return found.group(1) if found else target.strip().strip("`")


def _selected_version_answer(conn: sqlite3.Connection, reference: str, *,
                             source_id: str | None
                             ) -> tuple[str, list[Evidence]] | None:
    """用户点名要看某一版：给出路径、时间、正文规模和开头预览。"""
    name = reference.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].strip()
    if not name:
        return None
    sql = [
        "SELECT l.location_id, l.asset_id, l.path, l.size, l.mtime_ns",
        "FROM asset_locations l JOIN sources s ON s.source_id = l.source_id",
        "WHERE l.availability = 'AVAILABLE' AND s.state = 'active'",
    ]
    params: list = []
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    sql.append("AND (l.path = ? OR lower(l.path) LIKE lower(?))")
    params.extend([reference, f"%{name}"])
    row = None
    for candidate in conn.execute(" ".join(sql), params):
        candidate_name = candidate["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if candidate_name.lower() == name.lower():
            row = candidate
            break
    if row is None:
        return None

    version = conn.execute(
        "SELECT version_id FROM content_versions WHERE asset_id = ? AND is_current = 1"
        " ORDER BY observed_at DESC LIMIT 1", (row["asset_id"],)).fetchone()
    chunks: list[sqlite3.Row] = []
    total_chunks = total_chars = 0
    if version is not None:
        total_chunks, total_chars = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM chunks"
            " WHERE version_id = ?", (version["version_id"],)).fetchone()
        chunks = conn.execute(
            "SELECT chunk_id, text, page, sheet FROM chunks WHERE version_id = ?"
            " ORDER BY (page IS NULL), page, sheet, ordinal LIMIT 40",
            (version["version_id"],)).fetchall()

    preview_parts: list[str] = []
    evidence: list[Evidence] = []
    for chunk in chunks:
        text = (chunk["text"] or "").strip()
        if not text:
            continue
        if len("".join(preview_parts)) < 420:
            preview_parts.append(text)
        if not evidence:
            evidence.append(Evidence(
                chunk_id=chunk["chunk_id"], asset_id=row["asset_id"],
                location_id=row["location_id"], path=row["path"], name=name,
                text=text, page=chunk["page"], sheet=chunk["sheet"], matches=0,
                version_id=version["version_id"] if version else "",
                mtime=(datetime.fromtimestamp(row["mtime_ns"] / 1e9)
                       .strftime("%Y-%m-%dT%H:%M") if row["mtime_ns"] else None),
            ))
    if not evidence:
        evidence.append(Evidence(
            chunk_id="", asset_id=row["asset_id"], location_id=row["location_id"],
            path=row["path"], name=name, text="", page=None, sheet=None, matches=0,
            version_id=version["version_id"] if version else "",
            mtime=(datetime.fromtimestamp(row["mtime_ns"] / 1e9)
                   .strftime("%Y-%m-%dT%H:%M") if row["mtime_ns"] else None),
        ))

    lines = [f"你选的是「{name}」：", f"`{row['path']}`"]
    size_mb = (row["size"] or 0) / 1024 / 1024
    updated = (datetime.fromtimestamp(row["mtime_ns"] / 1e9).strftime("%Y-%m-%d %H:%M")
               if row["mtime_ns"] else "未知")
    lines.append(f"- 大小：{size_mb:.1f} MB，更新于 {updated}")
    if version is not None:
        lines.append(f"- 正文：已索引 {total_chars} 字，共 {total_chunks} 个片段")
    else:
        lines.append("- 正文：还没有可读的解析结果，请先在桌面端完成解析")
    preview = "\n".join(preview_parts).strip()
    if preview:
        preview = " ".join(preview.split())
        if len(preview) > 420:
            preview = preview[:420] + "…"
        lines.append(f"\n开头预览：{preview}")
    return "\n".join(lines), evidence


def _library_overview(conn: sqlite3.Connection, *, source_id: str | None,
                      ext: str | None) -> str:
    clauses = ["l.availability = 'AVAILABLE'", "s.state = 'active'"]
    params: list = []
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    where = " AND ".join(clauses)
    total = conn.execute(
        "SELECT COUNT(*) FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where}", params,
    ).fetchone()[0]
    by_ext = conn.execute(
        "SELECT l.ext AS ext, COUNT(*) AS n FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where} GROUP BY l.ext ORDER BY n DESC LIMIT 8", params,
    ).fetchall()
    source_rows = conn.execute(
        "SELECT s.label,"
        " (SELECT COUNT(*) FROM asset_locations l WHERE l.source_id = s.source_id"
        "  AND l.availability = 'AVAILABLE') AS n"
        " FROM sources s WHERE s.state = 'active' ORDER BY s.created_at"
    ).fetchall()
    coverage = indexer.coverage(conn)
    filtered = bool(source_id or ext)
    lines = [f"当前知识库共有 {total} 个可检索文件{'（按当前筛选）' if filtered else ''}。"]
    if not filtered and source_rows:
        lines.append("数据源：" + "；".join(
            f"{row['label']}（{row['n']} 个）" for row in source_rows
        ))
    if by_ext:
        lines.append("文件类型：" + "、".join(
            f"{row['ext'] or '无扩展名'} {row['n']}" for row in by_ext
        ))
    lines.append(
        f"正文索引：已解析 {coverage['indexed']}，待解析 {coverage['pending']}，"
        f"无文本层 {coverage['empty']}，失败 {coverage['failed']}，"
        f"仅登记（不解压）{coverage['metadata_only']}，"
        f"暂不支持 {coverage['unsupported']}。"
    )
    lines.append(f"OCR：已识别 {coverage['ocr_indexed']}，待 OCR {coverage['ocr_pending']}。")
    if filtered:
        lines.append("正文与 OCR 数字为全库统计。")
    lines.append("这是本地索引统计，不是本次检索到的证据条数。")
    return "\n".join(lines)


def _merge_evidence(left: list[Evidence], right: list[Evidence],
                    limit: int) -> list[Evidence]:
    """分级扩展时保留已有（更靠近项目）的证据，再补新范围内的证据。"""
    merged = list(left)
    seen = {item.chunk_id for item in merged}
    for item in right:
        if item.chunk_id in seen:
            continue
        merged.append(item)
        seen.add(item.chunk_id)
        if len(merged) >= limit:
            break
    return merged


def _ask(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
         ext: str | None = None, top_k: int | None = None,
         history: list[str] | None = None,
         anchor_sources: list[str] | None = None,
         previous_answer: str | None = None,
         project_id: str | None = None,
         scope: str | None = None,
         scope_info: dict | None = None,
         on_delta=None) -> dict:
    text = (question or "").strip()
    if not text:
        raise ValueError("问题不能为空")
    settings = load_settings(conn)
    limit = top_k or settings["top_k"]
    coverage = _coverage(conn)
    provider = settings["provider"]
    context_text = _contextual_query(text, history)
    previous = _context_base(history) if history and context_text != text else None
    # 问题里点名了数据源（如「实验记录里…」）就先限定范围，避免被其他库的文档抢答。
    source_hint: str | None = None
    if source_id is None:
        named = _named_source(conn, context_text)
        if named is not None:
            source_id, source_hint = named
    # 追问已经通过 _contextual_query 补全了主题词，不再按上一轮数据源强制过滤：
    # FTS 在“MATCH + source_id”组合下为了凑满候选会扫描全库，单个追问可达一分钟。
    effective_source = source_id

    # 项目检索范围：project（主资料）→ expanded（主资料 + 关联）→ global。
    # 本地快答分支先用当前范围过滤；证据不足时由下面的检索循环逐级扩大。
    requested_scope = projects.scope_for(conn, project_id, scope)
    if scope_info is not None:
        scope_info.update({"requested": requested_scope, "used": requested_scope,
                           "expanded": False, "project_id": project_id})
    active_allowed, active_excluded = projects.asset_filter(
        conn, project_id, requested_scope)
    primary_allowed, _ = projects.asset_filter(conn, project_id, "project")

    if previous_answer:
        reference = _resolve_reference(text, previous_answer)
        if reference:
            selected = _selected_version_answer(
                conn, reference, source_id=effective_source)
            if selected is not None:
                selected_text, selected_evidence = selected
                citations = [item.citation(index) for index, item
                             in enumerate(selected_evidence, start=1)]
                return {
                    "question": text,
                    "answer": selected_text,
                    "refused": False,
                    "reason": "version_selected",
                    "provider": "library",
                    "model": None,
                    "citations": citations,
                    "evidence_count": len(citations),
                    "warning": None,
                    "coverage": coverage,
                    "context_used": previous,
                }
            if re.search(r"\.[a-z0-9]{2,5}$", reference, re.IGNORECASE):
                return {
                    "question": text,
                    "answer": (f"上一轮列出的「{reference}」没有在本地索引里找到，"
                               "可能已移动、改名或还没解析。可以让我按主题重新检索。"),
                    "refused": False,
                    "reason": "version_missing",
                    "provider": "library",
                    "model": None,
                    "citations": [],
                    "evidence_count": 0,
                    "warning": None,
                    "coverage": coverage,
                    "context_used": previous,
                }
    anchor_prefix: str | None = None
    if (previous and not source_id and anchor_sources
            and len(anchor_sources) == 1):
        row = conn.execute(
            "SELECT root_uri FROM sources WHERE source_id = ?",
            (anchor_sources[0],)).fetchone()
        if row is not None and row["root_uri"]:
            anchor_prefix = row["root_uri"].rstrip("\\/") + "\\"

    if _is_source_overview(text):
        return {
            "question": text,
            "answer": _source_overview_answer(conn),
            "refused": False,
            "reason": "source_overview",
            "provider": "library",
            "model": None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    if _is_version_question(text):
        version_analysis = query.analyze(context_text, synonyms=query.synonym_map(conn))
        overview = _version_overview(
            conn, version_analysis, source_id=effective_source, ext=ext,
            allowed_assets=active_allowed, excluded_assets=active_excluded)
        if overview:
            overview_text, version_matches = overview
            citations = _path_citations(version_matches, 20)
            return {
                "question": text,
                "answer": overview_text,
                "refused": False,
                "reason": "version_overview",
                "provider": "library",
                "model": None,
                "citations": citations,
                "evidence_count": len(citations),
                "warning": None,
                "coverage": coverage,
                "context_used": previous,
            }

    if _is_path_question(text):
        path_analysis = query.analyze(context_text, synonyms=query.synonym_map(conn))
        located = _locate_answer(
            conn, path_analysis, source_id=effective_source, ext=ext,
            allowed_assets=active_allowed, excluded_assets=active_excluded)
        if located:
            located_text, located_matches = located
            citations = _path_citations(located_matches, 5)
            return {
                "question": text,
                "answer": located_text,
                "refused": False,
                "reason": "path_location",
                "provider": "library",
                "model": None,
                "citations": citations,
                "evidence_count": len(citations),
                "warning": None,
                "coverage": coverage,
                "context_used": previous,
            }

    if _is_library_overview(text):
        return {
            "question": text,
            "answer": _library_overview(conn, source_id=source_id, ext=ext),
            "refused": False,
            "reason": "library_overview",
            "provider": "library",
            "model": None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    matched_source = _match_source(conn, text, source_id=source_id)
    if matched_source is not None:
        return {
            "question": text,
            "answer": _source_recall_answer(conn, matched_source),
            "refused": False,
            "reason": "source_recall",
            "provider": "library",
            "model": None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    if _is_folder_question(text):
        analysis = query.analyze(context_text, synonyms=query.synonym_map(conn))
        folder_match = _best_folder(
            conn, analysis, source_id=effective_source, ext=ext,
            allowed_assets=active_allowed, excluded_assets=active_excluded)
        if folder_match is not None:
            overview = _folder_overview(conn, folder_match)
            if overview:
                return {
                    "question": text,
                    "answer": overview,
                    "refused": False,
                    "reason": "folder_overview",
                    "provider": "library",
                    "model": None,
                    "citations": [],
                    "evidence_count": 0,
                    "warning": None,
                    "coverage": coverage,
                    "context_used": previous,
                }

    if _is_stat_question(text):
        stats_hint = _stats_answer(
            conn, context_text, source_id=effective_source, ext=ext,
            allowed_assets=active_allowed, excluded_assets=active_excluded)
        if stats_hint is not None:
            return {
                "question": text,
                "answer": stats_hint["answer"],
                "refused": False,
                "reason": "table_stats_hint",
                "provider": "library",
                "model": None,
                "citations": stats_hint["citations"],
                "evidence_count": len(stats_hint["citations"]),
                "warning": None,
                "coverage": coverage,
                "context_used": previous,
            }

    plan = planner.make(context_text)
    budget = limit if top_k else plan.mode.budget
    path_question = _is_path_question(text)
    scope_chain = ({"project": ["project", "expanded", "global"],
                    "expanded": ["expanded", "global"],
                    "global": ["global"]}[requested_scope]
                   if project_id else ["global"])
    evidence: list[Evidence] = []
    plan_stats: dict = {}
    used_scope = "global" if not project_id else requested_scope
    for candidate_scope in scope_chain:
        allowed, excluded = projects.asset_filter(conn, project_id, candidate_scope)
        attempt_stats: dict = {}
        attempt = retrieve(
            conn, context_text, source_id=effective_source, ext=ext,
            limit=budget,
            focus=_focus_terms(text) if previous else None,
            path_only=path_question,
            plan=plan, stats=attempt_stats,
            anchor_prefix=anchor_prefix,
            allowed_assets=allowed, excluded_assets=excluded,
            priority_assets=(primary_allowed if candidate_scope == "expanded" else None))
        plan_stats = attempt_stats
        merged = _merge_evidence(evidence, attempt, budget)
        # 只有真正补到新证据时才把 used 记为更外层的范围；
        # 外层没有新增内容时仍显示为项目范围，避免误导用户。
        if len(merged) > len(evidence) or not evidence:
            used_scope = candidate_scope
        evidence = merged
        minimum = 2 if candidate_scope == "project" else 1
        if len(evidence) >= minimum:
            break
    if scope_info is not None:
        scope_info.update({"used": used_scope,
                           "expanded": used_scope != requested_scope})
    plan_payload = {**plan.as_dict(), **plan_stats}
    plan_note = _plan_note(plan, plan_stats)

    if not evidence:
        indexed = coverage["content_indexed"]
        pending = coverage["content_pending"]
        scope = f"在「{source_hint}」中" if source_hint else ""
        answer = f"{scope}没有找到足够证据回答这个问题。当前正文索引覆盖 {indexed} 个文件"
        if pending:
            answer += f"，另有 {pending} 个文件待解析"
        answer += "；扫描件需要 OCR（L2）后才能检索。"
        return {
            "question": text,
            "answer": answer,
            "refused": True,
            "reason": "no_evidence",
            "provider": provider,
            "model": settings.get("model") or None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
            "plan": plan_payload,
        }

    if provider == "extractive":
        answer = _extractive_answer(evidence)
        if on_delta is not None:
            on_delta(answer)
        warning = None
    elif provider == "openai":
        answer = _openai_answer(settings, text, evidence, previous, plan_note,
                                include_path=path_question, on_delta=on_delta,
                                previous_answer=previous_answer if previous else None)
        warning = _citation_warning(answer, len(evidence))
    else:
        answer = _ollama_answer(settings, text, evidence, previous, plan_note,
                                include_path=path_question, on_delta=on_delta,
                                previous_answer=previous_answer if previous else None)
        warning = _citation_warning(answer, len(evidence))

    if not answer:
        raise AnswerError("模型没有返回任何内容")
    refused = _looks_like_refusal(answer)
    citations = [item.citation(index) for index, item in enumerate(evidence, start=1)]
    return {
        "question": text,
        "answer": answer,
        "refused": refused,
        "reason": "insufficient_evidence" if refused else None,
        "provider": provider,
        "model": settings.get("model") or None,
        "citations": citations,
        "evidence_count": len(evidence),
        "warning": warning,
        "coverage": coverage,
        "context_used": previous,
        "plan": plan_payload,
    }


def ask(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
        ext: str | None = None, top_k: int | None = None,
        history: list[str] | None = None,
        anchor_sources: list[str] | None = None,
        previous_answer: str | None = None,
        project_id: str | None = None,
        scope: str | None = None,
        on_delta=None) -> dict:
    """问答入口：附加本次实际使用的检索范围（项目 / 扩展 / 全局）。"""
    scope_info: dict = {}
    result = _ask(
        conn, question, source_id=source_id, ext=ext, top_k=top_k,
        history=history, anchor_sources=anchor_sources,
        previous_answer=previous_answer, project_id=project_id, scope=scope,
        scope_info=scope_info, on_delta=on_delta)
    if scope_info:
        result["scope"] = scope_info
    return result
