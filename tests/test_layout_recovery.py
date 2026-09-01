"""Commit 7F — overflow diagnosis + recovery policy unit tests (7F-1/7F-2/7F-3).

Covers the pure policy layer in ``pdf2zh/semantic/layout/recovery.py``:

- :class:`OverflowReason` — why a run overflowed (WIDTH / HEIGHT /
  UNBREAKABLE_TOKEN / FIXED_COLUMN_COLLISION / PRESERVED_REGION).
- :func:`classify_reason` — correct inference from a settled LayoutResult.
- :func:`decide_recovery` — the WRAP → SHRINK → CLIP ladder with the
  per-kind exemptions (code / page column / marker never move).
- :class:`LayoutBudget` — budget gating (allow_shrink / allow_clip /
  disallowed wrap).
- :class:`OverflowDiagnosis` — the JSON-safe record a renderer logs.
- :func:`diagnose_overflow` — the single entry point.

All pure data — no renderer, no detector, no translator.
"""

from pdf2zh.semantic.layout.overflow import LayoutResult, OverflowPolicy, lay_out
from pdf2zh.semantic.layout.primitives import (
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)
from pdf2zh.semantic.layout.recovery import (
    LayoutBudget,
    OverflowDiagnosis,
    OverflowReason,
    RecoveryDecision,
    budget_for_kind,
    classify_reason,
    decide_recovery,
    diagnose_overflow,
)


def _m(s, sz):
    """Simple proportional measurer: latin ~0.5em, cjk ~1.0em."""
    w = 0.0
    for ch in s or "":
        if ord(ch) >= 0x2E80:
            w += sz
        else:
            w += sz * 0.5
    return w


# ---------------------------------------------------------------------------
# classify_reason
# ---------------------------------------------------------------------------


def test_classify_width_overflow():
    r = LayoutResult(
        text="a breakable sentence",
        lines=["a breakable sentence"],
        line_widths=[200.0],
        primitive_kind="flow",
        overflow=True,
    )
    assert classify_reason(r, avail_width=100.0) is OverflowReason.WIDTH


def test_classify_unbreakable_token():
    url = "https://very-long-identifier-does-not-wrap"
    r = LayoutResult(
        text=url,
        lines=[url],
        line_widths=[400.0],
        primitive_kind="flow",
        overflow=True,
    )
    assert classify_reason(r, avail_width=100.0) is OverflowReason.UNBREAKABLE_TOKEN


def test_classify_height_overflow():
    r = LayoutResult(
        text="word1 word2 word3",
        lines=["word1", "word2", "word3"],
        line_widths=[40.0, 40.0, 40.0],
        font_size=11.0,
        primitive_kind="flow",
        overflow=True,
    )
    assert (
        classify_reason(r, avail_width=200.0, avail_height=22.0)
        is OverflowReason.HEIGHT
    )


def test_classify_preserved_region():
    r = LayoutResult(
        text="def f():",
        lines=["def f():"],
        line_widths=[30.0],
        primitive_kind="preserved",
        overflow=True,
    )
    assert classify_reason(r, avail_width=20.0) is OverflowReason.PRESERVED_REGION


def test_classify_fixed_column():
    r = LayoutResult(
        text="42",
        lines=["42"],
        line_widths=[16.0],
        primitive_kind="column",
        overflow=False,
    )
    assert classify_reason(r, avail_width=10.0) is OverflowReason.FIXED_COLUMN_COLLISION


# ---------------------------------------------------------------------------
# decide_recovery — the ladder + exemptions
# ---------------------------------------------------------------------------


def test_code_is_never_recovered():
    """PreservedRegion (code) always PRESERVE_OVERFLOW — never wrap/shrink/clip."""
    for reason in OverflowReason:
        d = decide_recovery("preserved", reason)
        assert d is RecoveryDecision.PRESERVE_OVERFLOW, reason


def test_page_column_never_moves():
    for reason in OverflowReason:
        assert decide_recovery("column", reason) is RecoveryDecision.PRESERVE_OVERFLOW


def test_marker_is_never_wrapped_or_shrunk():
    for reason in OverflowReason:
        assert (
            decide_recovery("anchor", reason, target="marker")
            is RecoveryDecision.PRESERVE_OVERFLOW
        )


def test_flow_wrap_then_shrink_then_clip():
    # WRAP when a breakable width overflow can be wrapped
    assert decide_recovery("flow", OverflowReason.WIDTH) is RecoveryDecision.WRAP
    # with budget without wrap -> SHRINK
    b = LayoutBudget(allow_wrap=False, allow_shrink=True, allow_clip=True)
    assert (
        decide_recovery("flow", OverflowReason.WIDTH, budget=b)
        is RecoveryDecision.SHRINK
    )
    # with budget without wrap/shrink -> CLIP
    b2 = LayoutBudget(allow_wrap=False, allow_shrink=False, allow_clip=True)
    assert (
        decide_recovery("flow", OverflowReason.WIDTH, budget=b2)
        is RecoveryDecision.CLIP
    )
    # with no recovery allowed -> PRESERVE_OVERFLOW
    b3 = LayoutBudget(allow_wrap=False, allow_shrink=False, allow_clip=False)
    assert (
        decide_recovery("flow", OverflowReason.WIDTH, budget=b3)
        is RecoveryDecision.PRESERVE_OVERFLOW
    )


def test_unbreakable_token_skips_wrap():
    """UNBREAKABLE_TOKEN cannot wrap -> straight to SHRINK/CLIP."""
    assert (
        decide_recovery("flow", OverflowReason.UNBREAKABLE_TOKEN)
        is RecoveryDecision.SHRINK
    )


def test_toc_title_never_clips():
    """TOC title anchor may wrap/shrink but never CLIP — page via column preserved."""
    b = budget_for_kind("anchor")
    # with a budget that allows wrap+shrink, a width overflow wraps first
    assert (
        decide_recovery("anchor", OverflowReason.WIDTH, budget=b, target="title")
        is RecoveryDecision.WRAP
    )
    # when wrap is disallowed the title shrinks, never clips
    b_noclip = LayoutBudget(allow_wrap=False, allow_shrink=True, allow_clip=False)
    assert (
        decide_recovery("anchor", OverflowReason.WIDTH, budget=b_noclip, target="title")
        is RecoveryDecision.SHRINK
    )


# ---------------------------------------------------------------------------
# LayoutBudget defaults by kind
# ---------------------------------------------------------------------------


def test_budget_defaults_by_kind():
    assert budget_for_kind("flow").allow_shrink is True
    assert budget_for_kind("flow").allow_clip is True
    assert budget_for_kind("anchor").max_extra_lines == 2
    assert budget_for_kind("column").allow_shrink is False
    assert budget_for_kind("column").allow_clip is False
    assert budget_for_kind("preserved").allow_wrap is False
    assert budget_for_kind("preserved").allow_clip is False


# ---------------------------------------------------------------------------
# diagnose_overflow — single entry, JSON-safe record
# ---------------------------------------------------------------------------


def test_diagnose_long_toc_title_wraps_first_or_shrinks():
    """TOC title exceeding avail width follows WRAP -> SHRINK ladder.

    A breakable title with the default anchor budget (wrap enabled) is WRAP;
    when wrap is not allowed it clearly degrades to SHRINK (never CLIP, never
    moving the page column).
    """
    r = LayoutResult(
        text="这是一个非常非常长的章节介绍标题",
        lines=["这是一个非常非常长的章节介绍标题"],
        line_widths=[260.0],
        font_size=10.5,
        primitive_kind="anchor",
        overflow=True,
    )
    d = diagnose_overflow(r, avail_width=160.0, target="title", original_font_size=10.5)
    assert isinstance(d, OverflowDiagnosis)
    assert d.reason is OverflowReason.WIDTH
    assert d.decision is RecoveryDecision.WRAP  # WRAP first per 7F ladder
    assert d.message.startswith("width overflow")

    # wrap disabled -> SHRINK (not CLIP, not PRESERVE_OVERFLOW)
    b = LayoutBudget(allow_wrap=False, allow_shrink=True, allow_clip=False)
    d2 = diagnose_overflow(
        r, avail_width=160.0, target="title", budget=b, original_font_size=10.5
    )
    assert d2.decision is RecoveryDecision.SHRINK
    assert "shrink" in d2.message

    # the record is JSON-safe & self-describing
    j = d.to_dict()
    assert j["reason"] == "width"
    assert j["decision"] == "wrap"


def test_diagnose_flow_wrap_message_counts_extra_lines():
    r = LayoutResult(
        text="a b c",
        lines=["a", "b", "c"],
        line_widths=[30.0, 30.0, 30.0],
        font_size=11.0,
        primitive_kind="flow",
        overflow=True,
    )
    d = diagnose_overflow(r, avail_width=100.0, avail_height=11.0)
    assert d.reason is OverflowReason.HEIGHT
    assert d.decision is RecoveryDecision.WRAP
    assert d.extra_lines == 2


def test_diagnose_unbreakable_flow_goes_to_shrink():
    url = "m" * 60
    r = LayoutResult(
        text=url,
        lines=[url],
        line_widths=[330.0],
        font_size=11.0,
        primitive_kind="flow",
        overflow=True,
    )
    d = diagnose_overflow(r, avail_width=200.0)
    assert d.reason is OverflowReason.UNBREAKABLE_TOKEN
    assert d.decision in (RecoveryDecision.SHRINK, RecoveryDecision.CLIP)


def test_diagnose_code_preserve_overflow():
    r = LayoutResult(
        text="def very_long_unbreakable_function_identifier():",
        lines=["def very_long_unbreakable_function_identifier():"],
        line_widths=[400.0],
        font_size=10.0,
        primitive_kind="preserved",
        overflow=True,
    )
    d = diagnose_overflow(r, avail_width=100.0)
    assert d.decision is RecoveryDecision.PRESERVE_OVERFLOW
    assert d.message == "preserved_region overflow -> preserve_overflow"


# ---------------------------------------------------------------------------
# integration with the real lay_out engine (7F consumes LayoutResult)
# ---------------------------------------------------------------------------


def test_lay_out_then_diagnose_code_is_preserve():
    """A too-wide code line flows through lay_out as PRESERVE, then diagnosis
    says PRESERVE_OVERFLOW — never auto-shrunk.  (Architecture locked.)"""
    code = PreservedRegion(text="def long_function():", bbox=(10, 10, 300, 26))
    r = lay_out(code, measure=_m, font_size=10.0)
    assert r.policy is OverflowPolicy.PRESERVE
    d = diagnose_overflow(r, avail_width=50.0)
    assert d.reason is OverflowReason.PRESERVED_REGION
    assert d.decision is RecoveryDecision.PRESERVE_OVERFLOW


def test_lay_out_then_diagnose_flow_wrap_decision():
    flow = FlowText(
        text="a relatively long translated sentence fits awkwardly",
        origin=(72, 30),
        max_width=60.0,
    )
    r = lay_out(flow, measure=_m, font_size=11.0)
    d = diagnose_overflow(r, avail_width=60.0)
    assert d.decision is RecoveryDecision.WRAP
    assert d.extra_lines > 0


def test_lay_out_then_diagnose_anchor_shrink_decision():
    anchor = FixedAnchor(
        text="An overlong TOC title that is too wide",
        x=72,
        y=30,
        max_width=80.0,
        role="title_x",
    )
    r = lay_out(anchor, measure=_m, font_size=10.0)
    d = diagnose_overflow(r, avail_width=80.0, target="title")
    assert d.reason in (OverflowReason.WIDTH, OverflowReason.UNBREAKABLE_TOKEN)
    assert d.decision in (RecoveryDecision.SHRINK, RecoveryDecision.WRAP)


def test_lay_out_then_diagnose_column_preserve():
    col = FixedColumn(text="456", column_x=540, y=30)
    r = lay_out(col, measure=_m, font_size=10.0)
    d = diagnose_overflow(r, avail_width=20.0)
    assert d.decision is RecoveryDecision.PRESERVE_OVERFLOW


# ---------------------------------------------------------------------------
# malicious-extension: page_x number preserved while a long title adapts
# ---------------------------------------------------------------------------


def test_diagnosis_keeps_page_column_separate():
    """A very long TOC title adapts (WRAP/SHRINK) while the page column stays
    PRESERVE_OVERFLOW — the two channels never merge in the diagnosis."""
    title = LayoutResult(
        text="这是一个非常非常长的章节介绍标题",
        lines=["这是一个非常非常长的章节介绍标题"],
        line_widths=[260.0],
        font_size=10.5,
        primitive_kind="anchor",
        overflow=True,
    )
    page = LayoutResult(
        text="12",
        lines=["12"],
        line_widths=[14.0],
        font_size=10.5,
        primitive_kind="column",
        overflow=False,
    )
    td = diagnose_overflow(
        title, avail_width=160.0, target="title", original_font_size=10.5
    )
    pd = diagnose_overflow(page, avail_width=12.0)
    assert td.decision in (RecoveryDecision.WRAP, RecoveryDecision.SHRINK)
    # page number channel is independent & immovable
    assert pd.decision is RecoveryDecision.PRESERVE_OVERFLOW


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
