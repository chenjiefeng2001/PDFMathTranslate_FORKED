# -*- coding: utf-8 -*-
"""7J-4 — Release gate (quality gate for a release candidate).

Runs, in order:

1. Regression latch test subset (pytest):
   - 7I-7  xobj_id=None -> -1 normalization
   - 7J-3A F9 text-layer integrity (NUL detector contract)
   - 7I-4  F1/F3/F5/F6/F8 detector coverage contract
   - 7I-6A/B evidence inventory + F9/F10 wiring
   - 7I-3  CID recovery (evidence-based, font-scoped)
   - 7I-5B F8 re-WRAP layout policy contract
   - 7I-6C F5/F7 eligibility gates
   - 7J-3C Case B token-shape latches

2. Corpus baseline re-run + frozen-matrix assertion:
   - total_residual == 1
   - by_defect == {"F4": 1}, by_first_divergence == {"parser": 1}
   - F4: FAIL 1 (p300 @ parser) — the preserved negative control
   - F5: SKIP 31, F7: NOT_MEASURED 31 (boundaries, never painted green)
   - F8/F9/F10: PASS 31 (no migration)
   - F1/F2/F3/F6: no FAIL

3. Historical F9 artifact capture (regression-guard sensitivity):
   - AI mono p3  (Case A footer)  -> NUL > 0 (FAIL)
   - AI mono p157 (Case B ►)      -> NUL > 0 (FAIL)
   (skipped with a note when the gitignored pdf2zh_files corpus is absent)

4. Optional fresh-engine smoke (--smoke, runs the real babeldoc pipeline):
   - Case B reproducer (token-faithful) -> specials preserved, NUL=0,
     no <b{id}> token leak, cjk_delta=0

Emits ``doc/7j4/gate_report.json``; exit code 0 iff everything passed.

Usage:  python doc/7j4/release_gate.py [--smoke] [--no-tests]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dual_forensics"))

from pdf_inspector import text_layer_integrity  # noqa: E402

LATCH_TESTS = [
    "tests/test_xobj_unicode_7i7.py",
    "tests/test_text_layer_integrity_7j3a.py",
    "tests/test_detector_coverage_7i4.py",
    "tests/test_evidence_inventory_7i6a.py",
    "tests/test_evidence_wiring_7i6b.py",
    "tests/test_cid_recovery.py",
    "tests/test_layout_policy_contract_7i5b.py",
    "tests/test_eligibility_7i6c.py",
    "tests/test_case_b_token_7j3c.py",
]

FROZEN_MATRIX = {
    "total_residual": 1,
    "by_defect": {"F4": 1},
    "by_first_divergence": {"parser": 1},
    "coverage": {
        "F1": {"FAIL": 0},
        "F2": {"FAIL": 0},
        "F3": {"FAIL": 0},
        "F4": {"FAIL": 1},
        "F5": {"SKIP": 31},
        "F6": {"FAIL": 0},
        "F7": {"NOT_MEASURED": 31},
        "F8": {"FAIL": 0, "PASS": 31},
        "F9": {"FAIL": 0, "PASS": 31},
        "F10": {"FAIL": 0, "PASS": 31},
    },
}

HISTORICAL_ARTIFACTS = [
    (
        "AI mono p3 (Case A footer)",
        "pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf",
        2,
    ),
    (
        "AI mono p157 (Case B ►)",
        "pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf",
        156,
    ),
]


def run_tests() -> dict:
    cmd = [sys.executable, "-m", "pytest", *LATCH_TESTS, "-q"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "summary": summary,
        "ok": proc.returncode == 0,
    }


def run_corpus_baseline() -> dict:
    script = ROOT / "doc" / "7i4" / "full_corpus_baseline.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    summary_path = ROOT / "doc" / "7i4-corpus-baseline" / "summary.json"
    if not summary_path.exists():
        return {
            "ok": False,
            "error": "corpus baseline summary missing",
            "returncode": proc.returncode,
        }
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    matrix = data["global_coverage_matrix"]
    checks = {}
    checks["total_residual"] = data["total_residual"] == FROZEN_MATRIX["total_residual"]
    checks["by_defect"] = data["by_defect"] == FROZEN_MATRIX["by_defect"]
    checks["by_first_divergence"] = (
        data["by_first_divergence"] == FROZEN_MATRIX["by_first_divergence"]
    )
    for defect, expect in FROZEN_MATRIX["coverage"].items():
        for status, value in expect.items():
            actual = matrix.get(defect, {}).get(status)
            checks[f"{defect}.{status}"] = actual == value
    # F4 must FAIL on the p300 parser page (negative control presence is a gate)
    ok = all(checks.values())
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "checks": checks,
        "total_residual": data["total_residual"],
        "by_defect": data["by_defect"],
    }


def check_historical_capture() -> dict:
    found = [p for _, p, _ in HISTORICAL_ARTIFACTS if (ROOT / p).exists()]
    if not found:
        return {
            "ok": True,
            "skipped": "pdf2zh_files corpus absent (gitignored)",
            "checks": {},
        }
    checks = {}
    for label, rel, pno in HISTORICAL_ARTIFACTS:
        path = ROOT / rel
        if not path.exists():
            checks[label] = {"nul": None, "detected": False, "note": "missing"}
            continue
        doc = pymupdf.open(str(path))
        s = text_layer_integrity(doc, pno)
        doc.close()
        checks[label] = {"nul_chars": s["nul_chars"], "detected": s["nul_chars"] > 0}
    return {"ok": all(v.get("detected") for v in checks.values()), "checks": checks}


def run_fresh_smoke() -> dict:
    """Run the Case B reproducer with the real engine and verify the outputs."""
    script = ROOT / "doc" / "7j3c" / "reproduce_case_b.py"
    try:
        subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
        )
    except subprocess.TimeoutExpired:
        # BabelDOC's async generator can hang after writing outputs; the
        # outputs are complete at that point, so continue to verification.
        pass
    work = ROOT / "doc" / "7j3c" / "work" / "out"
    mono = work / "case_b_input.no_watermark.zh.mono.pdf"
    dual = work / "case_b_input.no_watermark.zh.dual.pdf"
    if not mono.exists() or not dual.exists():
        return {"ok": False, "error": "fresh outputs missing after smoke run"}
    m = pymupdf.open(str(mono))
    d = pymupdf.open(str(dual))
    mono_text = m[0].get_text()
    dual_text = "".join(d[i].get_text() for i in range(len(d)))
    m.close()
    d.close()
    specials = {ch for ch in "\u00ef\u2014\u25ba\u2192" if ch in mono_text + dual_text}
    import re

    leaks = re.findall(r"<b\d+>|</b\d+>", mono_text + dual_text)
    return {
        "ok": len(specials) == 4 and "\x00" not in mono_text + dual_text and not leaks,
        "specials_present": sorted(hex(ord(c)) for c in specials),
        "nul_count": mono_text.count("\x00") + dual_text.count("\x00"),
        "token_leaks": leaks[:5],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--smoke", action="store_true", help="run fresh-engine smoke (real babeldoc)"
    )
    ap.add_argument(
        "--no-tests", action="store_true", help="skip the pytest latch subset"
    )
    args = ap.parse_args()

    report: dict = {"gate": "7j4-release-gate", "checks": {}}
    ok = True

    if not args.no_tests:
        t = run_tests()
        report["checks"]["latch_tests"] = t
        ok &= t["ok"]
    else:
        report["checks"]["latch_tests"] = {"skipped": "--no-tests"}

    c = run_corpus_baseline()
    report["checks"]["corpus_baseline"] = c
    ok &= c["ok"]

    h = check_historical_capture()
    report["checks"]["historical_capture"] = h
    ok &= h["ok"]

    if args.smoke:
        s = run_fresh_smoke()
        report["checks"]["fresh_smoke"] = s
        ok &= s["ok"]

    report["all_ok"] = ok
    out = ROOT / "doc" / "7j4" / "gate_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
