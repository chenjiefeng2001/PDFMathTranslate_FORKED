# -*- coding: utf-8 -*-
"""Commit 7D — eval.compare / baseline / regression detection tests.

Saves a baseline from a report, reloads it, and verifies that:
- an identical report passes;
- a metric drop (e.g. TOC page column drift) is reported as an explicit
  regression with baseline & current values.
"""

import json

from pdf2zh.semantic.eval import (
    compare_reports,
    evaluate,
    load_baseline,
    save_baseline,
)

from tests.pdf_eval_build import add_page, build_toc, new_doc, write


def _report(tmp_path, broken=False):
    src = str(tmp_path / "src.pdf")
    out = str(tmp_path / "out.pdf")
    build_toc(src)
    if broken:
        doc = new_doc()
        add_page(doc, [
            (72, 80, "Contents", "bold", 14),
            (72, 110, "Introduction .................... 1", "body", 12),
            (96, 135, "Background ........................ 3", "body", 12),
        ])
        write(doc, out)
    else:
        build_toc(out)
    return evaluate(src, out)


def test_save_load_baseline_roundtrip(tmp_path):
    rep = _report(tmp_path)
    baseline_path = str(tmp_path / "baseline.json")
    save_baseline(rep, baseline_path, version="0.0.0", model="mock", commit="abc123")
    doc = load_baseline(baseline_path)
    assert doc["meta"]["pdf2zh_version"] == "0.0.0"
    assert doc["meta"]["model"] == "mock"
    assert doc["meta"]["commit"] == "abc123"
    for key in ("metrics", "meta"):
        assert key in doc
    # JSON-safe on disk
    with open(baseline_path, encoding="utf-8") as fh:
        json.load(fh)


def test_identical_report_passes(tmp_path):
    base = _report(tmp_path)
    cur = _report(tmp_path)
    res = compare_reports(base, cur)
    assert res["status"] == "pass"
    assert res["regressions"] == []


def test_regression_detected(tmp_path):
    base = _report(tmp_path)
    cur = _report(tmp_path, broken=True)
    res = compare_reports(base, cur)
    assert res["status"] == "regression"
    metrics = {r["metric"] for r in res["regressions"]}
    assert "toc_page_x_accuracy" in metrics
    found = [r for r in res["regressions"] if r["metric"] == "toc_page_x_accuracy"][0]
    assert found["baseline"] == 1.0
    assert found["current"] < 0.5
    assert found["worse_lower"] is False  # accuracy: higher is better


def test_overflow_regression_uses_lower_better(tmp_path):
    base = {"overflow_count": 0.0, "toc_page_x_accuracy": 1.0}
    cur = {"overflow_count": 3.0, "toc_page_x_accuracy": 1.0}
    res = compare_reports(base, cur)
    assert res["status"] == "regression"
    found = [r for r in res["regressions"] if r["metric"] == "overflow_count"][0]
    assert found["worse_lower"] is True


def test_metric_noise_within_tolerance_not_regression(tmp_path):
    base = _report(tmp_path)
    cur = dict(base)
    cur["metrics"]["toc_page_x_accuracy"] = 0.98  # within 0.05 tolerance
    res = compare_reports(base, cur)
    assert res["status"] == "pass"