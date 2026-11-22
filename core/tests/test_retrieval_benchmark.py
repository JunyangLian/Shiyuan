"""检索基线：120 篇带唯一标记的文档 + 140 道有明确目标文件的题。

这不是"真实用户语料"的替代品，但能稳定衡量召回是否退化：
文件名、正文长词、中文两字短词（LIKE 兜底）和自然语言问句各占一部分。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from shiyuan import db, indexer, scanner, search, store

CHARS = "紫杉银杏松柏槐柳杨桐枫桦楠樟榕桂梅兰竹菊荷莲菱藻蕨苔藓藤萝"
SHORT_TOKENS = [
    CHARS[i] + CHARS[j]
    for i in range(len(CHARS))
    for j in range(len(CHARS))
    if i != j
][:120]


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "benchmark.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _build_corpus(conn, tmp_path: Path) -> list[tuple[str, str]]:
    root = tmp_path / "检索基线"
    root.mkdir()
    cases: list[tuple[str, str]] = []
    for index, token in enumerate(SHORT_TOKENS):
        platform = "NovaSeq" if index % 2 == 0 else "BGI-T7"
        sample = 100 + index
        marker = f"标记{index:03d}"
        name = f"项目{index:03d}-{token}.md"
        (root / name).write_text(
            f"# 项目{index:03d} 实验记录\n"
            f"物种：{token}物种{index:03d}\n"
            f"样本量：{sample}\n"
            f"测序平台：{platform}\n"
            f"{marker}：本项目的样本量为 {sample}，平台为 {platform}。\n",
            encoding="utf-8",
        )
        cases.append((name, name))

    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return cases


def test_retrieval_benchmark_recall(conn, tmp_path):
    stem_cases = _build_corpus(conn, tmp_path)

    queries: list[tuple[str, str]] = []
    for index in range(40):
        queries.append((f"项目{index:03d}-{SHORT_TOKENS[index]}", stem_cases[index][1]))
    for index in range(40, 80):
        queries.append((f"标记{index:03d}", stem_cases[index][1]))
    for index in range(80, 120):
        queries.append((SHORT_TOKENS[index], stem_cases[index][1]))
    for index in range(20):
        queries.append((f"标记{index:03d} 的样本量是多少？", stem_cases[index][1]))

    ranks: list[int | None] = []
    for query, expected in queries:
        result = search.search(conn, query, limit=5)
        names = [item["name"] for item in result["items"]]
        ranks.append(names.index(expected) + 1 if expected in names else None)

    total = len(ranks)
    recall_at_1 = sum(1 for rank in ranks if rank == 1) / total
    recall_at_5 = sum(1 for rank in ranks if rank and rank <= 5) / total
    mrr = sum(1.0 / rank for rank in ranks if rank) / total
    print(
        f"retrieval benchmark: n={total} R@1={recall_at_1:.3f} "
        f"R@5={recall_at_5:.3f} MRR={mrr:.3f}"
    )

    assert total == 140
    assert recall_at_5 >= 0.95, f"R@5 退化：{recall_at_5:.3f}"
    assert recall_at_1 >= 0.80, f"R@1 退化：{recall_at_1:.3f}"
    assert mrr >= 0.80, f"MRR 退化：{mrr:.3f}"
    assert math.isfinite(mrr)
