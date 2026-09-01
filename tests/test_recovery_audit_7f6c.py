# -*- coding: utf-8 -*-
"""Commit 7F-6c-4 — layout-level recovery audit metrics tests.

Locks ``pdf2zh.semantic.eval.recovery_audit`` — the independent, per-kind
recovery-behavior metrics computed over the unified 7F-6a contract:

- ``recovery_policy_integrity`` — no stale ``wrap``/``no_action`` decision
  while overflowing; no CLIP on a TOC title; decision ∈ the kind's ladder.
- ``recovery_bounded`` — steps within the kind's hard bound.
- ``list_recovery_integrity`` — marker never recovered; content/continuation
  policy-consistent and bounded.
- ``toc_recovery_integrity`` — page never recovered; title never CLIP.
- ``list/toc_recovery_steps`` / ``_font_size`` / ``toc_recovery_overflow`` —
  raw observables, never synthesized into one score.
"""

import pytest

from pdf2zh.semantic.eval.recovery_audit import (
    audit_code,
    audit_flow,
    audit_list,
    audit_recovery,
    audit_toc,
)
from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.list_layout import layout_list_item
from pdf2zh.semantic.layout.overflow import lay_out
from pdf2zh.semantic.layout.primitives import FlowText, PreservedRegion
from pdf2zh.semantic.layout.recovery import budget_for_kind
from pdf2zh.semantic.layout.toc_layout import layout_toc_entry
from pdf2zh.semantic.models import ListItemNode


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


# ---------------------------------------------------------------------------
# atomic results: flow / code
# ---------------------------------------------------------------------------


def test_flow_short_audit_no_recovery():
    r = adaptive_layout(
        FlowText(text="Hello", origin=(0.0, 0.0), max_width=200.0),
        measure=_measure,
        avail_width=200.0,
        avail_height=400.0,
        font_size=10.0,
        budget=budget_for_kind("flow"),
    )
    a = audit_recovery(r)
    assert a["recovery_policy_integrity"] == 1.0
    assert a["recovery_bounded"] == 1.0
    assert a["decision"] is None and a["steps"] == []
    f = audit_flow(r)
    assert f["flow_recovery_integrity"] == 1.0
    assert f["recovery_steps"] == 0


def test_flow_clip_audit_policy_consistent():
    r = adaptive_layout(
        FlowText(text="A" * 60, origin=(0.0, 0.0), max_width=40.0, max_height=400.0),
        measure=_measure,
        avail_width=40.0,
        avail_height=400.0,
        font_size=10.0,
        budget=budget_for_kind("flow"),
    )
    assert r.overflow is True
    a = audit_recovery(r)
    # decision reflects what ran (clip / shrink), never a stale wrap
    assert a["recovery_policy_integrity"] == 1.0
    assert a["decision"] not in ("no_action", "wrap")
    assert a["recovery_bounded"] == 1.0
    assert len(a["steps"]) <= 3


def test_code_audit_never_recovers():
    prim = PreservedRegion(text="def f():", bbox=(10.0, 10.0, 300.0, 26.0))
    r = lay_out(prim, measure=_measure, font_size=10.0)
    a = audit_code(r)
    assert a["code_recovery_integrity"] == 1.0
    assert a["recovery_steps"] == 0
    # the unified audit also sees a clean preserved result as policy-consistent
    assert audit_recovery(r)["recovery_policy_integrity"] == 1.0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _list_item(content, content_width=200.0):
    return ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=52.0,
        content_width=content_width,
        y=700.0,
        content=content,
    )


def test_list_audit_marker_never_recovered():
    agg = layout_list_item(
        _list_item("A" * 1000, content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text="A" * 1000,
    )
    assert agg.marker.lines == ["1."]
    assert not agg.marker.recovery_steps and not agg.marker.recovery_decision
    a = audit_list(agg)
    assert a["list_recovery_integrity"] == 1.0
    assert a["list_recovery_steps"] <= 3
    assert a["list_recovery_font_size"] <= 1.0  # shrink never grows the font


def test_list_audit_clean_item():
    agg = layout_list_item(
        _list_item("First item"),
        measure=_measure,
        font_size=10.0,
        content_text="First item",
    )
    a = audit_list(agg)
    assert a["list_recovery_integrity"] == 1.0
    assert a["list_recovery_steps"] == 0
    assert a["list_recovery_font_size"] == 1.0


# ---------------------------------------------------------------------------
# toc
# ---------------------------------------------------------------------------


def _entry(
    page_x=500.0, title_x=72.0, number="", page_number="12", leader_present=True
):
    return {
        "title": (f"{number} Introduction").strip(),
        "number": number,
        "title_only": "Introduction",
        "level": 0,
        "page_number": page_number,
        "title_x": title_x,
        "page_x": page_x,
        "indent": title_x,
        "dot_leader": "......",
        "leader_present": leader_present,
        "continuation": [],
        "bbox": [title_x, 0.0, page_x, 16.0],
    }


def test_toc_audit_wrap_no_clip():
    agg = layout_toc_entry(
        _entry(),
        measure=_measure,
        size=10.0,
        y=750.0,
        translated_title=("word " * 30).strip(),
    )
    assert agg.recovery["decision"] == "wrap"
    a = audit_toc(agg)
    assert a["toc_recovery_integrity"] == 1.0
    assert a["toc_recovery_steps"] == 1
    assert a["toc_recovery_font_size"] == 1.0
    assert a["toc_recovery_overflow"] == 1.0  # not overflowing → trivially honest
    # the page channel (FixedColumn) never carries recovery
    assert not agg.page.recovery_steps and not agg.page.recovery_decision


def test_toc_audit_shrink_font_ratio():
    agg = layout_toc_entry(
        _entry(),
        measure=_measure,
        size=10.0,
        y=750.0,
        translated_title=("word " * 60).strip(),
    )
    assert agg.recovery["decision"] == "shrink"
    a = audit_toc(agg)
    assert a["toc_recovery_integrity"] == 1.0
    assert a["toc_recovery_steps"] >= 2
    assert a["toc_recovery_font_size"] < 1.0  # genuinely shrank
    assert "CLIP" not in (agg.title.recovery_steps or [])


def test_toc_audit_preserve_overflow_honest():
    agg = layout_toc_entry(
        _entry(page_x=100.0),
        measure=_measure,
        size=10.0,
        y=750.0,
        translated_title=("word " * 120).strip(),
    )
    assert agg.overflow is True
    assert agg.recovery["decision"] == "preserve_overflow"
    a = audit_toc(agg)
    # over-budget + explicit overflow → honest
    assert a["toc_recovery_overflow"] == 1.0
    assert a["toc_recovery_integrity"] == 1.0
    assert "CLIP" not in (agg.title.recovery_steps or [])


def test_toc_audit_silent_overflow_is_dishonest():
    """A synthetic dishonest record: overflow set but no recovery ran → the
    overflow metric drops (the audit is not fooled by a silent fit)."""
    from pdf2zh.semantic.layout.overflow import LayoutResult, OverflowPolicy

    fake_title = LayoutResult(
        text="x",
        lines=["x"],
        line_widths=[5.0],
        overflow=False,
        policy=OverflowPolicy.WRAP,
        font_size=10.0,
        primitive_kind="anchor",
    )
    fake_agg = type(
        "FakeToc",
        (),
        {
            "title": fake_title,
            "page": None,
            "number": None,
            "leader": None,
            "continuation": [],
            "overflow": True,  # claims overflow…
        },
    )()
    a = audit_toc(fake_agg)
    # …but no channel actually overflowed and no recovery ran → dishonest
    assert a["toc_recovery_overflow"] == 0.0


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__]))
