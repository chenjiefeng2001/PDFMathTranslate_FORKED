"""Commit 7E-2a/b — list layout contract + wrapping tests.

Covers ``pdf2zh.semantic.layout.list_layout``:

- ``layout_list_item`` geometry passthrough (marker_x / content_x /
  continuation_x / y verbatim from the node, never recomputed);
- marker is a FixedAnchor: single verbatim line, never wrapped, never
  translated (the adapter takes pre-translated text, never a translator);
- content wraps inside ``content_width``: short → 1 line, long English,
  CJK, mixed CJK/English, embedded newlines; an overlong unbreakable token
  runs the bounded 7F-6c ladder (SHRINK → CLIP, overflow explicit, never
  silent);
- continuation stays pinned to ``content_x`` (``continuation_x ==
  content_x``) and each line may wrap;
- failure degrades to an overflow-flagged single line, never raises;
- architecture: the adapter only calls ``lay_out`` — never
  ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text`` directly.
"""

import inspect
import json

from pdf2zh.semantic.layout.list_layout import (
    ListLayoutResult,
    layout_list_item,
    layout_list_node,
)
from pdf2zh.semantic.layout.overflow import OverflowPolicy
from pdf2zh.semantic.models import ListItemNode, ListNode

_SIZE = 10.0  # latin 5pt, CJK 10pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80:
            w += size
        else:
            w += size * 0.5
    return w


def _item(
    text="First item",
    marker="1.",
    marker_x=40.0,
    content_x=52.0,
    content_width=200.0,
    marker_width=10.0,
    y=700.0,
    continuation=None,
):
    """直接构造 ListItemNode（几何原样），不依赖 detector/parser。"""
    return ListItemNode(
        marker=marker,
        content=text,
        continuation=list(continuation or []),
        marker_x=marker_x,
        marker_width=marker_width,
        content_x=content_x,
        content_width=content_width,
        y=y,
    )


# ── 1. geometry passthrough ──────────────────────────────────────────────


def test_geometry_passthrough_verbatim():
    layout = layout_list_item(
        _item(marker_x=40.0, content_x=52.0, content_width=200.0, y=700.0),
        measure=_measure,
        font_size=10.0,
        line_step=-14.0,
    )
    assert layout.marker_x == 40.0
    assert layout.content_x == 52.0
    assert layout.continuation_x == 52.0  # == content_x, never re-derived
    assert layout.y == 700.0
    assert layout.line_step == -14.0


def test_marker_is_fixed_anchor_single_line():
    layout = layout_list_item(_item(), measure=_measure, font_size=10.0)
    assert layout.marker.lines == ["1."]
    assert layout.marker.policy is OverflowPolicy.SHRINK
    assert layout.marker.primitive_kind == "anchor"
    # marker 永不 wrap —— 即使超宽也不拆行（溢出上报，不静默）
    layout_wide = layout_list_item(
        _item(
            marker="MMMMMMMM.",
            text="wide",
            marker_x=0.0,
            content_x=0.0,
            content_width=0.0,
            marker_width=10.0,
        ),
        measure=_measure,
        font_size=10.0,
    )
    assert len(layout_wide.marker.lines) == 1
    assert layout_wide.marker.overflow is True


def test_short_content_single_line_no_overflow():
    layout = layout_list_item(
        _item(content_width=200.0),
        measure=_measure,
        font_size=10.0,
        content_text="First item",
    )
    assert layout.content.lines == ["First item"]
    assert layout.content.overflow is False
    assert layout.content.policy is OverflowPolicy.WRAP


def test_long_english_wraps_within_content_width():
    text = "This is a very long translated list item that cannot fit on one line at all"
    layout = layout_list_item(
        _item(content_width=120.0),
        measure=_measure,
        font_size=10.0,
        content_text=text,
    )
    assert len(layout.content.lines) >= 2
    assert all(w <= 120.0 + 1e-6 for w in layout.content.line_widths)
    assert " ".join(layout.content.lines) == text  # wrap breaks at word edges


def test_cjk_wraps():
    text = "这是一个非常长的列表项目后续内容需要换行显示"
    layout = layout_list_item(
        _item(content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text=text,
    )
    assert len(layout.content.lines) >= 2
    assert "".join(layout.content.lines) == text


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


def test_newline_in_translated_content_is_hard_break():
    layout = layout_list_item(
        _item(content_width=500.0),
        measure=_measure,
        font_size=10.0,
        content_text="first translated line\nsecond translated line",
    )
    assert layout.content.lines == ["first translated line", "second translated line"]


def test_overlong_token_bounded_recovery_not_silent():
    """7F-6c-1: an overlong unbreakable token runs the bounded ladder and
    clips with explicit overflow — never silent, never an infinite loop."""
    token = "Supercalifragilisticexpialidocious"
    layout = layout_list_item(
        _item(content_width=40.0),
        measure=_measure,
        font_size=10.0,
        content_text=token,
    )
    assert layout.content.overflow is True  # 溢出显式上报
    assert len(layout.content.recovery_steps) <= 3
    assert layout.content.recovery_decision in ("clip", "preserve_overflow")
    assert len("".join(layout.content.lines)) < len(token)  # truncated, not silent


def test_no_width_means_no_wrap():
    layout = layout_list_item(
        _item(content_width=0.0),
        measure=_measure,
        font_size=10.0,
        content_text="single long line that never wraps because width is zero",
    )
    assert len(layout.content.lines) == 1


# ── 2. continuation pinned to content_x ──────────────────────────────────


def test_continuation_x_equals_content_x():
    layout = layout_list_item(
        _item(content_x=52.0, content_width=200.0),
        measure=_measure,
        font_size=10.0,
        content_text="First item",
        continuation_texts=["continuation one", "continuation two"],
    )
    assert len(layout.continuation) == 2
    for cl in layout.continuation:
        # 延续行锚定 content_x（bbox 携带原始列位置）
        assert cl.bbox[0] == 52.0
        assert cl.primitive_kind == "flow"


def test_continuation_wraps_independently():
    layout = layout_list_item(
        _item(content_width=60.0),
        measure=_measure,
        font_size=10.0,
        content_text="First",
        continuation_texts=["a continuation line that itself wraps over more lines"],
    )
    assert len(layout.continuation[0].lines) >= 2


def test_translated_text_used_when_provided():
    layout = layout_list_item(
        _item(content_width=500.0),
        measure=_measure,
        font_size=10.0,
        content_text="译后内容",
        continuation_texts=["译后延续行"],
    )
    assert layout.content.lines == ["译后内容"]
    assert layout.continuation[0].lines == ["译后延续行"]
    # 适配器不持翻译器：原文仅作缺省
    layout2 = layout_list_item(
        _item(text="First item"), measure=_measure, font_size=10.0
    )
    assert layout2.content.lines == ["First item"]


# ── 3. JSON-safety + failure degrade ─────────────────────────────────────


def test_layout_result_json_safe():
    layout = layout_list_item(
        _item(content_width=80.0),
        measure=_measure,
        font_size=10.0,
        content_text="long enough to wrap over several lines",
        continuation_texts=["cont line"],
    )
    json.dumps(layout.to_dict())
    d = layout.to_dict()
    assert d["continuation_x"] == d["content_x"]


def test_measure_failure_degrades_never_raises():
    """测量失败被 lay_out 兜底（0 宽）—— 绝不抛出、不静默崩溃。"""

    def bad_measure(s, size):
        raise RuntimeError("boom")

    layout = layout_list_item(
        _item(content_width=100.0),
        measure=bad_measure,
        font_size=10.0,
        content_text="anything",
    )
    assert layout.content.lines == ["anything"]


# ── 4. layout_list_node：整树 → 扁平逐 item 结果 ────────────────────────


def _nested_tree():
    """1 → a → i 三层嵌套树（几何来自构造，非 level 计算）。"""
    l2 = ListNode(level=2)
    l2.items.append(
        _item(text="deep", marker="i.", marker_x=64.0, content_x=76.0, y=660.0)
    )
    l1 = ListNode(level=1)
    l1.items.append(
        _item(
            text="Background",
            marker="a.",
            marker_x=52.0,
            content_x=64.0,
            y=680.0,
            continuation=None,
        )
    )
    l1.items[0].children.append(l2)
    l0 = ListNode(level=0)
    l0.items.append(
        _item(text="Intro", marker="1.", marker_x=40.0, content_x=52.0, y=700.0)
    )
    l0.items[0].children.append(l1)
    return l0


def test_layout_list_node_walks_nested_tree():
    results = layout_list_node(_nested_tree(), measure=_measure, font_size=10.0)
    assert len(results) == 3
    assert [r.marker.lines[0] for r in results] == ["1.", "a.", "i."]
    # 逐级 content_x 递增（来自节点原始几何，不是 level 计算）
    xs = [r.content_x for r in results]
    assert xs[2] > xs[1] > xs[0]


# ── 5. architecture：只走 lay_out，不直接调 wrap/shrink/clip ──────────


def test_list_layout_never_calls_wrap_shrink_clip_directly():
    import pdf2zh.semantic.layout.list_layout as mod

    src = inspect.getsource(mod)
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src
    assert "lay_out(" in src


def test_list_layout_no_geometry_reinference():
    import pdf2zh.semantic.layout.list_layout as mod

    src = inspect.getsource(mod)
    for banned in ("level *", "* level", "index *", "marker_width *"):
        assert banned not in src
    assert isinstance(ListLayoutResult, type)
