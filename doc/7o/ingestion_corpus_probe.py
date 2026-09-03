"""Temporary corpus probe — Adaptive Ingestion v1.1 scenarios on real PDFs.

Drives the production ``run_magicpdf_main`` entry (only the translator is
stubbed pass-through, since no network translation service is reachable
here) with ``--trace`` on, then verifies each scenario's trace story:

    raw ingest → canonical ingest → quality decision → ingest.select
    → plan → audit

Scenarios runnable in this environment (MinerU present, marker absent):
    1. auto (normal MinerU)        → selected=mineru, no fallback
    6. forced mineru               → selected=mineru, reason=forced_backend
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from pdf2zh.v3.flight_recorder import read_events

logging.getLogger("magic_pdf").setLevel(logging.WARNING)
logging.getLogger("magicpdf").setLevel(logging.INFO)


class EchoTranslator:
    """Pass-through translator — keeps block text stable for the trace."""

    def __init__(self, *a, **k):
        pass

    def translate(self, text, ignore_cache=False):
        return text or ""


def make_ns(pdf, out, ingest_backend):
    return argparse.Namespace(
        files=[pdf],
        output=out,
        pages=None,
        lang_in="en",
        lang_out="zh",
        service="echo",
        thread=4,
        no_parallel=False,
        parallel_workers=None,
        vfont="",
        vchar="",
        envs={},
        prompt=None,
        ignore_cache=False,
        compatible=False,
        debug=False,
        dir=False,
        backend="auto",
        mode="fast",
        parse_engine="magicpdf",
        magicpdf_ocr=False,
        magicpdf_ocr_mode="auto",
        ingest_backend=ingest_backend,
        marker_json=None,
        marker_version=None,
        trace=True,
        trace_dir="",
        log_file="",
        magicpdf_render=True,
    )


def story(pdf_path, out_dir, ingest_backend, label):
    from unittest.mock import patch

    from pdf2zh.magicpdf_cli import run_magicpdf_main

    stem = Path(pdf_path).stem
    trace_jsonl = Path(out_dir) / "trace" / f"{stem}_events.jsonl"
    with patch("pdf2zh.translator.build_translator", return_value=EchoTranslator()):
        code = run_magicpdf_main(make_ns(pdf_path, out_dir, ingest_backend))

    events = list(read_events(str(trace_jsonl)))
    names = [e["event"] for e in events]
    ok = True
    problems = []

    def check(cond, msg):
        nonlocal ok
        if not cond:
            ok = False
            problems.append(msg)

    # story skeleton
    check(names[0] == "run.begin" and names[-1] == "run.end", "run begin/end")
    check(
        "ingest.raw.begin" in names and "ingest.raw.block" in names,
        "raw events present",
    )
    check(
        "ingest.begin" in names and "ingest.block" in names and "ingest.end" in names,
        "canonical ingest events present",
    )
    # raw before canonical, same trace ids
    raw_idx = [i for i, e in enumerate(events) if e["event"] == "ingest.raw.block"]
    can_idx = [i for i, e in enumerate(events) if e["event"] == "ingest.block"]
    check(raw_idx and can_idx and max(raw_idx) < min(can_idx), "raw before canonical")
    raw_ids = {events[i]["trace_id"] for i in raw_idx}
    can_ids = {events[i]["trace_id"] for i in can_idx}
    check(raw_ids == can_ids, "raw/canonical share trace_id")

    # raw facts
    r0 = events[raw_idx[0]]["payload"]
    check(r0.get("box") and r0.get("normalized") is True, "raw block has declared box")
    check(
        r0.get("box_space") == "page_tl" and r0.get("box_origin") == "top-left",
        "raw coordinate semantics declared",
    )

    # select decision
    sel = next(e for e in events if e["event"] == "ingest.select")
    d = sel["payload"]["decision"]
    check(
        d["selected_backend"] == "mineru",
        f"selected=mineru (got {d['selected_backend']})",
    )
    check(d["fallback"] is False, "no fallback")
    expected_reason = (
        "forced_backend" if ingest_backend == "mineru" else "primary_ingest_pass"
    )
    check(
        d["reason"] == expected_reason, f"reason={expected_reason} (got {d['reason']})"
    )
    check(d["fallback_attempted"] is False, "no fallback attempted")

    # pipeline continued into plan
    check(any(n.startswith("plan.") for n in names), "plan events present")

    # audit outputs
    audit_dir = Path(out_dir) / "audit"
    summary = audit_dir / "summary.json"
    check(summary.exists(), "audit summary.json written")
    if summary.exists():
        s = json.loads(summary.read_text(encoding="utf-8"))
        check(
            s.get("qualification") == "PASS",
            f"qualification PASS (got {s.get('qualification')})",
        )
        check(s.get("rule_fails", 0) == 0, "no rule fails")

    print(
        f"\n=== {label}: {Path(pdf_path).name} | backend={ingest_backend} | rc={code}"
    )
    print(
        f"    events={len(events)} raw_blocks={len(raw_idx)} canonical_blocks={len(can_idx)} "
        f"plan_events={sum(1 for n in names if n.startswith('plan.'))}"
    )
    print(
        f"    select: selected={d['selected_backend']} reason={d['reason']} "
        f"fallback={d['fallback']} attempted={d['fallback_attempted']}"
    )
    print(f"    VERDICT: {'OK' if ok else 'FAILED ' + '; '.join(problems)}")
    return code, ok


def main():
    files = [
        os.path.join("tests", "file", "TestPDF.pdf"),
        os.path.join("tests", "file", "translate.cli.plain.text.pdf"),
        os.path.join("tests", "file", "1905.11395v2.pdf"),
    ]
    scenarios = [
        ("auto", "scenario-1-normal-mineru"),
        ("mineru", "scenario-6-forced-mineru"),
    ]
    all_ok = True
    with tempfile.TemporaryDirectory(prefix="corpus_probe_") as tmp:
        for pdf in files:
            for backend, label in scenarios:
                try:
                    out = os.path.join(tmp, Path(pdf).stem, backend)
                    os.makedirs(out, exist_ok=True)
                    _, ok = story(pdf, out, backend, label)
                    all_ok = all_ok and ok
                except Exception as exc:  # noqa: BLE001 -- probe must not die
                    print(
                        f"\n=== {label}: {Path(pdf).name} | backend={backend} | EXCEPTION: {exc!r}"
                    )
                    all_ok = False
    print(f"\nCORPUS RESULT: {'ALL OK' if all_ok else 'FAILURES PRESENT'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
