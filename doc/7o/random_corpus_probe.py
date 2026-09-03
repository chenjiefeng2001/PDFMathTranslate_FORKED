"""随机全文管线探针 — 从 tests/file 随机抽 PDF × 场景（auto / mineru），真机验证。

随机化（--seed 可复现）：
- 从 tests/file/*.pdf 全集随机抽 N 个（默认 5）；
- 每个文件随机选场景：auto（scenario-1）或 mineru 强制（scenario-6）；
- 大文件（>8MB）按 --max-mb 上限过滤，避免单文件跑满小时级时长。

每个 run 走生产入口 run_magicpdf_main（仅 translator 用 echo 桩），--trace
全开，验证 ingest.select 故事链 + audit PASS（与 doc/7o 探针同标准）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingestion_corpus_probe import EchoTranslator, make_ns  # noqa: E402

from pdf2zh.v3.flight_recorder import read_events  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FILE_DIR = REPO / "tests" / "file"

# 已知重名变体/输出副本，剔除避免重复采样
SKIP_PATTERNS = (" (1)", ".no_watermark.")


def list_pdfs(max_mb: float) -> list[Path]:
    pdfs = []
    for p in sorted(FILE_DIR.glob("*.pdf")):
        if any(s in p.name for s in SKIP_PATTERNS):
            continue
        if p.stat().st_size > max_mb * 1024 * 1024:
            continue
        pdfs.append(p)
    return pdfs


def run_one(pdf: Path, backend: str, out: Path) -> tuple[bool, list[str]]:
    """跑一个 run 并按 7o 探针同款标准校验 trace 故事。"""
    from unittest.mock import patch

    from pdf2zh.magicpdf_cli import run_magicpdf_main

    problems: list[str] = []
    trace_jsonl = out / "trace" / f"{pdf.stem}_events.jsonl"
    ns = make_ns(str(pdf), str(out), backend)
    t0 = time.time()
    try:
        with patch("pdf2zh.translator.build_translator", return_value=EchoTranslator()):
            code = run_magicpdf_main(ns)
    except Exception as exc:  # noqa: BLE001
        return False, [f"EXCEPTION: {exc!r}"]
    elapsed = time.time() - t0

    if not trace_jsonl.exists():
        return False, [f"rc={code}, no trace file at {trace_jsonl}"]
    events = list(read_events(str(trace_jsonl)))
    names = [e["event"] for e in events]

    def check(cond, msg):
        if not cond:
            problems.append(msg)

    check(names and names[0] == "run.begin" and names[-1] == "run.end", "run begin/end")
    check("ingest.raw.begin" in names, "raw begin")
    check(
        "ingest.begin" in names and "ingest.block" in names, "canonical ingest events"
    )
    sel_list = [e for e in events if e["event"] == "ingest.select"]
    check(len(sel_list) == 1, f"exactly one ingest.select (got {len(sel_list)})")
    if sel_list:
        d = sel_list[0]["payload"]["decision"]
        expected = "forced_backend" if backend == "mineru" else "primary_ingest_pass"
        check(d["selected_backend"] == "mineru", f"selected={d['selected_backend']}")
        check(d["reason"] == expected, f"reason={d['reason']} != {expected}")
        check(d["fallback_attempted"] is False, "fallback_attempted must be False here")
    check(any(n.startswith("plan.") for n in names), "plan events present")
    summary = out / "audit" / "summary.json"
    check(summary.exists(), "audit summary.json written")
    if summary.exists():
        s = json.loads(summary.read_text(encoding="utf-8"))
        # 渲染层 MEDIUM 证据在 echo 桩下可能出现；HIGH（FAIL）绝不允许。
        check(
            s.get("qualification") in ("PASS", "PASS_WITH_MEDIUM"),
            f"qualification={s.get('qualification')}",
        )
        check(
            s.get("by_severity", {}).get("HIGH", 0) == 0,
            f"HIGH severity fails: {s.get('by_severity')}",
        )
    print(
        f"  {pdf.name} | {backend} | rc={code} | {elapsed:.1f}s | "
        f"events={len(events)} blocks={sum(1 for n in names if n == 'ingest.block')}"
    )
    print(f"    VERDICT: {'OK' if not problems else 'FAILED: ' + '; '.join(problems)}")
    return not problems, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--max-mb", type=float, default=8.0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pool = list_pdfs(args.max_mb)
    if not pool:
        print("no eligible PDFs", file=sys.stderr)
        return 2
    picks = rng.sample(pool, min(args.samples, len(pool)))
    runs = []
    for p in picks:
        backend = rng.choice(["auto", "mineru"])
        runs.append((p, backend))
    seed_tag = args.seed if args.seed is not None else "random"
    print(
        f"randomized corpus probe | seed={seed_tag} pool={len(pool)} sample={len(runs)}"
    )
    for p, b in runs:
        print(f"  picked: {p.name} -> {b}")

    all_ok = True
    with tempfile.TemporaryDirectory(prefix="rand_probe_") as tmp:
        for i, (pdf, backend) in enumerate(runs):
            out = Path(tmp) / f"run{i}"
            out.mkdir(parents=True)
            try:
                ok, _ = run_one(pdf, backend, out)
            except Exception as exc:  # noqa: BLE001
                print(f"  {pdf.name} | {backend} | HARNESS EXCEPTION: {exc!r}")
                ok = False
            all_ok = all_ok and ok

    print(f"\nRANDOMIZED RESULT: {'ALL OK' if all_ok else 'FAILURES PRESENT'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
