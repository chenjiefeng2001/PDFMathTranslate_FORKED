# -*- coding: utf-8 -*-
"""Commit 7B — geometry layout primitives & payload mapping tests.

Covers ``pdf2zh.semantic.layout.primitives`` and
``pdf2zh.semantic.layout.mapping``:

- FixedAnchor / FixedColumn / PreservedRegion preserve original geometry.
- FlowText exposes max width / height / line height.
- Continuation preserves its parent anchor.
- Existing payloads map cleanly onto primitives (Paragraph→FlowText,
  List→FixedAnchor/Continuation, TOC title→FixedAnchor, TOC page→FixedColumn,
  Code→PreservedRegion).
- Geometry is never derived from ``level`` / entry ``index``.
- Existing List / Code behavior keeps its original-geometry contract.
"""

from pdf2zh.semantic.layout.constraints import FixedWidth, MaxWidth
from pdf2zh.semantic.layout.mapping import (
    flow_text,
    list_anchor,
    list_continuation,
    preserved_region,
    toc_page_column,
    toc_title_anchor,
)
from pdf2zh.semantic.layout.primitives import (
    Continuation,
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)


# -- 1. FixedAnchor preserves x -------------------------------------------

def test_fixed_anchor_preserves_x():
    a = FixedAnchor(text="content", x=123.4, y=50.0)
    assert a.x == 123.4
    assert a.y == 50.0
    assert a.kind == "anchor"


def test_list_anchor_content_x_passthrough():
    a = list_anchor(text="item", x=96.5, y=40.0)
    assert isinstance(a, FixedAnchor)
    assert a.x == 96.5  # copied verbatim, never recomputed
    assert a.role == "content_x"


def test_toc_title_anchor_title_x_passthrough():
    a = toc_title_anchor(text="标题", x=108.5, y=30.0)
    assert isinstance(a, FixedAnchor)
    assert a.x == 108.5  # title_x from node
    assert a.role == "title_x"


# -- 2. FixedColumn preserves x -------------------------------------------

def test_fixed_column_preserves_x():
    col = FixedColumn(text="42", column_x=540.5, y=30.0)
    assert col.column_x == 540.5
    assert col.x == 540.5


def test_toc_page_column_keeps_original_page_x():
    col = toc_page_column(text="15", column_x=540.0, y=45.0)
    assert isinstance(col, FixedColumn)
    assert col.column_x == 540.0
    # translation length change must not move the column — x is the original.
    assert col.x == 540.0


# -- 3. PreservedRegion preserves bbox ------------------------------------

def test_preserved_region_preserves_bbox():
    r = PreservedRegion(text="x = a + b", bbox=(30.0, 40.0, 220.0, 66.0))
    assert r.bbox == (30.0, 40.0, 220.0, 66.0)
    assert r.width == 190.0
    assert r.height == 26.0
    assert r.origin == (30.0, 40.0)


def test_mapped_preserved_region():
    r = preserved_region("def f():", bbox=(20.0, 10.0, 180.0, 26.0))
    assert isinstance(r, PreservedRegion)
    assert r.bbox == (20.0, 10.0, 180.0, 26.0)


# -- 4. FlowText exposes max width ----------------------------------------

def test_flow_text_exposes_max_width_height():
    f = FlowText(
        text="A paragraph", origin=(50.0, 60.0), max_width=400.0,
        max_height=120.0, line_height=14.0,
    )
    assert f.max_width == 400.0
    assert f.max_height == 120.0
    assert f.line_height == 14.0
    assert f.origin == (50.0, 60.0)
    assert f.x == 50.0 and f.y == 60.0


def test_flow_text_mapping():
    f = flow_text("hello", origin=(40.0, 30.0), max_width=300.0, max_height=90.0)
    assert isinstance(f, FlowText)
    assert f.max_width == 300.0
    assert f.max_height == 90.0


# -- 5. Continuation preserves parent anchor ------------------------------

def test_continuation_preserves_parent_anchor():
    parent = FixedAnchor(text="first", x=96.0, y=40.0, role="content_x")
    cont = list_continuation(text="wrapped", x=96.0, y=55.0, parent=parent)
    assert isinstance(cont, Continuation)
    assert cont.parent_anchor is parent
    assert cont.continuation_x == 96.0
    assert cont.continuation_y == 55.0


def test_continuation_default_parent_none():
    cont = Continuation(text="next line", continuation_x=72.0, continuation_y=30.0)
    assert cont.parent_anchor is None


# -- 6/8. Payload mapping (Paragraph / List / TOC / Code) ------------------

def test_mapping_paragraph_to_flow():
    f = flow_text("normal text", origin=(70.0, 80.0))
    assert isinstance(f, FlowText)


def test_mapping_list_to_anchor_and_continuation():
    parent = list_anchor("A. alpha", x=90.0, y=40.0)
    cont = list_continuation("continuation line", x=90.0, y=56.0, parent=parent)
    assert isinstance(parent, FixedAnchor)
    assert isinstance(cont, Continuation)
    assert cont.parent_anchor is parent


def test_mapping_toc_title_and_page():
    title = toc_title_anchor("Introduction", x=72.0, y=50.0)
    page = toc_page_column("3", column_x=540.0, y=50.0)
    assert isinstance(title, FixedAnchor)
    assert isinstance(page, FixedColumn)
    assert title.x != page.x


def test_mapping_code_to_preserved():
    r = preserved_region("int main() {", bbox=(20.0, 20.0, 200.0, 36.0))
    assert isinstance(r, PreservedRegion)
    # code geometry is immutable: width/height are the original bbox's.
    assert r.width == 180.0


# -- 9/10. Geometry never derived from index / level -----------------------

def test_geometry_not_derived_from_index():
    """Two FixedColumns with different column_x must keep their own values (no
    ``index * width`` propagation) — the primitive holds whatever the parser
    measured for each entry."""
    cols = [
        FixedColumn(text="3", column_x=500.0),
        FixedColumn(text="15", column_x=540.0),
        FixedColumn(text="28", column_x=580.0),
    ]
    xs = [c.column_x for c in cols]
    assert xs == [500.0, 540.0, 580.0]  # distinct per entry, not equalised


def test_geometry_not_derived_from_level():
    """Three nested TOC anchors with different title_x keep each own value —
    the primitive never applies ``level * constant``."""
    anchors = [
        FixedAnchor(text="a", x=72.0, role="title_x"),
        FixedAnchor(text="b", x=108.0, role="title_x"),
        FixedAnchor(text="c", x=138.0, role="title_x"),
    ]
    assert [a.x for a in anchors] == [72.0, 108.0, 138.0]


# -- 12/13/14. List / Code / Style behavior unchanged ----------------------

def test_list_renderer_contract_unchanged():
    """ListRenderer still emits marker PRESERVE + content at original content_x
    and, when mapping to primitives, content pins that same x."""
    from pdf2zh.semantic.models import ListNode, ListItemNode
    from pdf2zh.semantic.renderer.list import ListRenderer

    tree = ListNode(
        level=0,
        items=[
            ListItemNode(
                marker="1.",
                content="First item",
                marker_x=72.0,
                content_x=96.0,
                level=0,
                indent=72.0,
            )
        ],
    )
    calls = []
    cmds = ListRenderer().render(tree, translate=lambda s: calls.append(s) or f"译_{s}")
    assert calls == ["First item"]  # marker never enters translator
    anchors = [list_anchor(c.text, c.x, c.y) for c in cmds if c.kind == "text"]
    assert anchors and anchors[0].x == 96.0  # content_x preserved


def test_code_preserve_contract_unchanged():
    """Code maps to PreservedRegion and its bbox is untouched (PRESERVE)."""
    r = preserved_region("for i in range(10):", bbox=(10.0, 10.0, 220.0, 26.0))
    assert isinstance(r, PreservedRegion)
    assert r.text == "for i in range(10):"
    assert r.bbox == (10.0, 10.0, 220.0, 26.0)


def test_style_contract_unchanged():
    """The layout layer carries geometry only; style markers are translated by
    the existing style pipeline untouched (no regression here)."""
    from pdf2zh.semantic.models import SpanStyle
    from pdf2zh.semantic.style_detector import extract_style_markers, inject_style_markers
    from pdf2zh.semantic.style_translate import translate_styled_paragraph

    bold = [SpanStyle(bold=True)] * 3
    marked = inject_style_markers("abc", bold)
    clean, _ = extract_style_markers(marked)
    assert clean == "abc"  # markers strip back to plain source
    # styled translation still works via the existing entry point
    t = translate_styled_paragraph("hello", None, lambda s: f"译_{s}")
    assert t.text == "译_hello"


# -- Constraints interact with primitives (integration) --------------------

def test_flow_geometry_combined_with_constraint():
    f = FlowText(text="x", origin=(10.0, 20.0), max_width=500.0, max_height=100.0)
    assert f.max_width == 500.0
    assert f.origin[1] == 20.0
    from pdf2zh.semantic.layout.constraints import LayoutGeometry, resolve_geometry

    g = resolve_geometry(LayoutGeometry(f.x, f.y, 0.0, 0.0), (FixedWidth(300.0),))
    assert g.width == 300.0
    # width never constrained by MaxWidth below the primitive's own max_width
    gm = resolve_geometry(
        LayoutGeometry(0.0, 0.0, f.max_width, f.max_height), (MaxWidth(200.0),)
    )
    assert gm.width == 200.0