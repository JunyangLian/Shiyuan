"""启动入口：`python -m shiyuan`（桌面端开发时由脚本并行拉起）。"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("SHIYUAN_HOST", "127.0.0.1")
    port = int(os.environ.get("SHIYUAN_PORT", "8737"))
    uvicorn.run("shiyuan.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
