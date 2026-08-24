"""B2 API 微基准：只读端点延迟分布 + 并发吞吐粗测。

用法：
    python script/bench/bench_api.py [--n 200]
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_common import (  # noqa: E402
    DEFAULT_EXE,
    http_json,
    launch_sidecar,
    median,
    pct,
    print_table,
    stop_sidecar,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    handle = launch_sidecar(Path(args.exe))
    try:
        base = handle.base()
        # 预热（懒导入）
        http_json("GET", base + "/api/engines", timeout=120.0)

        rows = []
        for name, path in (
            ("GET /api/health", "/api/health"),
            ("GET /api/engines", "/api/engines"),
            ("GET /api/tasks", "/api/tasks"),
        ):
            samples_ms: list[float] = []
            for _ in range(args.n):
                _, _, elapsed = http_json("GET", base + path)
                samples_ms.append(elapsed * 1000)
            rows.append(
                (
                    name,
                    f"{median(samples_ms):.1f}",
                    f"{pct(samples_ms, 0.95):.1f}",
                    f"{pct(samples_ms, 0.99):.1f}",
                    f"{max(samples_ms):.1f}",
                )
            )

        print_table(
            f"read latency (n={args.n})",
            rows,
            ("endpoint", "p50 ms", "p95 ms", "p99 ms", "max ms"),
        )

        # 并发吞吐：16 线程打 health
        stop = threading.Event()
        counters = [0] * 16

        def _worker(slot: int) -> None:
            while not stop.is_set():
                http_json("GET", base + "/api/health")
                counters[slot] += 1

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(_worker, i) for i in range(16)]
            import time

            time.sleep(3.0)
            stop.set()
            for future in futures:
                future.result()
        total = sum(counters)
        print(f"\nhealth throughput (16 threads): {total / 3.0:.0f} req/s")
        return 0
    finally:
        stop_sidecar(handle)


if __name__ == "__main__":
    sys.exit(main())
