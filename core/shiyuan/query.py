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
    # 疑问代词与泛动词：留下会把「什么/哪些」当正文关键词，召回大量无关文档。
    "哪些", "哪个", "哪几", "多少", "几个", "什么", "怎么", "如何", "为什么",
    "写", "写了", "做", "做了", "跑", "跑了", "说", "讲", "看", "介绍", "总结", "概括",
}

# 英文虚词与高频词：都是子串匹配的噪声源（如 to 命中 photo、photoshop），
# 单独出现时几乎不携带检索意图。
STOP_TERMS_EN = {
    "a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "with", "from",
    "and", "or", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "this", "that", "these", "those", "as", "if", "do", "does", "did", "done",
    "how", "what", "which", "who", "whom", "whose", "when", "where", "why",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "not", "no", "yes", "you", "your", "yours", "we", "our", "ours", "they",
    "their", "them", "he", "she", "his", "her", "him", "me", "my", "us", "i",
    "am", "so", "than", "then", "there", "here", "about", "into", "onto", "over",
    "under", "between", "via", "per", "use", "uses", "using", "used", "have",
    "has", "had", "also", "more", "most", "some", "any", "all", "each", "other",
    "one", "two", "first", "second", "hi", "hello",
}

PREFIXES = ("请问", "帮我找", "帮我查", "帮我", "麻烦", "我想找", "我想查",
            "我想看看", "我想知道", "我想问", "你还记得", "你记得", "还记得",
            "那个", "这个", "请找")
TAILS = ("是多少呢", "是多少", "是什么", "有哪些", "有哪几个", "怎么样", "怎么办",
         "为什么", "如何", "吗", "呢", "吧", "？", "?")

# 疑问句中间的口语结构：只去掉疑问部分，保留真正的主体词。
# 例如「专利里写了什么」→「专利里」，「MISA 跑了什么参数」→「MISA 参数」。
QUESTION_PHRASES = (
    re.compile(
        r"(?:有|包含|包括|收录|记录|提到|写|讲|说|做|跑|用|干|搞|学|修|看|列|放|存|画|整|拍|录)"
        r"(?:了|过)?(?:些)?(?:什么|哪些|啥)"
    ),
    re.compile(r"(?:怎么|怎样|如何)(?:用|做|弄|操作|使用|处理|配置|设置|看|查)?"),
    # 来源问句：「数据是从哪里来的」整体去掉，别把「从/来」留成检索词。
    re.compile(r"(?:是)?从哪(?:里|儿)?(?:来|来的|得来)?"),
    # 位置问句要整体去掉，否则「文件/放在」会变成通用检索词。
    re.compile(r"(?:放在|存放|保存|存在|放到|存到|保存在)(?:了)?哪(?:个)?(?:文件夹|目录|盘)?"),
    re.compile(r"(?:在)?哪(?:个)?(?:文件夹|目录|盘)(?:里|中|内)?"),
    re.compile(r"(?:是|有)(?:什么|谁|哪里|哪儿|哪个|哪几种?)"),
    re.compile(r"哪(?:些|个|几|里|儿)"),
    re.compile(r"为什么"),
    re.compile(r"多少(?:个|份|篇|条|次|张)?"),
    re.compile(r"(?:里面?|中|内)都"),
    re.compile(r"在?哪(?:里|儿)?$"),
)

# 句尾方位词不参与检索：「专利里」→「专利」。
_LOCATIVE_TAIL = re.compile(r"(?:里面?|里边?|中|内)$")

# 「怎么用 / 如何配置 / 使用教程」这类问题优先给说明类文档。
_HOWTO = re.compile(
    r"(?:怎么|怎样|如何|咋)(?:用|使用|操作|配置|设置|安装|运行|启动|跑|做|弄|整理|归档|处理|管理|分类)"
    r"|(?:使用说明|操作说明|快速上手|快速开始|使用手册|入门教程|教程|指南)"
)

# 「X 是做什么的 / 有什么用」优先给说明类文档。
_INTRO = re.compile(r"(?:是|做|干)(?:什么|啥|嘛)(?:的)?|干什么|有什么用|介绍一下|介绍下")


def _strip_locative(value: str) -> str:
    while len(value) > 2:
        trimmed = _LOCATIVE_TAIL.sub("", value)
        if trimmed == value:
            break
        value = trimmed
    return value


def _remove_question_phrases(value: str) -> str:
    """去掉句子中间的疑问结构，保留主体词。"""
    for pattern in QUESTION_PHRASES:
        value = pattern.sub("", value)
    value = " ".join(value.split())
    return _strip_locative(value.strip("，,。.；;：:！!？?、 "))

SYNONYMS = {
    "样本量": ["样本数", "样本数量", "case数", "sample size"],
    "样本数": ["样本量", "样本数量"],
    "样本": ["sample"],
    "测序": ["sequencing", "测序数据"],
    "阈值": ["threshold", "cutoff", "临界值"],
    "实验记录": ["实验日志", "实验笔记", "lab notebook"],
    "质控": ["质量控制", "quality control", "qc"],
    "差异表达": ["deg", "differential expression"],
    "蛋白": ["protein", "蛋白质"],
    "论文": ["paper", "文献"],
    "文库": ["library", "建库"],
    "滑窗": ["sliding", "sliding window", "滑动窗口"],
    "窗口": ["window"],
    "共线性": ["collinearity", "synteny", "共线"],
    # 语料里的文件名多为英文，问题多为中文：补一批科研场景名词，
    # 让「结果文件」「权重」「染色体」这类词也能命中英文文件名。
    # 注意只映射「结果文件」而不是宽泛的「结果」，否则任何带 result 的
    # 输出文件都会盖过「滑窗分析结果」这类真正的主题。
    "结果文件": ["result", "results", "output"],
    "权重": ["weight"],
    "回归": ["regression"],
    "异方差": ["heteroskedasticity", "heteroscedasticity"],
    "面板数据": ["panel"],
    "染色体": ["chromosome"],
    "长度": ["length"],
    "基序": ["motif"],
    "注释": ["annotation", "annot"],
    "基因组": ["genome"],
    "组装": ["assembly"],
    "深度": ["depth"],
    "亲本": ["parent"],
    "筛选": ["filter"],
    "鉴定": ["identify", "identification"],
    "比对": ["alignment", "align"],
    "参数": ["parameter", "param"],
    "脚本": ["script"],
    "微卫星": ["microsatellite", "ssr"],
    "分子标记": ["marker", "markers"],
    "引物": ["primer"],
    "序列": ["sequence"],
    "富集": ["enrichment"],
    "通路": ["pathway"],
    "突变": ["mutation", "variant"],
    "群体": ["population"],
    "遗传": ["genetic"],
    "多样性": ["diversity"],
    "性别决定": ["sex determination", "sdr"],
    "雌雄": ["性别", "sex"],
    "混池": ["pool", "pooled"],
    "异常": ["abnormal", "exception", "error"],
    "单据": ["receipt", "document", "invoice"],
    # 文件名多为英文、问题多为中文：补一批“中文说法 → 英文文件名”的映射。
    "单据识别": ["receipt recognition", "receipt ocr", "document extraction"],
    "提示词": ["prompt", "prompts", "prompt template"],
    "封面信": ["cover letter", "cover_letter"],
    "投稿信": ["cover letter", "cover_letter"],
    "投稿": ["submission", "submit"],
    "完美标记": ["perfect marker", "perfect markers", "perfect_markers"],
    "标记": ["marker", "markers"],
    "引物设计": ["primer design", "primer"],
    "结果表": ["result table", "result_table", "results table"],
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
    core = _remove_question_phrases(strip_question(normalized))
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
        if word in STOP_TERMS_EN:
            continue
        add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", core):
        # 「的」通常只是连接词，按它切开可避免「鱼的候选」这类跨词 gram。
        for part in re.split("的", run):
            part = _strip_locative(part)
            if not part:
                continue
            if len(part) <= 6:
                add(part)
            for size in (2, 3):
                for index in range(len(part) - size + 1):
                    add(part[index:index + size])

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
        "howto": bool(_HOWTO.search(normalized)),
        "intro": bool(_INTRO.search(normalized)),
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


def maximal_terms(analysis: dict) -> list[dict]:
    """去掉被更长词包含的 gram，只留语义单元。"""
    concepts = analysis["concepts"]
    terms = [concept["term"] for concept in concepts]
    return [
        concept for concept in concepts
        if not any(concept["term"] != other and concept["term"] in other
                   for other in terms)
    ]


def minimum_concepts(analysis: dict) -> int:
    """按“最大词”而不是全部 gram 计算门槛。

    「专利的创新点」会生成 专利/创新点/创新/新点 四个 gram，但它们其实只有两个
    语义单元；若按 4 个算，必须同时命中两个词才算证据，专利文档反而被过滤掉。
    """
    return 1 if len(maximal_terms(analysis)) <= 2 else 2


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
