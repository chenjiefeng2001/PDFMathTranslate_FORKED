# -*- coding: utf-8 -*-
"""Commit 7F-4, extended by 7F-6c-1 — adaptive list layout tests.

List adaptive recovery only affects **content / continuation**; the **marker is
PRESERVE** (never wrap / shrink / clip — it is a raw ``lay_out`` call, never
fed to the adaptive executor).  Even with a pathological
``translation == "A" * 1000`` the geometry anchors are locked:

    marker_x unchanged
    content_x unchanged
    continuation_x == content_x

Multi-line wrap is a *normal* result, not an overflow.  Since 7F-6c-1 the
content / continuation run the **list_content** budget (WRAP → SHRINK → CLIP),
so an overlong unbreakable token now shrinks then clips with explicit overflow
(never silent) — the recovery ladder is bounded (at most WRAP + SHRINK + CLIP).
"""

from pdf2zh.semantic.layout.list_layout import (
    ListLayoutResult,
    layout_list_item,
    layout_list_node,
)
from pdf2zh.semantic.models import ListItemNode, ListNode


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _item(
    text="First item",
    marker="1.",
    marker_x=40.0,
    content_x=52.0,
    content_width=200.0,
    y=700.0,
    continuation=None,
):
    return ListItemNode(
        marker=marker,
        content=text,
        continuation=list(continuation or []),
        marker_x=marker_x,
        marker_width=10.0,
        content_x=content_x,
        content_width=content_width,
        y=y,
    )


def _layout(text, **kw):
    return layout_list_item(
        _item(
            text=text,
            **{
                k: v
                for k, v in kw.items()
                if k
                in (
                    "content_width",
                    "content_x",
                    "marker_x",
                    "y",
                    "marker",
                    "continuation",
                )
            },
        ),
        measure=_measure,
        font_size=10.0,
        content_text=text,
    )


# ---------- marker is preserved + content wraps ---------------------------


def test_marker_preserved_content_wraps():
    text = "This is a long translated list item that definitely wraps over lines"
    layout = layout_list_item(
        _item(content_width=110.0),
        measure=_measure,
        font_size=10.0,
        content_text=text,
    )
    assert isinstance(layout, ListLayoutResult)
    # marker stays a single verbatim FixedAnchor line
    assert layout.marker.lines == ["1."]
    assert layout.marker.primitive_kind == "anchor"
    # content wraps normally; multi-line is normal (not overflow)
    assert len(layout.content.lines) >= 2
    assert layout.content.overflow is False


def test_long_english_wraps_adaptive():
    layout = layout_list_item(
        _item(content_width=100.0),
        measure=_measure,
        font_size=10.0,
        content_text="This is a very long translated list item that cannot fit on one line",
    )
    assert len(layout.content.lines) >= 2
    assert all(w <= 100.0 + 1e-6 for w in layout.content.line_widths)
    assert " ".join(layout.content.lines) == layout.content.text


def test_cjk_content_wraps():
    layout = layout_list_item(
        _item(content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text="这是一个非常长的列表项目后续内容需要换行显示",
    )
    assert len(layout.content.lines) >= 2
    assert "".join(layout.content.lines) == layout.content.text


def test_mixed_cjk_english_wraps():
    text = "这是中文 content 与 English 混合的列表项需要换行"
    layout = layout_list_item(
        _item(content_width=90.0),
        measure=_measure,
        font_size=10.0,
        content_text=text,
    )
    assert len(layout.content.lines) >= 2
    assert "".join(layout.content.lines).replace(" ", "") == text.replace(" ", "")


# ---------- continuation pinned to content_x -------------------------------


def test_continuation_preserved_and_pinned():
    layout = layout_list_item(
        _item(content_x=52.0, content_width=200.0),
        measure=_measure,
        font_size=10.0,
        content_text="First item",
        continuation_texts=["continuation one", "continuation two"],
    )
    assert len(layout.continuation) == 2
    for cl in layout.continuation:
        assert cl.bbox[0] == 52.0
        assert cl.primitive_kind == "flow"


def test_continuation_wraps_and_keeps_x():
    layout = layout_list_item(
        _item(content_x=52.0, content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text="First",
        continuation_texts=["a continuation line that itself wraps over more lines"],
    )
    assert len(layout.continuation[0].lines) >= 2
    assert layout.continuation[0].bbox[0] == 52.0


# ---------- geometry anchors unchanged under adversarial translation --------


def test_100x_translation_preserves_anchors():
    layout = layout_list_item(
        _item(marker_x=40.0, content_x=52.0, content_width=200.0, y=700.0),
        measure=_measure,
        font_size=10.0,
        content_text="A" * 1000,
    )
    # marker untouched
    assert layout.marker.lines == ["1."]
    assert layout.marker_x == 40.0
    assert layout.content_x == 52.0
    assert layout.continuation_x == 52.0
    assert layout.y == 700.0


def test_continuation_100x_preserves_anchors():
    layout = layout_list_item(
        _item(content_x=52.0, content_width=200.0, y=700.0),
        measure=_measure,
        font_size=10.0,
        content_text="item",
        continuation_texts=["B" * 1000],
    )
    assert layout.content_x == 52.0
    assert layout.continuation_x == 52.0
    assert layout.continuation[0].bbox[0] == 52.0


# ---------- nested geometry unchanged ---------------------------------------


def test_nested_geometry_unchanged():
    l2 = ListNode(level=2)
    l2.items.append(
        _item(text="deep", marker="i.", marker_x=64.0, content_x=76.0, y=660.0)
    )
    l1 = ListNode(level=1)
    it1 = _item(text="Background", marker="a.", marker_x=52.0, content_x=64.0, y=680.0)
    it1.children.append(l2)
    l1.items.append(it1)
    l0 = ListNode(level=0)
    it0 = _item(text="Intro", marker="1.", marker_x=40.0, content_x=52.0, y=700.0)
    it0.children.append(l1)
    l0.items.append(it0)

    results = layout_list_node(l0, measure=_measure, font_size=10.0)
    assert [r.marker.lines[0] for r in results] == ["1.", "a.", "i."]
    xs = [r.content_x for r in results]
    assert xs[2] > xs[1] > xs[0]
    # markers stay fixed at their original columns
    assert [r.marker_x for r in results] == [40.0, 52.0, 64.0]


# ---------- no recovery loop / overflow diagnostics ------------------------


def test_overlong_unbreakable_token_no_loop():
    """7F-6c-1: an overlong unbreakable token runs the bounded ladder
    (SHRINK → CLIP) — never a while-loop, overflow stays explicit, and the
    geometry anchors are untouched."""
    token = "Supercalifragilisticexpialidocious"
    layout = layout_list_item(
        _item(content_width=40.0),
        measure=_measure,
        font_size=10.0,
        content_text=token,
    )
    assert layout.content.overflow is True
    assert len(layout.content.recovery_steps) <= 3
    assert layout.content.recovery_decision in ("clip", "preserve_overflow")
    assert len("".join(layout.content.lines)) < len(token)  # never silent
    # recovery must never touch the anchors
    assert layout.marker_x == 40.0
    assert layout.content_x == 52.0
    assert layout.continuation_x == 52.0


def test_list_layout_recovery_json_safe():
    import json

    layout = layout_list_item(
        _item(content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text="很长很长的内容需要换行显示",
        continuation_texts=["又一延续行"],
    )
    json.dumps(layout.to_dict())


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__]))
