"""检索评测：用固定查询集量 R@1 / R@5 / R@10 / MRR，改动排序前后跑同一套。

查询集默认放在 $SHIYUAN_HOME/evals/search.json，格式：
{
  "queries": [
    {"q": "问题或关键词", "expect": ["路径片段", "..."], "tag": "semantic"}
  ]
}
expect 里的片段按不区分大小写、统一斜杠后做子串匹配；命中任意一个就算找到。

用法：
    python evals/search_eval.py --queries .tmp/evals/search.json -n 10
    python evals/search_eval.py --queries ... --no-vector   # 只看词面召回
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shiyuan import config, db, embeddings, search  # noqa: E402


def _normalize(value: str) -> str:
    return (value or "").lower().replace("/", "\\")


def _rank(items: list[dict], expect: list[str]) -> int | None:
    targets = [_normalize(item) for item in expect if item]
    for index, item in enumerate(items, start=1):
        path = _normalize(item.get("path") or "")
        if any(target in path for target in targets):
            return index
    return None


def _load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    queries = data.get("queries") if isinstance(data, dict) else data
    if not isinstance(queries, list) or not queries:
        raise SystemExit("查询集为空或格式不正确")
    cleaned = []
    for entry in queries:
        if not isinstance(entry, dict) or not entry.get("q"):
            continue
        expect = [str(value) for value in entry.get("expect") or [] if value]
        if not expect:
            continue
        cleaned.append({
            "q": str(entry["q"]),
            "expect": expect,
            "tag": str(entry.get("tag") or "all"),
        })
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser(description="拾源检索评测")
    parser.add_argument("--queries", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("-n", type=int, default=10, help="计算指标的结果深度")
    parser.add_argument("--limit", type=int, default=50, help="检索返回条数")
    parser.add_argument("--tag", default=None, help="只跑某个标签")
    parser.add_argument("--no-vector", action="store_true", help="关闭语义召回，只看词面")
    parser.add_argument("--show", type=int, default=3, help="每条查询打印前几条结果")
    parser.add_argument(
        "--set", action="append", default=[], metavar="NAME=VALUE",
        help="临时覆盖排序常量做 A/B，如 COVERAGE_WEIGHT=0、Z_GATE=2.0")
    args = parser.parse_args()

    for override in args.set:
        name, _, raw = override.partition("=")
        target = search if hasattr(search, name) else embeddings if hasattr(embeddings, name) else None
        if target is None or not name or not raw:
            raise SystemExit(f"未知参数：{override}")
        current = getattr(target, name)
        setattr(target, name, type(current)(float(raw)) if isinstance(current, float) else int(raw))

    queries_path = args.queries or (config.app_home() / "evals" / "search.json")
    if not queries_path.exists():
        raise SystemExit(f"找不到查询集：{queries_path}")
    queries = _load(queries_path)
    if args.tag:
        queries = [entry for entry in queries if entry["tag"] == args.tag]
    if not queries:
        raise SystemExit("没有可运行的查询")

    if args.no_vector:
        embeddings.enabled = lambda settings: False   # type: ignore[assignment]

    conn = db.connect(args.db)
    depth = max(1, args.n)
    rows: dict[str, dict] = {}
    misses: list[str] = []
    try:
        for entry in queries:
            result = search.search(conn, entry["q"], limit=max(50, args.limit))
            rank = _rank(result["items"][:depth], entry["expect"])
            key = entry["tag"]
            for bucket in (key, "ALL"):
                row = rows.setdefault(bucket, {"n": 0, "at1": 0, "at5": 0, "at10": 0, "mrr": 0.0})
                row["n"] += 1
                if rank:
                    row["mrr"] += 1 / rank
                    if rank <= 1:
                        row["at1"] += 1
                    if rank <= 5:
                        row["at5"] += 1
                    if rank <= 10:
                        row["at10"] += 1
            if rank is None:
                misses.append(entry["q"])
            top = " | ".join(
                Path(item["path"]).name for item in result["items"][:max(0, args.show)])
            limit_note = "" if result["total"] <= args.limit else f"  (共 {result['total']})"
            print(f"{'#' + str(rank) if rank else 'miss':>5}  "
                  f"[{entry['tag']}] {entry['q']}  ->  {top}{limit_note}")
    finally:
        conn.close()

    print("\ntag            n   R@1   R@5  R@10   MRR")
    for tag in sorted(rows, key=lambda value: (value == "ALL", value)):
        row = rows[tag]
        n = max(1, row["n"])
        print(f"{tag:<12} {row['n']:>4}  {row['at1']/n:5.2f} {row['at5']/n:5.2f}"
              f" {row['at10']/n:5.2f}  {row['mrr']/n:5.2f}")
    if misses:
        print(f"\n未命中 {len(misses)} 条：")
        for question in misses:
            print(f"  - {question}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
