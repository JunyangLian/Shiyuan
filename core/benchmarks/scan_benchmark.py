"""目录级全量对账基准：创建 N 个小文件并计时首次/二次扫描。

用法：
    python core/benchmarks/scan_benchmark.py --files 10000
    python core/benchmarks/scan_benchmark.py --files 100000 --dir D:\\bench

二次扫描模拟目录级事件触发的整源对账；目录路径会打印出来，便于按需保留或删除。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shiyuan import db, scanner, store

WORKSPACE = Path(__file__).resolve().parents[2]
TMP_ROOT = WORKSPACE / ".tmp"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--dir", help="测试目录；默认创建临时目录")
    args = parser.parse_args()

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(args.dir) if args.dir else TMP_ROOT / f"scan-bench-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    for index in range(args.files):
        (root / f"file_{index:06d}.md").write_text("x\n", encoding="utf-8")
    create_seconds = time.perf_counter() - started

    home = TMP_ROOT / f"scan-db-{os.getpid()}"
    home.mkdir(parents=True, exist_ok=True)
    conn = db.connect(home / "benchmark.db")
    db.init_db(conn)
    try:
        source = store.add_source(conn, root)
        started = time.perf_counter()
        first = scanner.scan_source(conn, source["source_id"])
        first_seconds = time.perf_counter() - started

        (root / "新增目录").mkdir()
        for index in range(10):
            (root / "新增目录" / f"new_{index}.md").write_text("y\n", encoding="utf-8")
        started = time.perf_counter()
        second = scanner.scan_source(conn, source["source_id"])
        second_seconds = time.perf_counter() - started
    finally:
        conn.close()

    print(json.dumps({
        "root": str(root),
        "db": str(home / "benchmark.db"),
        "files": args.files,
        "create_seconds": round(create_seconds, 3),
        "first_scan_seconds": round(first_seconds, 3),
        "first_scan_result": first,
        "second_scan_seconds": round(second_seconds, 3),
        "second_scan_result": second,
        "files_per_second": round(args.files / first_seconds, 1) if first_seconds else None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
