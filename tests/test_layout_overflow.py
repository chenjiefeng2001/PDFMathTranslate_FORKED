# -*- coding: utf-8 -*-
"""Commit 7C — overflow policy + LayoutResult engine tests.

Covers ``pdf2zh.semantic.layout.overflow``:

- per-primitive policy mapping (FlowText≠FixedAnchor≠FixedColumn)
- LayoutResult fields + a JSON-safe ``to_dict``
- FlowText short → single line, overflow False
- FlowText long → multiple lines
- PreservedRegion (code) is always PRESERVE and never wrapped
- FixedAnchor SHRINK mechanism is present but not auto-applied
- FixedColumn (TOC page) PRESERVE, column_x untouched
- CLIP never silent (overflow always True)
"""

from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    OverflowPolicy,
    lay_out,
    policy_for,
)
from pdf2zh.semantic.layout.primitives import (
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)

_SIZE = 10.0  # latin 5pt, CJK 10pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80:
            w += size
        else:
            w += size * 0.5
    return w


def _flow(text, max_width=200.0, max_height=400.0):
    return FlowText(text=text, origin=(40.0, 40.0), max_width=max_width, max_height=max_height)


# -- policy mapping --------------------------------------------------------

def test_policy_per_primitive_kind():
    assert policy_for("flow") is OverflowPolicy.WRAP
    assert policy_for("preserved") is OverflowPolicy.PRESERVE
    assert policy_for("column") is OverflowPolicy.PRESERVE
    assert policy_for("anchor") is OverflowPolicy.SHRINK
    assert policy_for("continuation") is OverflowPolicy.WRAP


def test_primitives_are_distinct_policies():
    assert policy_for("flow") is not policy_for("anchor")
    assert policy_for("flow") is not policy_for("column")
    assert policy_for("anchor") is not policy_for("column")


# -- LayoutResult + to_dict ------------------------------------------------

def test_layout_result_to_dict_json_safe():
    import json
    r = LayoutResult(
        text="hello", lines=["hi"], line_widths=[12.0], overflow=True,
        policy=OverflowPolicy.CLIP, font_size=9.5, primitive_kind="anchor",
    )
    d = r.to_dict()
    assert d["policy"] == "clip"
    assert d["overflow"] is True
    json.dumps(d)


# -- FlowText short / long -------------------------------------------------

def test_flow_short_single_line_no_overflow():
    r = lay_out(_flow("Hello"), measure=_measure)
    assert len(r.lines) == 1
    assert r.lines == ["Hello"]
    assert r.overflow is False
    assert r.policy is OverflowPolicy.WRAP


def test_flow_long_wraps_to_multiple_lines():
    r = lay_out(_flow("This is a long paragraph of flow text that must wrap"), measure=_measure)
    assert len(r.lines) >= 2
    assert r.primitive_kind == "flow"
    assert all(w <= 200.0 + 1e-6 for w in r.line_widths)


def test_flow_cjk_wraps():
    # narrow width forces wrapping through the overflow engine
    r = lay_out(
        _flow("中文中文中文中文中文"), measure=_measure, avail_width=24.0, font_size=_SIZE
    )
    assert len(r.lines) >= 2
    assert "".join(r.lines) == "中文中文中文中文中文"


# -- PreservedRegion (code) is always PRESERVE -----------------------------

def test_code_region_never_wrapped():
    text = "def very_long_function_name_with_no_spaces() -> Dict[str, int]:"
    prim = PreservedRegion(text=text, bbox=(20.0, 10.0, 260.0, 26.0))
    r = lay_out(prim, measure=_measure)
    assert r.policy is OverflowPolicy.PRESERVE
    assert r.lines == [text]          # one verbatim line, never split
    assert r.bbox == (20.0, 10.0, 260.0, 26.0)  # geometry immutable
    # if the code is wider than its box we report overflow but do NOT wrap
    assert _measure(text) > 240.0
    assert r.overflow is True


def test_code_short_fits_no_overflow():
    prim = PreservedRegion(text="x = 1", bbox=(20.0, 10.0, 260.0, 26.0))
    r = lay_out(prim, measure=_measure)
    assert r.overflow is False
    assert r.lines == ["x = 1"]


# -- FixedAnchor SHRINK is not auto-applied this commit --------------------

def test_anchor_overflow_flagged_but_not_shrunk_by_default():
    text = "这是一个非常长的中文标题文字内容需要收缩吗"
    prim = FixedAnchor(text=text, x=72.0, y=40.0, max_width=90.0, role="title_x")
    r = lay_out(prim, measure=_measure, font_size=_SIZE)
    assert r.policy is OverflowPolicy.SHRINK
    assert r.lines == [text]          # still one line
    assert r.font_size == _SIZE       # font untouched: no auto-shrink
    assert r.overflow is True         # overflow surfaced for the host to decide


def test_anchor_shrink_mechanism_exercised_when_enabled():
    text = "这是一个非常长的中文标题文字内容需要收缩吗"
    prim = FixedAnchor(text=text, x=72.0, y=40.0, max_width=120.0, role="title_x")
    r = lay_out(prim, measure=_measure, allow_shrink=True, min_font_size=3.0, font_size=_SIZE)
    assert r.policy is OverflowPolicy.SHRINK
    assert r.font_size < _SIZE
    assert r.overflow is False        # shrink fits it


def test_anchor_fits_no_overflow():
    prim = FixedAnchor(text="Section 2", x=72.0, y=30.0, max_width=300.0)
    r = lay_out(prim, measure=_measure)
    assert r.overflow is False
    assert r.lines == ["Section 2"]


# -- FixedColumn (TOC page) ------------------------------------------------

def test_column_preserves_column_x():
    prim = FixedColumn(text="42", column_x=540.0, y=40.0)
    r = lay_out(prim, measure=_measure)
    assert r.policy is OverflowPolicy.PRESERVE
    assert r.lines == ["42"]
    assert r.bbox[0] == 540.0         # page column untouched


# -- CLIP last resort never silent -----------------------------------------

def test_clip_is_never_silent():
    prim = _flow("This definitely does not fit in a tiny box at all at all at all")
    r = lay_out(prim, measure=_measure, policy=OverflowPolicy.CLIP, avail_width=20.0)
    assert r.policy is OverflowPolicy.CLIP
    assert r.overflow is True
    assert len("".join(r.lines)) < len(prim.text)  # truncated


def test_preserve_can_report_overflow_but_never_moves():
    prim = FixedAnchor(text="WWWWWWWW WWWWWWWW WWWW", x=10.0, y=0.0, max_width=20.0)
    r = lay_out(prim, measure=_measure, policy=OverflowPolicy.PRESERVE)
    assert r.lines == ["WWWWWWWW WWWWWWWW WWWW"]
    assert r.bbox[0] == 10.0
    assert r.overflow is True