"""Tauri sidecar 入口：把 REST/SSE API 固化为可执行（onedir 形态）。

用法（打包后）：
    pdf2zh-api-sidecar.exe [--port 11009]

由 frontend/src-tauri 的外壳以子进程方式托管。
"""

import argparse
import multiprocessing
import sys
import time


def main() -> int:
    t0 = time.perf_counter()

    # frozen (PyInstaller) 环境必须最先调用：legacy 并行翻译使用
    # ProcessPoolExecutor，Windows spawn 会以特殊 argv 重新拉起本 exe；
    # freeze_support() 拦截该请求并进入 worker 引导。缺失时子进程会
    # 把整个 uvicorn 服务再起一遍（端口冲突退出）→ BrokenProcessPool，
    # 多进程版面分析/GPU worker 全部失效。
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description="pdf2zh REST/SSE sidecar")
    parser.add_argument("--port", type=int, default=11009)
    args, _ = parser.parse_known_args()

    import logging

    import uvicorn

    from pdf2zh.services.api import create_api_app
    from pdf2zh.services.runtime_singleton import get_runtime_service

    # 桌面壳把 sidecar 的 stdout/stderr 重定向进 %TEMP%\pdf2zh-sidecar.log，
    # 但**本进程此前从未给 root logger 装任何 handler** —— Python 在这种
    # 情况下只用 "handler of last resort"，它只输出 WARNING 及以上、
    # 且格式只有消息正文。结果：MinerU 的进度条（直接写 stdout）看得见，
    # 而 pdf2zh 自己的 logger.info/warning 几乎全部沉底：一次 325 页扫描件
    # 翻译跑完，日志里零条 WARNING/ERROR，排障时完全看不到
    # 「翻译器超时」「零块译出」「产物未生成」这类决定性信息。
    #
    # 这里补一个 stderr handler：走 stderr 才能进桌面壳那份重定向日志，
    # 且与上面的 last-resort 行为兼容（只提升可见性，不改变既有语义）。
    root = logging.getLogger()
    if not root.handlers:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(stderr_handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        logging.getLogger("pdf2zh.sidecar").info(
            "root logger: installed stderr handler at INFO"
        )

    app = create_api_app(
        service=get_runtime_service(), allow_origins=["http://tauri.localhost"]
    )

    t1 = time.perf_counter()
    print(
        f"[sidecar] app created in {t1 - t0:.1f}s, starting uvicorn on :{args.port} ..."
    )

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
