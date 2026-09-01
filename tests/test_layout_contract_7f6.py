# -*- coding: utf-8 -*-
"""Commit 7F-6a — unified LayoutResult contract (architecture gate).

Proves that all four layout paths (Flow / List / TOC / Code) expose the
**same output contract** — :class:`LayoutResultLike` (``primitive_kind`` /
``lines`` / ``line_widths`` / ``bbox`` / ``overflow`` / ``font_size`` /
``recovery`` / ``to_dict()``) — without unifying their implementations and
without changing any rendering behavior, geometry, recovery policy, or
existing output:

    Flow  -> lay_out(FlowText)          -> LayoutResult     (atomic run)
    Code  -> lay_out(PreservedRegion)   -> LayoutResult     (PRESERVE, never adaptive)
    List  -> layout_list_item           -> ListLayoutResult (→ as_layout_result view)
    TOC   -> layout_toc_entry           -> TocEntryLayoutResult (→ as_layout_result view)

Locked guarantees (7F-6a):

1. **Common contract** — every path's result satisfies ``LayoutResultLike``
   (atomically, or through :func:`as_layout_result` for List / TOC).
2. **``recovery`` is uniform** — ``None`` when nothing ran; a JSON-safe dict
   with ``steps`` when recovery executed; ``to_dict()`` output is unchanged.
3. **Geometry ownership stays in semantic nodes** — reading the contract view
   never mutates the semantic node's anchors (``marker_x`` / ``content_x`` /
   ``y`` / ``level``).
4. **Contract source purity** — ``contract.py`` re-derives nothing from
   ``level`` / ``index``, never detects / parses / translates / draws, and
   never executes wrap/shrink/clip itself (it is a read-only view).
"""

import ast
import inspect
from pathlib import Path

from pdf2zh.semantic.layout.contract import LayoutResultLike, as_layout_result
from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    OverflowPolicy,
    lay_out,
)
from pdf2zh.semantic.layout.primitives import FlowText, PreservedRegion

_HERE = Path(__file__).resolve().parent
_CONTRACT_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "contract.py"


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _check_contract(view, expected_kind):
    """Assert the full LayoutResultLike contract shape on any view."""
    assert isinstance(view, LayoutResultLike)
    assert view.primitive_kind == expected_kind
    assert isinstance(view.lines, list) and all(isinstance(l, str) for l in view.lines)
    assert isinstance(view.line_widths, list)
    assert len(view.line_widths) == len(view.lines)
    assert isinstance(view.bbox, tuple) and len(view.bbox) == 4
    assert isinstance(view.overflow, bool)
    assert isinstance(view.font_size, float)
    assert view.recovery is None or isinstance(view.recovery, dict)
    d = view.to_dict()
    assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# 1. All four layout paths expose the common contract
# ---------------------------------------------------------------------------


def test_flow_path_exposes_common_contract():
    prim = FlowText(
        text="translated paragraph text",
        origin=(72.0, 700.0),
        max_width=300.0,
        max_height=60.0,
    )
    result = lay_out(prim, measure=_measure, font_size=11.0)
    assert isinstance(result, LayoutResult)
    # atomic -> the contract is satisfied directly, adapter returns it as-is
    view = as_layout_result(result)
    assert view is result
    _check_contract(view, "flow")
    assert view.lines  # at least one settled line
    assert view.font_size == 11.0


def test_code_path_exposes_common_contract():
    text = "def very_long_function_name_that_nobody_should_break():"
    prim = PreservedRegion(text=text, bbox=(10.0, 10.0, 300.0, 26.0))
    result = lay_out(prim, measure=_measure, font_size=10.0)
    assert isinstance(result, LayoutResult)
    _check_contract(as_layout_result(result), "preserved")
    # code is PRESERVE: verbatim single line, never adaptive
    assert result.policy is OverflowPolicy.PRESERVE
    assert result.lines == [text]


def test_list_path_exposes_common_contract_through_adapter():
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.models import ListItemNode

    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=200.0,
        y=700.0,
        level=0,
        content="original content",
    )
    agg = layout_list_item(it, font_size=11.0, content_text="translated content")
    view = as_layout_result(agg)
    _check_contract(view, "list")
    assert view.font_size == 11.0
    assert "1." in view.lines  # marker channel is part of the view
    assert "translated content" in view.lines


def test_toc_path_exposes_common_contract_through_adapter():
    from pdf2zh.semantic.layout.toc_layout import layout_toc_entry

    entry = {
        "title_x": 72.0,
        "page_x": 500.0,
        "level": 1,
        "bbox": (72.0, 700.0, 500.0, 714.0),
        "number": "2.3",
        "page_number": "42",
        "leader_present": True,
    }
    agg = layout_toc_entry(
        entry, size=10.0, y=700.0, translated_title="translated title"
    )
    view = as_layout_result(agg)
    _check_contract(view, "toc")
    assert view.font_size == 10.0  # toc uses ``size``; view maps it to font_size
    assert "42" in view.lines  # page column run is in the view
    assert view.overflow is False


def test_all_four_paths_have_distinct_primitive_kinds():
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.layout.toc_layout import layout_toc_entry
    from pdf2zh.semantic.models import ListItemNode

    flow = as_layout_result(
        lay_out(FlowText(text="t", origin=(0, 0), max_width=100), measure=_measure)
    ).primitive_kind
    code = as_layout_result(
        lay_out(PreservedRegion(text="x", bbox=(0, 0, 10, 10)), measure=_measure)
    ).primitive_kind
    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=200.0,
        y=700.0,
        content="c",
    )
    lst = as_layout_result(layout_list_item(it, font_size=11.0)).primitive_kind
    toc = as_layout_result(
        layout_toc_entry(
            {"title_x": 72.0, "page_x": 500.0, "level": 1},
            size=10.0,
            y=700.0,
            translated_title="t",
        )
    ).primitive_kind
    assert {flow, code, lst, toc} == {"flow", "preserved", "list", "toc"}


# ---------------------------------------------------------------------------
# 2. ``recovery`` is a uniform member; ``to_dict()`` output unchanged
# ---------------------------------------------------------------------------


def test_recovery_member_is_uniform_across_paths():
    from pdf2zh.semantic.layout.adaptive import adaptive_layout

    # overflowed flow run -> recovery is a JSON-safe dict with steps
    r = adaptive_layout(
        FlowText(text="X" * 200, origin=(0.0, 0.0), max_width=20.0, max_height=10.0),
        measure=_measure,
        avail_width=20.0,
        avail_height=10.0,
        font_size=11.0,
    )
    assert r.overflow
    assert isinstance(r.recovery, dict)
    assert "steps" in r.recovery and r.recovery["steps"]
    assert "decision" in r.recovery and r.recovery["decision"]

    # clean flow run -> None (NO_ACTION), and to_dict hides the key
    r2 = adaptive_layout(
        FlowText(text="hi", origin=(0.0, 0.0), max_width=200.0),
        measure=_measure,
        avail_width=200.0,
        font_size=11.0,
    )
    assert not r2.overflow
    assert r2.recovery is None
    assert "recovery" not in r2.to_dict()

    # list / toc views surface the aggregate recovery (or None) too
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.models import ListItemNode

    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=200.0,
        y=700.0,
        content="c",
    )
    list_view = as_layout_result(layout_list_item(it, font_size=11.0))
    assert list_view.recovery is None or isinstance(list_view.recovery, dict)


def test_layout_result_to_dict_output_unchanged():
    """7F-6a refactor must not change the JSON shape a renderer sees."""
    r = LayoutResult(
        text="t",
        lines=["t"],
        line_widths=[10.0],
        bbox=(1.0, 2.0, 3.0, 4.0),
        overflow=True,
        policy=OverflowPolicy.CLIP,
        font_size=9.5,
        primitive_kind="flow",
        recovery_reason="width",
        recovery_decision="clip",
        recovery_steps=["WRAP", "SHRINK", "CLIP"],
        original_font_size=11.0,
    )
    d = r.to_dict()
    assert d["recovery"] == {
        "reason": "width",
        "decision": "clip",
        "steps": ["WRAP", "SHRINK", "CLIP"],
        "original_font_size": 11.0,
        "final_font_size": 9.5,
    }
    assert d["policy"] == "clip" and d["font_size"] == 9.5
    assert d["primitive_kind"] == "flow" and d["overflow"] is True

    r2 = LayoutResult(text="x", lines=["x"], line_widths=[5.0], bbox=(0, 0, 0, 0))
    d2 = r2.to_dict()
    assert "recovery" not in d2  # clean result: key hidden, as before 7F-6a


# ---------------------------------------------------------------------------
# 3. Geometry ownership stays in semantic nodes
# ---------------------------------------------------------------------------


def test_geometry_ownership_stays_in_semantic_nodes():
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.models import ListItemNode

    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=200.0,
        y=700.0,
        level=0,
        content="original",
    )
    before = (it.marker_x, it.content_x, it.content_width, it.y, it.level)
    agg = layout_list_item(it, font_size=11.0, content_text="translated 内容")
    view = as_layout_result(agg)
    # reading the contract view must not mutate the semantic node's anchors
    _ = view.lines, view.line_widths, view.bbox, view.overflow, view.to_dict()
    assert (it.marker_x, it.content_x, it.content_width, it.y, it.level) == before
    assert agg.marker_x == 40.0 and agg.content_x == 60.0 and agg.y == 700.0


def test_as_layout_result_rejects_non_results():
    import pytest

    with pytest.raises(TypeError):
        as_layout_result("not a layout result")


# ---------------------------------------------------------------------------
# 4. contract.py source purity — read-only view, no geometry math
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    """Executable code with docstrings stripped (prose must not trip guards)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _clean(body):
        return [
            n
            for n in body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _ast_binops(source: str):
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        op = type(node.op).__name__

        def _name(n):
            if isinstance(n, ast.Name):
                return n.id
            if isinstance(n, ast.Attribute):
                return n.attr
            return None

        out.append((op, _name(node.left), _name(node.right)))
    return out


def test_contract_source_never_derives_geometry():
    src = _code(_CONTRACT_PATH)
    for op, l, r in _ast_binops(src):
        if {"level", "index"} & {l, r}:
            raise AssertionError(f"contract.py 用 {op}({l},{r}) 重建几何")


def test_contract_source_never_detects_parses_translates_or_draws():
    src = _code(_CONTRACT_PATH)
    for banned in (
        "looks_like",
        "detect_",
        "parse_",
        "translate",
        "renderer",
        "magicpdf",
    ):
        assert banned not in src, f"contract.py 不得引用 {banned}"


def test_contract_source_is_decision_readonly_no_execution():
    src = _code(_CONTRACT_PATH)
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src, f"contract.py 不得直接执行 {banned}"
    # it only *views* settled results
    assert "as_layout_result" in src


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
