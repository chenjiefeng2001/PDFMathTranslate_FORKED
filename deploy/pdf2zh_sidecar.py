"""Tauri sidecar 入口：把 REST/SSE API 固化为单文件可执行。

用法（打包后）：
    pdf2zh-api-sidecar.exe [--port 11009]

由 frontend/src-tauri 的外壳以子进程方式托管。
"""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="pdf2zh REST/SSE sidecar")
    parser.add_argument("--port", type=int, default=11009)
    args, _ = parser.parse_known_args()

    import uvicorn

    from pdf2zh.services.api import create_api_app
    from pdf2zh.services.runtime_singleton import get_runtime_service

    app = create_api_app(
        service=get_runtime_service(), allow_origins=["*"]
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
