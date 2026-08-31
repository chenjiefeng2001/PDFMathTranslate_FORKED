# -*- coding: utf-8 -*-
"""Commit 7F-6b — Flow officially consumes Recovery (architecture gate).

Locks the Flow behavior contract before List / TOC / Code are touched:

    FlowText
        ↓  lay_out()                    (baseline; WRAP is flow's default policy)
        ↓  classify_reason / decide_recovery   (recovery.py — policy)
        ↓  adaptive_layout()            (7F-6b: bounded WRAP → SHRINK → CLIP)
        ↓  LayoutResult (with recovery record)
        ↓  renderer draws only

Guarantees (7F-6b):

1. short text            → overflow=False, recovery=None;
2. long English / CJK / mixed → wrap first (WRAP is flow's default policy);
3. unbreakable token     → WRAP is skipped, SHRINK engages directly;
4. WRAP insufficient     → SHRINK (clamped by min_font_size), stops when it
   fits — never proceeds to CLIP;
5. SHRINK insufficient   → CLIP, overflow stays True (never silent);
6. ``recovery.steps`` mirrors reality — no ``overflow=True`` with a stale
   ``decision="wrap"`` / ``no_action``;
7. execution is bounded  — at most WRAP(1) → SHRINK(1) → CLIP(1), never a
   ``while overflow`` loop (10000-char pathological token terminates);
8. ``final_font_size <= original_font_size`` and ``>= min_font_size``;
9. recovery record is JSON-safe (7F-6a unified ``recovery`` member);
10. renderer stays draw-only — no wrap/shrink/clip execution, and
    ``magicpdf_renderer`` never imports the layout/recovery layer;
11. geometry is never recomputed by recovery (origin / bbox passthrough);
12. the evaluator can detect Flow overflow (end-to-end magicpdf
    ``stats["flow_overflow"]``).
"""

import inspect
import json

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import LayoutResult, OverflowPolicy
from pdf2zh.semantic.layout.primitives import FlowText
from pdf2zh.semantic.layout.recovery import LayoutBudget, budget_for_kind
from pdf2zh.semantic.renderer.flow import render_flow_text


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _flow(text, w=200.0, h=400.0):
    return FlowText(text=text, origin=(40.0, 40.0), max_width=w, max_height=h)


def _flow_budget():
    return budget_for_kind("flow")


# ---------------------------------------------------------------------------
# 1. short text → no recovery
# ---------------------------------------------------------------------------


def test_short_text_no_recovery():
    out = render_flow_text(
        "Hello", origin=(72.0, 722.0), max_width=468.0, max_height=22.0,
        font_size=12.0, measure=_measure,
    )
    assert out["overflow"] is False
    assert out["recovery"] is None
    assert out["lines"] == ["Hello"]
    assert out["policy"] == OverflowPolicy.WRAP.value


def test_short_text_adaptive_no_recovery():
    r = adaptive_layout(
        _flow("A short line"), measure=_measure, font_size=10.0,
        avail_width=200.0, avail_height=400.0, budget=_flow_budget(),
    )
    assert isinstance(r, LayoutResult)
    assert r.overflow is False
    assert r.recovery is None
    assert r.recovery_steps == []


# ---------------------------------------------------------------------------
# 2. long English / CJK / mixed → WRAP first
# ---------------------------------------------------------------------------


def test_long_english_prefers_wrap():
    text = "This is a long translated paragraph that must wrap over several lines"
    out = render_flow_text(
        text, origin=(72.0, 722.0), max_width=120.0, max_height=400.0,
        font_size=10.0, measure=_measure,
    )
    assert len(out["lines"]) >= 2
    assert out["overflow"] is False
    # WRAP is flow's default policy: a clean wrap is NOT an explicit recovery
    assert out["recovery"] is None
    assert out["font_size"] == 10.0  # wrap keeps the font unchanged


def test_cjk_wraps():
    text = "这是一段很长的中文译文内容" * 3
    out = render_flow_text(
        text, origin=(72.0, 722.0), max_width=120.0, max_height=400.0,
        font_size=10.0, measure=_measure,
    )
    assert len(out["lines"]) >= 2
    assert out["overflow"] is False


def test_mixed_cjk_english_wraps():
    text = "English 中文混合 mixed text 内容内容内容内容内容"
    out = render_flow_text(
        text, origin=(72.0, 722.0), max_width=100.0, max_height=400.0,
        font_size=10.0, measure=_measure,
    )
    assert len(out["lines"]) >= 2
    assert out["overflow"] is False


# ---------------------------------------------------------------------------
# 3. unbreakable token → WRAP skipped, SHRINK engages directly
# ---------------------------------------------------------------------------


def test_unbreakable_token_skips_wrap_goes_shrink():
    r = adaptive_layout(
        _flow("A" * 40, w=100.0, h=400.0), measure=_measure, font_size=10.0,
        avail_width=100.0, avail_height=400.0, budget=_flow_budget(),
    )
    assert r.overflow is False          # SHRINK fixed it (fits at min size)
    assert r.recovery_steps == ["SHRINK"]  # WRAP never ran for an unbreakable token
    assert r.recovery_decision == "shrink"
    assert r.font_size == 5.0


# ---------------------------------------------------------------------------
# 4. WRAP insufficient → SHRINK, stop when it fits (never CLIP)
# ---------------------------------------------------------------------------


def test_wrap_then_shrink_stops_before_clip():
    """Height overflow after WRAP; SHRINK shrinks the font so the single line
    fits width → stops, never proceeds to CLIP."""
    text = "word " * 13  # 64 chars; wraps into 2 lines (height overflow)
    r = adaptive_layout(
        _flow(text, w=200.0, h=10.0), measure=_measure, font_size=10.0,
        avail_width=200.0, avail_height=10.0, budget=_flow_budget(),
    )
    assert r.recovery_steps[0] == "WRAP"
    assert r.recovery_steps == ["WRAP", "SHRINK"]
    assert r.overflow is False
    assert r.recovery_decision == "shrink"
    assert "CLIP" not in r.recovery_steps


# ---------------------------------------------------------------------------
# 5. SHRINK insufficient → CLIP (overflow stays True, never silent)
# ---------------------------------------------------------------------------


def test_shrink_insufficient_goes_clip():
    r = adaptive_layout(
        _flow("A" * 60, w=100.0, h=400.0), measure=_measure, font_size=10.0,
        avail_width=100.0, avail_height=400.0, budget=_flow_budget(),
    )
    assert r.recovery_steps == ["SHRINK", "CLIP"]
    assert r.overflow is True
    assert r.recovery_decision == "clip"
    assert len("".join(r.lines)) < 60  # truncated — but never silent


def test_full_ladder_steps_order():
    # 7I-5C: with a *narrow* box the wrapped text cannot fit even after
    # SHRINK re-wraps, so the full WRAP -> SHRINK -> CLIP ladder runs in order.
    # (A wider box that re-wrap can fit now stops at WRAP -> SHRINK.)
    text = "word " * 60
    r = adaptive_layout(
        _flow(text, w=4.0, h=400.0), measure=_measure, font_size=10.0,
        avail_width=4.0, avail_height=400.0, budget=_flow_budget(),
    )
    assert r.recovery_steps == ["WRAP", "SHRINK", "CLIP"]
    assert r.overflow is True
    assert r.recovery_decision == "clip"


def test_no_stale_decision_when_overflow():
    """overflow=True must never carry a stale wrap/no_action decision."""
    # 7I-5C: both inputs genuinely overflow (narrow wrapable box, unbreakable
    # token) so overflow must never carry a stale wrap/no_action decision.
    for text, w, h in [("word " * 60, 4.0, 400.0), ("A" * 80, 30.0, 300.0)]:
        out = render_flow_text(
            text, origin=(0.0, 0.0), max_width=w, max_height=h,
            font_size=10.0, measure=_measure,
        )
        assert out["overflow"] is True
        assert out["recovery"]["decision"] not in ("no_action", "wrap")


# ---------------------------------------------------------------------------
# 6/7. bounded execution + pathological inputs terminate
# ---------------------------------------------------------------------------


def test_recovery_bounded_max_three_steps():
    text = "word " * 60
    r = adaptive_layout(
        _flow(text, w=100.0, h=40.0), measure=_measure, font_size=10.0,
        avail_width=100.0, avail_height=40.0, budget=_flow_budget(),
    )
    assert len(r.recovery_steps) <= 3  # WRAP(1) → SHRINK(1) → CLIP(1)


def test_pathological_huge_token_terminates():
    """A 10000-char unbreakable token must terminate without a while-loop."""
    huge = "A" * 10000
    r = adaptive_layout(
        _flow(huge, w=10.0, h=10.0), measure=_measure, font_size=10.0,
        avail_width=10.0, avail_height=10.0, budget=_flow_budget(),
    )
    assert len(r.recovery_steps) <= 3
    assert r.overflow is True
    # and via the full render path too (never hangs)
    out = render_flow_text(
        huge, origin=(0.0, 0.0), max_width=10.0, max_height=10.0,
        font_size=10.0, measure=_measure,
    )
    assert out["overflow"] is True
    assert len((out["recovery"] or {}).get("steps", [])) <= 3


# ---------------------------------------------------------------------------
# 8. budget clamps: min_font_size honored, final <= original
# ---------------------------------------------------------------------------


def test_min_font_size_honored():
    b = LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True,
                     min_font_size=8.0)
    r = adaptive_layout(
        _flow("A" * 60, w=100.0, h=400.0), measure=_measure, font_size=10.0,
        avail_width=100.0, avail_height=400.0, budget=b,
    )
    assert r.font_size >= 8.0 - 1e-6
    assert r.recovery["final_font_size"] >= 8.0 - 1e-6
    assert r.recovery["final_font_size"] <= r.recovery["original_font_size"] + 1e-6


def test_final_font_never_exceeds_original():
    for text, w, h in [
        ("word " * 60, 100.0, 40.0),
        ("A" * 40, 100.0, 400.0),
        ("word " * 13, 200.0, 10.0),
    ]:
        r = adaptive_layout(
            _flow(text, w=w, h=h), measure=_measure, font_size=10.0,
            avail_width=w, avail_height=h, budget=_flow_budget(),
        )
        if r.recovery is not None:
            assert r.recovery["final_font_size"] <= \
                r.recovery["original_font_size"] + 1e-6


def test_allow_shrink_false_is_honest_preserve():
    """Explicit opt-out: WRAP only, then honest PRESERVE_OVERFLOW — never a
    silent shrink/clip, and the recovery record says so."""
    out = render_flow_text(
        "A" * 80, origin=(0.0, 0.0), max_width=30.0, max_height=300.0,
        font_size=10.0, measure=_measure, allow_shrink=False,
    )
    assert out["overflow"] is True
    steps = (out["recovery"] or {}).get("steps", [])
    assert "SHRINK" not in steps and "CLIP" not in steps
    assert out["recovery"]["decision"] == "preserve_overflow"


# ---------------------------------------------------------------------------
# 9. recovery record is JSON-safe
# ---------------------------------------------------------------------------


def test_recovery_json_safe():
    r = adaptive_layout(
        _flow("word " * 60, w=100.0, h=40.0), measure=_measure, font_size=10.0,
        avail_width=100.0, avail_height=40.0, budget=_flow_budget(),
    )
    d = r.to_dict()
    json.dumps(d)
    assert "recovery" in d
    assert set(d["recovery"]) >= {
        "reason", "decision", "steps", "original_font_size", "final_font_size",
    }
    # render_flow_text payload is JSON-safe too
    out = render_flow_text(
        "A" * 60, origin=(0.0, 0.0), max_width=40.0, max_height=400.0,
        font_size=10.0, measure=_measure,
    )
    json.dumps(out)
    assert isinstance(out["recovery"], dict)


# ---------------------------------------------------------------------------
# 10. renderer stays draw-only (architecture locks)
# ---------------------------------------------------------------------------


def test_flow_renderer_never_executes_recovery():
    import pdf2zh.semantic.renderer.flow as mod

    src = inspect.getsource(mod)
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src
    assert "adaptive_layout(" in src  # bounded executor, not hand-rolled recovery


def test_magicpdf_renderer_never_imports_recovery_layer():
    import pdf2zh.v3.magicpdf_renderer as mod

    src = inspect.getsource(mod)
    assert "semantic.layout" not in src      # no recovery/adaptive import
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src


def test_recovery_layer_never_imports_renderer_or_translator():
    """Docstrings stripped: prose may describe the pipeline, executable code
    must never reference the renderer / translator / magicpdf layers."""
    import ast
    import pdf2zh.semantic.layout.recovery as rec

    tree = ast.parse(inspect.getsource(rec))

    def _clean(body):
        return [
            n for n in body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    src = ast.unparse(tree)
    for banned in ("renderer", "translator", "magicpdf", "flow_sidechannel"):
        assert banned not in src


# ---------------------------------------------------------------------------
# 11. geometry is never recomputed by recovery
# ---------------------------------------------------------------------------


def test_geometry_not_recomputed_by_recovery():
    out = render_flow_text(
        "A" * 200, origin=(72.0, 722.0), max_width=40.0, max_height=400.0,
        font_size=10.0, measure=_measure,
    )
    assert out["overflow"] is True
    assert out["recovery"] is not None
    # first-line anchor is verbatim from the primitive origin, recovery or not
    assert out["commands"][0]["x"] == 72.0
    assert out["commands"][0]["y"] == 722.0
    # the settled font reaches the draw command (SHRINK carries its size)
    assert out["commands"][0]["font_size"] == out["font_size"]


# ---------------------------------------------------------------------------
# 12. evaluator can detect Flow overflow (end-to-end)
# ---------------------------------------------------------------------------


def _block(text, translated, x0=72.0, y0=700.0, x1=540.0, y1=722.0):
    from pdf2zh.v3.canonical_page import BlockModel, LineModel, SpanModel

    line = LineModel(text=text, baseline=0.0, x0=x0, y0=y0, x1=x1, y1=y1)
    line.spans.append(
        SpanModel(size=12.0, text=text, x0=x0, y0=y0, x1=x1, y1=y1)
    )
    return BlockModel(
        text=text, kind="paragraph", x0=x0, y0=y0, x1=x1, y1=y1,
        lines=[line], metadata={"translated": translated},
    )


def test_magicpdf_surfaces_flow_overflow():
    from pdf2zh.v3.flow_sidechannel import build_block_flow_payload
    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

    payload = build_block_flow_payload(
        _block(text="Source", translated="A" * 500, x0=72.0, y0=700.0, x1=120.0, y1=722.0)
    )
    # the recovery happened at layout time — the evaluator can see it
    assert payload["overflow"] is True
    assert payload["recovery"] is not None
    assert "CLIP" in payload["recovery"]["steps"]

    entry = {
        "block_id": "p0_flow", "page": 0, "kind": "paragraph",
        "text": "Source", "translated": "A" * 500,
        "render_path": "translate_refit",
        "src_box": [72.0, 700.0, 120.0, 722.0],
        "dst_box": [72.0, 700.0, 120.0, 722.0],
        "font_size": 12.0,
        "render_payload": payload,
    }
    pdf, stats = render_plan_to_pdf(
        [entry], page_sizes={0: [612.0, 792.0]}, cjk_font=True
    )
    assert stats.get("flow_layout_used", 0) == 1  # settled commands drawn
    assert stats.get("flow_overflow", 0) >= 1     # overflow observable downstream
    assert "flow_legacy_fallback" not in stats


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))
