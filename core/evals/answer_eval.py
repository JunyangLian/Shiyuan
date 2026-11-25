"""问答评测：检查引用命中、关键信息、拒答与延迟。

查询集默认放在 $SHIYUAN_HOME/evals/answer.json，格式：
{
  "queries": [
    {"q": "问题", "expect_paths": ["路径片段"], "must_include": ["关键词"],
     "expect_refusal": false, "tag": "paper"}
  ]
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shiyuan import answer, config, db  # noqa: E402


def _norm(value: str) -> str:
    return "".join((value or "").lower().split())


def _path_hit(result: dict, expects: list[str]) -> bool:
    if not expects:
        return True
    citations = result.get("citations") or []
    for citation in citations:
        path = (citation.get("path") or "").replace("/", "\\").lower()
        if any(expected.lower().replace("/", "\\") in path for expected in expects):
            return True
    return False


def _load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    queries = data.get("queries") if isinstance(data, dict) else data
    return [entry for entry in queries if isinstance(entry, dict) and entry.get("q")]


def main() -> int:
    parser = argparse.ArgumentParser(description="拾源问答评测")
    parser.add_argument("--queries", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args()

    path = args.queries or (config.app_home() / "evals" / "answer.json")
    if not path.exists():
        raise SystemExit(f"找不到查询集：{path}")
    queries = _load(path)
    if args.tag:
        queries = [entry for entry in queries if entry.get("tag") == args.tag]
    if not queries:
        raise SystemExit("没有可运行的查询")

    conn = db.connect(args.db)
    rows: dict[str, dict] = {}
    failures: list[str] = []
    try:
        for entry in queries:
            started = time.perf_counter()
            try:
                result = answer.ask(conn, entry["q"])
                error = None
            except Exception as exc:  # noqa: BLE001 - 评测要记录而不是中断
                result = None
                error = str(exc)
            elapsed = time.perf_counter() - started
            expect_refusal = bool(entry.get("expect_refusal"))
            reasons = []
            if error:
                passed = False
                reasons.append(f"error={error[:80]}")
            else:
                refusal_ok = bool(result.get("refused")) == expect_refusal
                if not refusal_ok:
                    reasons.append("拒答判断错误")
                if expect_refusal and result.get("refused"):
                    path_ok = keyword_ok = citation_ok = True
                else:
                    path_ok = _path_hit(result, [
                        str(item) for item in entry.get("expect_paths") or []])
                    if not path_ok:
                        reasons.append("引用未命中目标文件")
                    keywords = [str(item) for item in entry.get("must_include") or []]
                    answer_text = _norm(result.get("answer") or "")
                    missing = [word for word in keywords if _norm(word) not in answer_text]
                    keyword_ok = not missing
                    if missing:
                        reasons.append("缺少关键信息:" + "/".join(missing))
                    citation_ok = result.get("warning") is None
                    if not citation_ok:
                        reasons.append("引用校验告警")
                passed = refusal_ok and path_ok and keyword_ok and citation_ok

            key = str(entry.get("tag") or "all")
            for bucket in (key, "ALL"):
                row = rows.setdefault(bucket, {"n": 0, "pass": 0, "seconds": 0.0})
                row["n"] += 1
                row["pass"] += 1 if passed else 0
                row["seconds"] += elapsed
            if not passed:
                failures.append(entry["q"])

            if result is not None:
                names = [citation.get("name") or "" for citation in
                         (result.get("citations") or [])][:max(0, args.show)]
                head = (result.get("answer") or "").replace("\n", " ")[:90]
                print(f"{'PASS' if passed else 'FAIL':4} [{key}] {elapsed:5.1f}s "
                      f"{entry['q']} -> {', '.join(names)}")
                if not passed:
                    print(f"      {head}")
                    print(f"      {'；'.join(reasons)}")
            else:
                print(f"FAIL [{key}] {elapsed:5.1f}s {entry['q']} -> ERROR")
                print(f"      {error}")
    finally:
        conn.close()

    print("\ntag            n  pass   rate   avg_s")
    for tag in sorted(rows, key=lambda value: (value == "ALL", value)):
        row = rows[tag]
        n = max(1, row["n"])
        print(f"{tag:<12} {row['n']:>3} {row['pass']:>5}  {row['pass']/n:5.2f}  "
              f"{row['seconds']/n:5.1f}")
    if failures:
        print("\n失败问题：")
        for question in failures:
            print("  -", question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
