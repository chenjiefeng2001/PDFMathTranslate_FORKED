# -*- coding: utf-8 -*-
"""Commit 7F-6d — Unified Golden Gate (tests/test_golden_gate_7f6d.py).

One consolidated regression gate over the full corpus (7D structural +
7F-5d adaptive-TOC), reusing the existing evaluator — nothing rebuilt::

    evaluate(source, output) → report
        ↓ compare_reports(baseline, current) → regressions
        ↓ assert the complete metric set is present and the gates pass

The metric set locked here is the 7F-6d DoD list: text_exactness / bbox /
font / list columns / toc columns / toc_page_column_stability / toc adaptive
recovery / overflow / outline_destination / code_preserved_bbox.

Every corpus case runs **identity** (same builder for source and output) —
the structural gates must be perfect (1.0 / 0), exactly like the individual
7D / 7F-5d gates.
"""

import pytest

from pdf2zh.semantic.eval import compare_reports, evaluate
from tests.pdf_eval_build import (
    build_cjk,
    build_code,
    build_list,
    build_nested_list,
    build_prose,
    build_toc,
    build_toc_adaptive_cjk_source,
    build_toc_adaptive_extreme_output,
    build_toc_adaptive_short,
    build_toc_adaptive_shrink_output,
    build_toc_adaptive_wrap_output,
    build_toc_multiline,
    build_toc_no_leader,
)

#: every key the gate guarantees to be present and individually readable
GATE_KEYS = [
    "text_exactness",
    "bbox_mean_delta",
    "bbox_max_delta",
    "font_match_rate",
    "bold_accuracy",
    "italic_accuracy",
    "list_marker_x_accuracy",
    "list_content_x_accuracy",
    "list_continuation_x_accuracy",
    "list_wrap_integrity",
    "list_nested_geometry_accuracy",
    "toc_title_x_accuracy",
    "toc_page_x_accuracy",
    "toc_page_number_accuracy",
    "toc_level_accuracy",
    "toc_leader_integrity",
    "toc_continuation_x_accuracy",
    "toc_page_column_stability",
    "toc_adaptive_wrap_integrity",
    "toc_adaptive_font_size",
    "toc_adaptive_overflow",
    "outline_destination_accuracy",
    "code_preserved_bbox",
    "overflow_count",
]

#: structural gates that must be perfect (1.0 / 0) on an identity corpus
PERFECT_GATES = [
    "list_marker_x_accuracy",
    "list_content_x_accuracy",
    "list_continuation_x_accuracy",
    "list_wrap_integrity",
    "list_nested_geometry_accuracy",
    "toc_title_x_accuracy",
    "toc_page_x_accuracy",
    "toc_page_number_accuracy",
    "toc_level_accuracy",
    "toc_leader_integrity",
    "toc_continuation_x_accuracy",
    "toc_page_column_stability",
    "toc_adaptive_wrap_integrity",
    "toc_adaptive_font_size",
    "toc_adaptive_overflow",
    "outline_destination_accuracy",
    "code_preserved_bbox",
]

CORPUS = [
    ("code", build_code),
    ("list", build_list),
    ("nested", build_nested_list),
    ("toc", build_toc),
    ("toc_multiline", build_toc_multiline),
    ("toc_no_leader", build_toc_no_leader),
    ("style", build_prose),
    ("cjk", build_cjk),
    ("toc_adaptive_short", build_toc_adaptive_short),
    ("toc_adaptive_wrap", build_toc_adaptive_wrap_output),
    ("toc_adaptive_shrink", build_toc_adaptive_shrink_output),
    ("toc_adaptive_extreme", build_toc_adaptive_extreme_output),
    ("toc_adaptive_cjk", build_toc_adaptive_cjk_source),
]


def _identity_metrics(tmp_path, builder):
    src = str(tmp_path / "src.pdf")
    out = str(tmp_path / "out.pdf")
    builder(src)
    builder(out)
    rep = evaluate(src, out)
    res = compare_reports(rep, rep)
    assert res["status"] == "pass", res["regressions"]
    return rep["metrics"]


def test_unified_golden_gate_all_corpus(tmp_path):
    for name, builder in CORPUS:
        m = _identity_metrics(tmp_path, builder)
        for k in GATE_KEYS:
            assert k in m, f"{name} 缺指标 {k}"
            assert isinstance(m[k], (int, float)), f"{name}::{k}"
        for k in PERFECT_GATES:
            assert m[k] == 1.0, f"{name}::{k} = {m[k]}（identity 必须完美）"
        assert m["overflow_count"] == 0, f"{name} identity 不应溢出页面"


def test_unified_golden_gate_metrics_are_individual(tmp_path):
    """每个指标独立可读 —— 报告是扁平标量 dict，绝不合成单一 score
    （7F-6c「每个指标独立，不合成一个 score」原则的最终锁定）。"""
    m = _identity_metrics(tmp_path, build_toc_adaptive_shrink_output)
    assert isinstance(m, dict)
    assert all(isinstance(v, (int, float)) for v in m.values())
    # 不存在合成总分；recovery 行为由独立指标表达
    assert not any(k in m for k in ("score", "fidelity", "overall"))
    # 恢复行为可独立观察：steps / font 由 layout 级 recovery_audit 表达
    from pdf2zh.semantic.eval.recovery_audit import audit_toc

    from pdf2zh.semantic.layout.toc_layout import layout_toc_entry

    agg = layout_toc_entry(
        {
            "title_x": 72.0,
            "page_x": 500.0,
            "level": 0,
            "number": "",
            "page_number": "12",
            "leader_present": True,
            "continuation": [],
            "bbox": [72.0, 0.0, 500.0, 16.0],
        },
        size=10.0,
        y=750.0,
        translated_title=("word " * 60).strip(),
    )
    a = audit_toc(agg)
    assert a["toc_recovery_steps"] >= 2  # WRAP + SHRINK executed
    assert a["toc_recovery_font_size"] < 1.0  # shrink 真实发生
    assert "toc_recovery_overflow" in a
    assert len(GATE_KEYS) == len(set(GATE_KEYS))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__]))
