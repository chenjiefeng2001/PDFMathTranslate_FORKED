"""B3 翻译管线基准：合成多页 PDF × 配置矩阵，输出阶段时间线 / 墙钟 / 峰值内存。

阶段时间线来自 SSE 帧（workload-model 百分比单调），可分离解析、翻译、
渲染段——即便引擎走外网（google），分段耗时仍具可比性。

用法：
    python script/bench/bench_translate.py [--pages 10] [--configs legacy_t1,legacy_t4]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_common import (  # noqa: E402
    DEFAULT_EXE,
    ensure_fixture,
    http_json,
    launch_sidecar,
    multipart_body,
    print_table,
    read_sse,
    stop_sidecar,
)

CONFIGS = {
    "legacy_t1": {"parse_engine": "legacy", "threads": "1"},
    "legacy_t4": {"parse_engine": "legacy", "threads": "4"},
    "legacy_t8": {"parse_engine": "legacy", "threads": "8"},
    "babeldoc_t4": {"parse_engine": "babeldoc", "threads": "4"},
}


def run_one(handle_base: str, pdf: Path, name: str, fields: dict[str, str]) -> dict[str, Any]:
    body, ctype = multipart_body(
        pdf,
        {
            **fields,
            "target_lang": "zh-CN",
            "source_lang": "auto",
            "engine": "google",
            "ignore_cache": "true",
        },
    )
    submit_started = time.perf_counter()
    status, resp, _ = http_json(
        "POST", handle_base + "/api/tasks",
        body=body, headers={"Content-Type": ctype}, timeout=60.0,
    )
    submit_ms = (time.perf_counter() - submit_started) * 1000
    if status != 200:
        return {"config": name, "error": f"submit {status}: {resp}"}
    task_id = resp["task_id"]

    done_at: dict[str, float] = {}
    stage_spans: dict[str, float] = {}
    last_ts = 0.0
    last_stage: str | None = None

    def _on_frame(frame) -> bool:
        nonlocal last_ts, last_stage
        if frame.event == "progress":
            stage = str(frame.data.get("stage") or "")
            if stage and stage != last_stage:
                if last_stage is not None:
                    stage_spans[last_stage] = (
                        stage_spans.get(last_stage, 0.0) + frame.ts - last_ts
                    )
                last_stage, last_ts = stage, frame.ts
        elif frame.event == "done":
            done_at["t"] = frame.ts
            return False
        return True

    read_sse(
        handle_base + f"/api/tasks/{task_id}/events",
        timeout=900.0,
        on_frame=_on_frame,
    )

    status2, state, _ = http_json("GET", handle_base + f"/api/tasks/{task_id}")
    wall = done_at.get("t", 0.0)
    return {
        "config": name,
        "status": state.get("status"),
        "submit_ms": round(submit_ms, 1),
        "wall_s": round(wall, 1),
        "stage_s": {k: round(v, 1) for k, v in sorted(stage_spans.items())},
        "files": len(state.get("result_files") or []),
        "peak_rss_mb": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=str(DEFAULT_EXE))
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--configs", default=",".join(CONFIGS))
    args = parser.parse_args()

    pdf = ensure_fixture(args.pages)
    names = [c for c in args.configs.split(",") if c]

    handle = launch_sidecar(Path(args.exe), sample_rss=True)
    results = []
    try:
        base = handle.base()
        # 预热：懒导入 translator 注册表与模型下载检查
        http_json("GET", base + "/api/engines", timeout=120.0)
        # 服务态预热等待：sidecar 启动后会在后台建常驻 worker 池（~8s），
        # 等它就绪后再开跑，模拟真实桌面使用节奏。
        time.sleep(12.0)
        rss_before_run = max(handle.rss_samples_mb) if handle.rss_samples_mb else 0.0

        for name in names:
            fields = CONFIGS.get(name, {})
            result = run_one(base, pdf, name, fields)
            parent_peak = max(handle.rss_parent_mb) if handle.rss_parent_mb else 0.0
            workers_peak = max(handle.rss_workers_mb) if handle.rss_workers_mb else 0.0
            result["peak_rss_mb"] = round(handle.peak_tree_rss_mb(), 0)
            result["rss_split"] = {
                "parent": round(parent_peak, 0),
                "workers": round(workers_peak, 0),
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
    finally:
        stop_sidecar(handle)

    rows = []
    for r in results:
        if "error" in r:
            rows.append((r["config"], r["error"], "-", "-", "-"))
            continue
        stage_summary = " ".join(f"{k}:{v}s" for k, v in r["stage_s"].items())
        rows.append((
            r["config"],
            f"{r['wall_s']}s",
            f"{r['submit_ms']}ms",
            f"{r['peak_rss_mb']:.0f}MB" if isinstance(r["peak_rss_mb"], (int, float)) else "-",
            stage_summary,
        ))
    print_table(
        f"translate pipeline ({args.pages}p, engine=google, ignore_cache)",
        rows,
        ("config", "wall", "submit", "peakRSS", "stage timeline (SSE)"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
