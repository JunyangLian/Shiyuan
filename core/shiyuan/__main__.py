"""启动入口：`python -m shiyuan`（桌面端开发时由脚本并行拉起）。"""

from __future__ import annotations

import os

import uvicorn

from .instance import acquire_http_instance


def main() -> None:
    host = os.environ.get("SHIYUAN_HOST", "127.0.0.1")
    port = int(os.environ.get("SHIYUAN_PORT", "8737"))
    if not acquire_http_instance(port):
        print(f"已有拾源核心在使用端口 {port}，本进程退出")
        return
    uvicorn.run("shiyuan.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
