"""Tauri sidecar 入口：把 REST/SSE API 固化为可执行（onedir 形态）。

用法（打包后）：
    pdf2zh-api-sidecar.exe [--port 11009]

由 frontend/src-tauri 的外壳以子进程方式托管。
"""

import argparse
import multiprocessing
import sys


def main() -> int:
    # frozen (PyInstaller) 环境必须最先调用：legacy 并行翻译使用
    # ProcessPoolExecutor，Windows spawn 会以特殊 argv 重新拉起本 exe；
    # freeze_support() 拦截该请求并进入 worker 引导。缺失时子进程会
    # 把整个 uvicorn 服务再起一遍（端口冲突退出）→ BrokenProcessPool，
    # 多进程版面分析/GPU worker 全部失效。
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description="pdf2zh REST/SSE sidecar")
    parser.add_argument("--port", type=int, default=11009)
    args, _ = parser.parse_known_args()

    import uvicorn

    from pdf2zh.services.api import create_api_app
    from pdf2zh.services.runtime_singleton import get_runtime_service

    app = create_api_app(
        service=get_runtime_service(), allow_origins=["http://tauri.localhost"]
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
