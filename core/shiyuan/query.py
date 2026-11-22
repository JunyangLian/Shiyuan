"""查询理解：归一化、剥离疑问句式、中文 n-gram 与小型同义词扩展。

不做"大模型改写"，只用确定性规则与内置词表：
- 问句尾部的「是多少 / 是什么」等会被去掉，避免把疑问词当成检索词；
- 中文按 2-gram / 3-gram 展开，英文保留单词；
- 同义词表只覆盖科研场景的高频说法，未收录时不影响原有召回。
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata

STOP_TERMS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "有", "我", "你", "他", "她", "它",
    "这", "那", "哪", "什么", "怎么", "怎样", "如何", "多少", "一下", "帮我", "请问",
    "关于", "以及", "吗", "呢", "吧", "的是", "是多少", "是什么",
    # 对话式追问里的口头语，不应作为检索词。
    "你还", "还记", "记得", "得那", "那个", "这个", "这些", "那些",
    "个单", "据助", "我想", "想看", "看看", "知道", "的单",
    "里有", "有什", "里有什", "有什么", "里有什么", "里面有什么",
}

PREFIXES = ("请问", "帮我找", "帮我查", "帮我", "麻烦", "我想找", "我想查",
            "我想看看", "我想知道", "我想问", "你还记得", "你记得", "还记得",
            "那个", "这个", "请找")
TAILS = ("是多少呢", "是多少", "是什么", "有哪些", "有哪几个", "怎么样", "怎么办",
         "为什么", "如何", "吗", "呢", "吧", "？", "?")

SYNONYMS = {
    "样本量": ["样本数", "样本数量", "case数"],
    "样本数": ["样本量", "样本数量"],
    "测序": ["sequencing", "测序数据"],
    "阈值": ["threshold", "cutoff", "临界值"],
    "实验记录": ["实验日志", "实验笔记", "lab notebook"],
    "质控": ["质量控制", "quality control", "qc"],
    "差异表达": ["deg", "differential expression"],
    "蛋白": ["protein", "蛋白质"],
    "论文": ["paper", "文献"],
    "文库": ["library", "建库"],
}

MAX_CONCEPTS = 24
SETTINGS_KEY = "query"
MAX_CUSTOM_GROUPS = 50
MAX_TERM_LEN = 32


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def strip_question(text: str) -> str:
    value = text.strip()
    changed = True
    while changed:
        changed = False
        for prefix in PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix):].lstrip("，,、 ")
                changed = True
    value = value.strip("，,。.；;：:！!？?、 ")
    while True:
        for tail in sorted(TAILS, key=len, reverse=True):
            if value.endswith(tail) and value != tail:
                value = value[: -len(tail)].strip("，,。.；;：:！!？?、 ")
                break
        else:
            break
    return value


def analyze(text: str, synonyms: dict[str, list[str]] | None = None) -> dict:
    normalized = normalize(text)
    core = strip_question(normalized)
    table = synonyms if synonyms is not None else SYNONYMS

    seen: set[str] = set()
    concepts: list[dict] = []

    def add(term: str) -> None:
        term = term.strip()
        if not term or term in seen or term in STOP_TERMS or len(concepts) >= MAX_CONCEPTS:
            return
        seen.add(term)
        variants = [term]
        for extra in table.get(term, ()):
            if extra not in variants:
                variants.append(extra)
        concepts.append({"term": term, "variants": variants})

    for word in re.findall(r"[a-z0-9_]{2,}", core):
        add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", core):
        if len(run) <= 6:
            add(run)
        for size in (2, 3):
            for index in range(len(run) - size + 1):
                add(run[index:index + size])

    variants: list[str] = []
    for concept in concepts:
        for variant in concept["variants"]:
            if variant not in variants:
                variants.append(variant)
    return {
        "normalized": normalized,
        "core": core,
        "concepts": concepts,
        "variants": variants[:48],
    }


def rank(text: str, analysis: dict) -> tuple[int, int]:
    """返回（命中的概念数，命中的变体数），用于候选排序。"""
    lowered = text.lower()
    concepts_hit = 0
    variants_hit = 0
    for concept in analysis["concepts"]:
        hit = False
        for variant in concept["variants"]:
            if variant in lowered:
                variants_hit += 1
                hit = True
        if hit:
            concepts_hit += 1
    return concepts_hit, variants_hit


def minimum_concepts(analysis: dict) -> int:
    count = len(analysis["concepts"])
    return 1 if count <= 2 else 2


# ---------------------------------------------------------------- 自定义同义词

def _clean_groups(raw: object) -> list[list[str]]:
    """把用户提交的二维数组整理成合法的同义词组。

    每组至少 2 个词；空词丢弃；词长上限 MAX_TERM_LEN；最多保留 MAX_CUSTOM_GROUPS 组。
    """
    if not isinstance(raw, list):
        raise ValueError("同义词必须是「组」的数组")
    groups: list[list[str]] = []
    for item in raw[:MAX_CUSTOM_GROUPS]:
        if not isinstance(item, list):
            raise ValueError("每个同义词组必须是字符串数组")
        terms: list[str] = []
        for term in item:
            if not isinstance(term, str):
                raise ValueError("同义词必须是字符串")
            value = normalize(term).strip()
            if not value:
                continue
            if len(value) > MAX_TERM_LEN:
                raise ValueError(f"同义词过长：{value[:8]}…（上限 {MAX_TERM_LEN} 字符）")
            if value not in terms:
                terms.append(value)
        if len(terms) >= 2:
            groups.append(terms)
    return groups


def load_custom(conn: sqlite3.Connection) -> list[list[str]]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return []
    try:
        stored = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    groups = stored.get("custom") if isinstance(stored, dict) else None
    try:
        return _clean_groups(groups or [])
    except ValueError:
        return []


def public_settings(conn: sqlite3.Connection) -> dict:
    return {"custom": load_custom(conn), "builtin_groups": len(SYNONYMS)}


def update_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    if payload.get("custom") is None:
        return public_settings(conn)
    groups = _clean_groups(payload["custom"])
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps({"custom": groups}, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


def synonym_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """内置同义词 + 用户自定义组，组内两两互指。"""
    table = {term: list(extras) for term, extras in SYNONYMS.items()}
    for group in load_custom(conn):
        for term in group:
            extras = table.setdefault(term, [])
            for other in group:
                if other != term and other not in extras:
                    extras.append(other)
    return table
