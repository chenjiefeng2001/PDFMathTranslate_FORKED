"""B1 启动基准：sidecar 冷启动 → API 就绪 → 首次 /api/engines（懒加载翻译注册表）。

用法：
    python script/bench/bench_startup.py [--runs 5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_common import (  # noqa: E402
    DEFAULT_EXE,
    http_json,
    launch_sidecar,
    median,
    print_table,
    stop_sidecar,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    exe = Path(args.exe)

    rows = []
    t_startup_runs: list[float] = []
    t_engines_runs: list[float] = []
    rss_runs: list[float] = []

    for run in range(1, args.runs + 1):
        handle = launch_sidecar(exe, sample_rss=True)
        try:
            base = handle.base()
            startup_ms = handle.startup_s * 1000
            # 健康后首次 engines（触发 translator 注册表懒导入）
            _, _, engines_elapsed = http_json(
                "GET", base + "/api/engines", timeout=120.0
            )
            import time

            time.sleep(1.0)  # 让 RSS 采样稳定
            rss = handle.peak_tree_rss_mb()
            t_startup_runs.append(startup_ms)
            t_engines_runs.append(engines_elapsed * 1000)
            rss_runs.append(rss)
            rows.append(
                (
                    run,
                    f"{startup_ms:.0f} ms",
                    f"{engines_elapsed * 1000:.0f} ms",
                    f"{rss:.0f} MB",
                )
            )
        finally:
            stop_sidecar(handle)

    print_table(
        "cold start (spawn -> health OK -> first /api/engines)",
        rows,
        ("run", "cold start", "first-engines", "RSS(idle)"),
    )
    print(
        f"\nmedian: cold-start {median(t_startup_runs):.0f} ms | "
        f"first-engines {median(t_engines_runs):.0f} ms | "
        f"rss {median(rss_runs):.0f} MB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
