"""Commit 7E-2b — nested list layout geometry tests.

Checks that per-level geometry survives layout untouched and strictly
increases down the nesting:

- ``marker_x`` / ``content_x`` per level strictly increasing (real columns,
  not level arithmetic);
- ``continuation_x == content_x`` at every level;
- geometry is **verbatim passthrough**: a node whose parsed geometry is
  unusual is laid out exactly where the parser put it (layout never
  re-derives or "fixes" positions from level / index / numbering);
- wrapping inside a nested item keeps its own content column.
"""

from pdf2zh.semantic.layout.list_layout import layout_list_item, layout_list_node
from pdf2zh.semantic.models import ListItemNode, ListNode

_SIZE = 10.0


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80:
            w += size
        else:
            w += size * 0.5
    return w


def _item(text, marker, marker_x, content_x, y, continuation=None, marker_width=10.0):
    return ListItemNode(
        marker=marker,
        content=text,
        continuation=list(continuation or []),
        marker_x=marker_x,
        marker_width=marker_width,
        content_x=content_x,
        content_width=200.0,
        y=y,
    )


def _tree():
    """1 → a → i 三层嵌套（几何逐级右移 + 下移）。"""
    l2 = ListNode(level=2)
    l2.items.append(_item("deep", "i.", 64.0, 76.0, 660.0))
    l1 = ListNode(level=1)
    b = _item("Background", "a.", 52.0, 64.0, 680.0)
    b.children.append(l2)
    l1.items.append(b)
    l0 = ListNode(level=0)
    intro = _item("Intro", "1.", 40.0, 52.0, 700.0)
    intro.children.append(l1)
    l0.items.append(intro)
    return l0


def test_per_level_marker_x_strictly_increasing():
    results = layout_list_node(_tree(), measure=_measure, font_size=10.0)
    mx = [r.marker_x for r in results]
    assert mx == [40.0, 52.0, 64.0]
    assert mx[1] > mx[0] and mx[2] > mx[1]


def test_per_level_content_x_strictly_increasing():
    results = layout_list_node(_tree(), measure=_measure, font_size=10.0)
    cx = [r.content_x for r in results]
    assert cx == [52.0, 64.0, 76.0]
    assert cx[1] > cx[0] and cx[2] > cx[1]


def test_continuation_x_equals_content_x_at_every_level():
    for level, (marker_x, content_x, y) in enumerate(
        ((40.0, 52.0, 700.0), (52.0, 64.0, 680.0), (64.0, 76.0, 660.0))
    ):
        item = _item(
            f"level {level}", f"{marker_x}.",
            marker_x, content_x, y,
            continuation=[f"cont of level {level}"],
        )
        r = layout_list_item(
            item, measure=_measure, font_size=10.0,
            continuation_texts=[f"cont of level {level}"],
        )
        assert r.continuation_x == content_x
        assert r.continuation[0].bbox[0] == content_x


def test_geometry_never_recomputed_or_fixed():
    """布局层绝不“修正”解析几何：怪异列也原样透传。"""
    item = _item("Odd", "x.", marker_x=123.0, content_x=200.0, y=500.0)
    r = layout_list_item(item, measure=_measure, font_size=10.0)
    assert r.marker_x == 123.0
    assert r.content_x == 200.0
    assert r.continuation_x == 200.0
    assert r.y == 500.0


def test_nested_wrap_keeps_own_content_column():
    item = _item(
        "a fairly long nested item that must wrap over lines",
        "a.", marker_x=52.0, content_x=64.0, y=680.0,
    )
    r = layout_list_item(
        item, measure=_measure, font_size=10.0,
        content_text="a fairly long nested item that must wrap over lines",
    )
    assert len(r.content.lines) >= 2
    # bbox 锚定该层 content_x —— 换行不改变列
    assert r.content.bbox[0] == 64.0


def test_nested_baselines_step_down_in_y_up():
    results = layout_list_node(_tree(), measure=_measure, font_size=10.0, line_step=-14.0)
    ys = [r.y for r in results]
    assert ys == [700.0, 680.0, 660.0]
    assert ys[1] < ys[0] and ys[2] < ys[1]
