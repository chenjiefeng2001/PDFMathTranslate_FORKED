"""阶段三/五/十/十一 扩展能力 — unit tests.

Covers:
  * ConstraintSolver Kiwi engine (auto) vs greedy fallback + advisory skip.
  * 阶段十 QualityGate composite scoring + JSON snapshot round-trip.
  * V8.2 rollout rules + fallback telemetry (feature_flags).
  * 阶段五 TypographyRule (operators).

Run with:
    python -m pytest tests/v3/test_phase3_extensions.py -v
"""
from __future__ import annotations
import json

import pytest

from pdf2zh.v3.visual_tree import BoundingBox
from pdf2zh.v3.constraint_graph import (
    ConstraintPriority, ConstraintRelation, ConstraintGraph, ConstraintSolver,
    KiwiSolver,
)
from pdf2zh.v3.evaluator import (
    EvaluationResult, composite_score, QualityGate, QualityGateResult,
    QualitySnapshot, WEIGHTS,
)
from pdf2zh.v3.feature_flags import (
    FeatureFlags, FallbackTelemetry,
    PercentRolloutRule, PageRangeRolloutRule, DocTypeRolloutRule,
    UserAllowlistRolloutRule, RolloutPolicy,
)
from pdf2zh.v3.operators import TypographyRule


# ── ConstraintSolver engines ─────────────────────────────────────────

def _chain_graph():
    cg = ConstraintGraph()
    cg.add_node("a", "paragraph", bbox=BoundingBox(50, 50, 300, 20))
    cg.add_node("b", "paragraph", bbox=BoundingBox(50, 120, 300, 20))
    cg.add_node("c", "paragraph", bbox=BoundingBox(50, 190, 300, 20))
    cg.add_edge("a", "b", ConstraintRelation.MUST_BELOW, priority="hard",
                gap=10.0)
    cg.add_edge("b", "c", ConstraintRelation.MUST_BELOW, priority="hard",
                gap=10.0)
    return cg


def test_kiwi_solver_available():
    assert KiwiSolver is not None


def test_solver_auto_uses_kiwi_and_orders_nodes():
    cg = _chain_graph()
    solver = ConstraintSolver(cg, 612, 792)
    assert solver.solve(engine="auto") is True
    na, nb, nc = cg.get_node("a"), cg.get_node("b"), cg.get_node("c")
    assert nb.resolved_bbox.y - na.resolved_bbox.y >= 10.0
    assert nc.resolved_bbox.y - nb.resolved_bbox.y >= 10.0


def test_solver_greedy_fallback():
    cg = _chain_graph()
    solver = ConstraintSolver(cg, 612, 792)
    assert solver.solve(engine="greedy") is True
    for n in (cg.get_node("a"), cg.get_node("b"), cg.get_node("c")):
        assert n.resolved_bbox is not None


def test_solver_unknown_engine_falls_back_to_greedy():
    cg = _chain_graph()
    solver = ConstraintSolver(cg, 612, 792)
    assert solver.solve(engine="nope") is True   # unknown → greedy fallback
    for n in (cg.get_node("a"), cg.get_node("b"), cg.get_node("c")):
        assert n.resolved_bbox is not None


def test_advisory_edges_are_not_enforced():
    """Advisory (typography-band) edges must never move blocks far away."""
    cg = ConstraintGraph()
    cg.add_node("a", "paragraph", bbox=BoundingBox(50, 100, 300, 20))
    cg.add_node("b", "paragraph", bbox=BoundingBox(50, 200, 300, 20))
    cg.add_edge("a", "b", ConstraintRelation.MUST_BELOW, priority="typography",
                gap=500.0)
    solver = ConstraintSolver(cg, 612, 792)
    solver.solve(engine="auto")
    b_y = cg.get_node("b").resolved_bbox.y
    assert b_y < 400.0   # not pushed 500pt down by an advisory edge


# ── 阶段十 Quality Gate ──────────────────────────────────────────────

def _sample_result():
    return EvaluationResult(translation_score=95.0, semantic_score=90.0,
                            typography_score=88.0, layout_score=92.0,
                            consistency_score=91.0, total_score=91.5,
                            per_page_scores={
                                1: {"translation": 95, "semantic": 90,
                                    "typography": 88, "layout": 92,
                                    "consistency": 91},
                            })


def test_composite_score_uses_weights():
    r = _sample_result()
    assert composite_score(r) == pytest.approx(
        WEIGHTS["translation"] * 95.0 + WEIGHTS["semantic"] * 90.0
        + WEIGHTS["typography"] * 88.0 + WEIGHTS["layout"] * 92.0
        + WEIGHTS["consistency"] * 91.0, abs=0.01)
    assert composite_score(r, weights={"layout": 1.0}) == 92.0


def test_quality_gate_pass():
    gate = QualityGate(threshold=85.0)
    result = gate.evaluate(_sample_result())
    assert isinstance(result, QualityGateResult)
    assert result.passed
    assert result.total_score >= 85.0
    assert not result.issues
    assert result.per_page_scores[1]["score"] > 80.0


def test_quality_gate_fail_and_per_page():
    low = EvaluationResult(translation_score=60.0, semantic_score=70.0,
                           typography_score=80.0, layout_score=90.0,
                           consistency_score=85.0, total_score=72.0,
                           per_page_scores={
                               1: {"translation": 60, "semantic": 70,
                                   "typography": 80, "layout": 90,
                                   "consistency": 85},
                           })
    gate = QualityGate(threshold=90.0, page_threshold=80.0)
    result = gate.evaluate(low)
    assert not result.passed
    assert any("total" in i for i in result.issues)
    assert any("page 1" in i for i in result.issues)


def test_quality_snapshot_roundtrip(tmp_path):
    snap = QualitySnapshot.capture(_sample_result(), total_score=91.5,
                                   extra={"doc": "x"})
    assert snap["schema"] == "pdf2zh.v3.quality-snapshot"
    path = QualitySnapshot.save(snap, str(tmp_path), tag="unit")
    loaded = QualitySnapshot.load(path)
    assert loaded == snap
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == snap


# ── V8.2 Rollout rules ───────────────────────────────────────────────

def test_percent_rule_is_deterministic_per_user():
    rule = PercentRolloutRule(percent=50.0)
    first = rule.matches(user_id="alice")
    for _ in range(5):
        assert rule.matches(user_id="alice") == first
    # percent boundaries are validated
    with pytest.raises(ValueError):
        PercentRolloutRule(101.0)


def test_percent_rule_covers_full_range():
    assert PercentRolloutRule(100.0).matches(user_id="anyone")
    assert not PercentRolloutRule(0.0).matches(user_id="anyone")


def test_page_range_rule():
    rule = PageRangeRolloutRule([1, 2])
    assert rule.matches(page_num=1)
    assert rule.matches(page_num=2)
    assert not rule.matches(page_num=5)


def test_page_range_rule_expand():
    rule = PageRangeRolloutRule([1, 2], include_external=True)
    assert rule.matches(page_num=1)
    assert rule.matches(page_num=50)


def test_doc_type_rule():
    rule = DocTypeRolloutRule(["paper"])
    assert rule.matches(doc_type="paper")
    assert not rule.matches(doc_type="textbook")


def test_policy_first_match_wins():
    policy = (RolloutPolicy()
              .add(UserAllowlistRolloutRule(["beta-user"]))
              .add(PageRangeRolloutRule([1, 2])))
    assert policy.enabled(user_id="beta-user", page_num=9)
    assert policy.enabled(user_id="other", page_num=2)
    assert not policy.enabled(user_id="other", page_num=5)


def test_fallback_telemetry_records_and_filters():
    sink = FallbackTelemetry()
    sink.record({"reason": "layout_failed", "page": 3})
    sink.record({"reason": "parser_error"})
    assert sink.count() == 2
    assert sink.count("layout_failed") == 1
    assert len(sink.events()) == 2
    sink.clear()
    assert sink.count() == 0


def test_feature_flags_evaluate_with_policy():
    flags = FeatureFlags(use_v4_engine=False,
                         rollout_policy=RolloutPolicy().add(
                             PageRangeRolloutRule([1])))
    assert flags.evaluate(page_num=1)
    assert not flags.evaluate(page_num=2)
    flags.rollout_policy = None
    assert not flags.evaluate(page_num=1)   # static master switch off


def test_feature_flags_record_fallback():
    sink = FallbackTelemetry()
    flags = FeatureFlags(use_v4_engine=True, telemetry=sink)
    flags.record_fallback({"reason": "v4_error"})
    assert sink.count() == 1


# ── 阶段五 TypographyRule ────────────────────────────────────────────

def test_typography_rule_adopts_geometry():
    rule = TypographyRule()
    m = rule.apply("Short translation", source="Short source",
                   bbox=(0, 0, 400, 20), font_size=12.0)
    assert m["rule"] == "adopt_source_geometry"


def test_typography_rule_shrinks_font_on_overflow():
    rule = TypographyRule()
    long_zh = "这段译文非常非常长必须缩小字号才能塞进窄容器内" * 3
    m = rule.apply(long_zh, source="src", bbox=(0, 0, 120, 20),
                   font_size=12.0)
    assert m["rule"] == "shrink_font"
    assert m["font_size"] < 12.0


def test_typography_rule_expands_block():
    rule = TypographyRule()
    m = rule.apply("中文", source="English source text", bbox=(0, 0, 400, 8),
                   font_size=12.0)
    assert m["rule"] in ("expand_block", "shrink_font")

