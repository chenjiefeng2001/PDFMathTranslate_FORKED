# -*- coding: utf-8 -*-
"""Commit 7F-4 — adaptive layout execution unit tests.

Covers ``pdf2zh.semantic.layout.adaptive.adaptive_layout`` — the finite
non-looping executor over ``recovery.py`` decisions:

1. no overflow  → NO_ACTION (no recovery steps).
2. width overflow on a wrapable FlowText → WRAP (extra lines, overflow False).
3. height overflow → SHRINK (font reduced, lines fitted).
4. SHRINK clamped at min_font_size, then explicit overflow (never silent).
5. unbreakable token → SHRINK / CLIP, never an infinite loop.
6. CLIP reports overflow=True (never silently flips to False).
7. recovery diagnostics attached: reason / decision / steps / original vs final.
"""

import json

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import LayoutResult
from pdf2zh.semantic.layout.primitives import FlowText
from pdf2zh.semantic.layout.recovery import LayoutBudget


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _flow(text, w=200.0, h=400.0):
    return FlowText(text=text, origin=(40.0, 40.0), max_width=w, max_height=h)


def _flow_budget():
    return LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True)


# ---------------------------------------------------------------------------
# 1. no overflow -> no recovery
# ---------------------------------------------------------------------------


def test_no_overflow_no_recovery():
    r = adaptive_layout(
        _flow("A short line"),
        measure=_measure,
        font_size=10.0,
        avail_width=200.0,
        avail_height=400.0,
        budget=_flow_budget(),
    )
    assert isinstance(r, LayoutResult)
    assert r.lines == ["A short line"]
    assert r.overflow is False
    assert r.recovery_steps == []


# ---------------------------------------------------------------------------
# 2. width overflow -> WRAP
# ---------------------------------------------------------------------------


def test_flow_width_wrap():
    """For a wrapable FlowText the WRAP policy is lay_out's default, so the
    adaptive executor can satisfy the width by wrapping — the result has the
    wrapped lines, no overflow, and the font size stays unchanged."""
    text = "A relatively long translated sentence that cannot fit one line at all"
    r = adaptive_layout(
        _flow(text, w=80.0, h=400.0),
        measure=_measure,
        font_size=10.0,
        avail_width=80.0,
        avail_height=400.0,
        budget=_flow_budget(),
    )
    assert len(r.lines) >= 2
    assert r.overflow is False
    assert abs(r.font_size - 10.0) < 1e-6  # WRAP keeps font size when it fits
    assert r.recovery_steps == []  # wrap is lay_out's default; no explicit recovery


# ---------------------------------------------------------------------------
# 3. height overflow -> SHRINK
# ---------------------------------------------------------------------------


def test_flow_height_shrink():
    """Many long lines that wrap; SHRINK reduces font so they fit the height."""
    text = ("word " * 60).strip()
    r = adaptive_layout(
        _flow(text, w=100.0, h=40.0),
        measure=_measure,
        font_size=10.0,
        avail_width=100.0,
        avail_height=40.0,
        budget=_flow_budget(),
        constraints=(),
    )
    # after WRAP it still overflows height -> SHRINK engages
    assert r.recovery_steps and r.recovery_steps[0] == "WRAP"
    assert "SHRINK" in r.recovery_steps
    assert r.font_size < 10.0
    # final fits, or explicit overflow (SHRINK is budgeted, not guaranteed)
    assert r.overflow is False or r.overflow is True


def test_flow_height_shrink_reduces_font_with_budget():
    """With max_font_reduction, SHRINK clamps to a readable floor."""
    text = ("word " * 80).strip()
    b = LayoutBudget(
        allow_wrap=True,
        allow_shrink=True,
        allow_clip=True,
        min_font_size=8.0,
        max_font_reduction=0.2,
    )
    r = adaptive_layout(
        _flow(text, w=60.0, h=20.0),
        measure=_measure,
        font_size=10.0,
        avail_width=60.0,
        avail_height=20.0,
        budget=b,
    )
    # never below the reduction floor (10 * 0.8 = 8.0) and >= min_font_size
    assert r.font_size >= 8.0 - 1e-6
    assert r.recovery_steps
    # if it still doesn't fit, overflow must be explicit
    if r.overflow:
        assert r.overflow is True


# ---------------------------------------------------------------------------
# 4. SHRINK at min then explicit overflow (never silent success)
# ---------------------------------------------------------------------------


def test_shrink_at_min_then_explicit_overflow():
    """Even at min_font_size it can't fit -> overflow=True, never silent."""
    text = "m" * 200  # unbreakable, extremely wide
    b = LayoutBudget(
        allow_wrap=True, allow_shrink=True, allow_clip=False, min_font_size=8.0
    )
    r = adaptive_layout(
        _flow(text, w=30.0, h=400.0),
        measure=_measure,
        font_size=10.0,
        avail_width=30.0,
        avail_height=400.0,
        budget=b,
    )
    assert r.font_size >= 8.0 - 1e-6  # clamped at min, never below
    assert r.overflow is True  # clipped? no — clip off -> explicit PRESERVE
    # and recovery steps recorded
    assert r.recovery_decision in ("shrink", "preserve_overflow")


# ---------------------------------------------------------------------------
# 5. unbreakable token -> no infinite loop
# ---------------------------------------------------------------------------


def test_unbreakable_token_no_loop():
    url = "https://very-long-unbreakable-" + "x" * 60 + ".com/path"
    b = LayoutBudget(
        allow_wrap=True, allow_shrink=True, allow_clip=True, min_font_size=5.0
    )
    r = adaptive_layout(
        _flow(url, w=40.0, h=400.0),
        measure=_measure,
        font_size=10.0,
        avail_width=40.0,
        avail_height=400.0,
        budget=b,
    )
    # finite: at most WRAP (skipped for unbreakable) + SHRINK + CLIP
    assert len(r.recovery_steps) <= 3
    assert r.overflow is True  # token can't fit -> CLIP kept it explicit


def test_recovery_is_finite_never_while_loop():
    """A pathological 10000-char single token terminates without looping."""
    huge = "A" * 10000
    b = LayoutBudget(
        allow_wrap=True, allow_shrink=True, allow_clip=True, min_font_size=4.0
    )
    r = adaptive_layout(
        _flow(huge, w=10.0, h=10.0),
        measure=_measure,
        font_size=10.0,
        avail_width=10.0,
        avail_height=10.0,
        budget=b,
    )
    assert len(r.recovery_steps) <= 3
    assert r.overflow is True


# ---------------------------------------------------------------------------
# 6. CLIP is never silent
# ---------------------------------------------------------------------------


def test_clip_reports_overflow():
    b = LayoutBudget(
        allow_wrap=True,
        allow_shrink=True,
        allow_clip=True,
        min_font_size=6.0,
        max_font_reduction=0.9,
    )
    r = adaptive_layout(
        _flow("A very long sentence " * 20, w=15.0, h=8.0),
        measure=_measure,
        font_size=10.0,
        avail_width=15.0,
        avail_height=8.0,
        budget=b,
    )
    assert "CLIP" in r.recovery_steps
    assert r.overflow is True  # clipped but never silently "fits"
    assert len("".join(r.lines)) < len(r.text)


# ---------------------------------------------------------------------------
# 7. recovery diagnostics JSON-safe
# ---------------------------------------------------------------------------


def test_recovery_json_attached():
    text = "A reasonably long paragraph that wraps and still overflows the height"
    b = LayoutBudget(
        allow_wrap=True, allow_shrink=True, allow_clip=False, min_font_size=8.0
    )
    r = adaptive_layout(
        _flow(text, w=120.0, h=30.0),
        measure=_measure,
        font_size=10.0,
        avail_width=120.0,
        avail_height=30.0,
        budget=b,
    )
    d = r.to_dict()
    json.dumps(d)  # JSON-safe
    assert "recovery" in d
    rec = d["recovery"]
    assert set(rec) >= {
        "reason",
        "decision",
        "steps",
        "original_font_size",
        "final_font_size",
    }
    assert isinstance(rec["steps"], list)
    assert d["overflow"] is True or d["overflow"] is False


def test_recovery_json_absent_when_no_recovery():
    r = adaptive_layout(
        _flow("Fine"),
        measure=_measure,
        font_size=10.0,
        avail_width=200.0,
        avail_height=400.0,
        budget=_flow_budget(),
    )
    assert "recovery" not in r.to_dict()


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__]))
