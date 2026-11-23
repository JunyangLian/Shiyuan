"""检索规划：先判断问题要什么，再决定取多少证据。

四类模式（规则优先，不调用模型）：

| 模式 | 典型问题 | 预算 | 目标 |
| --- | --- | --- | --- |
| precision | 参数是多少、用哪个软件 | 4 | 少量高置信证据 |
| explain | 介绍一下、原理是什么 | 6 | 证据够用即可 |
| comprehensive | 做过哪些研究、整体梳理 | 10 | 跨家族覆盖 + 去重 |
| history | 改了什么、版本差异 | 6 | 保留版本来源 |

规划器只产出计划，不负责检索；answer 按计划执行候选选择。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import query, retrieval


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    budget: int
    per_family: int
    per_asset: int
    min_families: int
    output_mode: str


MODES: dict[str, Mode] = {
    "precision": Mode("precision", "精确查找", 4, 1, 1, 1,
                      "direct_answer_with_citations"),
    "explain": Mode("explain", "知识解释", 6, 2, 1, 2,
                    "explanation_with_citations"),
    "comprehensive": Mode("comprehensive", "综合探索", 10, 2, 1, 5,
                          "topic_grouped_summary"),
    "history": Mode("history", "历史追溯", 6, 2, 1, 1,
                    "version_changes_with_sources"),
}

_HISTORY = re.compile(
    r"(修改|改动|变更|变化|差异|对比|改了|更新了|之前版本|历史版本|不同版本|版本之间)")
_COMPREHENSIVE = re.compile(
    r"(哪些研究|做过哪些|都做过|都有哪些|整体|综合|梳理|总结一下|归纳|研究方向|研究主题"
    r"|历届|概述|全貌|进展|包含哪些|涉及哪些)"
    r"|(?:实验室|课题组|团队|我们).{0,6}(?:做过|研究|方向|项目)")
_PRECISION = re.compile(
    r"(参数|数值|是多少|多少|哪个软件|哪种软件|什么软件|版本号|日期|作者"
    r"|基因名|编号|阈值|浓度|剂量|比例|哪一年|什么时候|是谁)")
_EXPLAIN = re.compile(r"(介绍|是什么|原理|机制|区别|为什么|怎么|如何|解释|含义|作用)")


@dataclass
class Plan:
    mode: Mode
    facets: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "intent": self.mode.key,
            "label": self.mode.label,
            "budget": self.mode.budget,
            "per_family": self.mode.per_family,
            "per_asset": self.mode.per_asset,
            "min_families": self.mode.min_families,
            "output_mode": self.mode.output_mode,
            "deduplicate_by": ["document_family", "content_similarity"],
            "required_coverage": self.facets,
            "reason": self.reason,
        }


def _facet_terms(analysis: dict) -> list[str]:
    """从清理后的查询里取「整词」，不把 2/3-gram 的滑窗碎片当覆盖目标。"""
    core = analysis.get("core") or ""
    terms: list[str] = []
    ascii_terms: list[str] = []
    for part in re.split(r"[，,。;；:：!?！？/、]+", core):
        for chunk in re.split(r"\s+|的", part):
            chunk = re.sub(r"(?:里面?|里边?|中|内)$", "", chunk.strip())
            if not (2 <= len(chunk) <= 6):
                continue
            if chunk in query.STOP_TERMS or retrieval.is_generic_term(chunk):
                continue
            bucket = ascii_terms if chunk.isascii() else terms
            if chunk not in bucket:
                bucket.append(chunk)
    ordered = ascii_terms + terms
    return ordered[:4]


def make(question: str, analysis: dict | None = None) -> Plan:
    """按问题措辞选择检索模式；无法判断时按解释型处理。"""
    text = re.sub(r"\s+", "", question or "")
    analysis = analysis or query.analyze(question or "")
    mode, reason = MODES["explain"], "默认按解释型问题处理"
    if _HISTORY.search(text):
        mode, reason = MODES["history"], "问题在问版本之间的变化"
    elif _COMPREHENSIVE.search(text):
        mode, reason = MODES["comprehensive"], "问题需要跨资料归纳"
    elif _PRECISION.search(text):
        mode, reason = MODES["precision"], "问题在找一个具体事实"

    return Plan(mode=mode, facets=_facet_terms(analysis), reason=reason)
