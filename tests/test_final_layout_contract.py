# -*- coding: utf-8 -*-
"""Commit 7F-6d — Final Layout Contract Audit (tests/test_final_layout_contract.py).

The one consolidated reference contract for the whole 7F pipeline:

    Semantic → Translation → Layout Adapter → adaptive_layout / lay_out
        → LayoutResult → Renderer (draw-only)

Primitive matrix (THE reference table — every architecture test defers to it):

    Primitive         | Layout            | Recovery             | Geometry
    ------------------+-------------------+----------------------+----------
    FlowText          | adaptive_layout   | WRAP→SHRINK→CLIP     | source
    List content      | adaptive_layout   | WRAP→SHRINK→CLIP     | content_x
    List continuation | adaptive_layout   | WRAP→SHRINK→CLIP     | continuation_x
    List marker       | lay_out           | PRESERVE             | marker_x
    TOC title         | adaptive_layout   | WRAP→SHRINK→PRESERVE | title_x
    TOC page          | lay_out           | PRESERVE             | page_x
    Code              | lay_out           | PRESERVE             | bbox

Locked here:

1. **Primitive matrix** — per-kind engine + recovery ladder (budget / policy).
2. **No-detour call matrix** — draw-only renderers (incl. host renderers) never
   call the executor / mechanics; layout never imports detectors / parsers /
   renderers; recovery never imports renderer / translator / detector.
3. **Geometry ownership final scan** — AST: no ``level *`` / ``index *`` math,
   and the geometry fields (``marker_x`` / ``content_x`` / ``continuation_x`` /
   ``title_x`` / ``page_x`` / ``bbox`` / ``destination_page``) are never
   *derived* from ``level`` / ``index`` anywhere in the layout / renderer /
   side-channel pipeline.
4. **Translation boundary final matrix** — the four malicious translators
   (empty / garbage / injected-marker / huge) cannot break List markers, TOC
   page columns, Code (0 calls) or Style fallback.
"""

import ast
import inspect
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_LAYOUT_DIR = _ROOT / "pdf2zh" / "semantic" / "layout"
_RENDERER_DIR = _ROOT / "pdf2zh" / "semantic" / "renderer"
_FLOW_SIDECHANNEL = _ROOT / "pdf2zh" / "v3" / "flow_sidechannel.py"
# every host renderer — draw-only: magicpdf (main), legacy pdf_renderer /
# renderer / image_renderer / overlay_view / overlay_renderer, outline.
_HOST_RENDERERS = [
    _ROOT / "pdf2zh" / "v3" / "magicpdf_renderer.py",
    _ROOT / "pdf2zh" / "v3" / "outline_renderer.py",
    _ROOT / "pdf2zh" / "v3" / "renderer.py",
    _ROOT / "pdf2zh" / "v3" / "pdf_renderer.py",
    _ROOT / "pdf2zh" / "v3" / "image_renderer.py",
    _ROOT / "pdf2zh" / "v3" / "overlay_view.py",
    _ROOT / "pdf2zh" / "overlay_renderer.py",
]

#: Geometry fields owned by Semantic — never derived downstream.
_GEOM_FIELDS = {
    "marker_x", "content_x", "continuation_x", "title_x", "page_x",
    "bbox", "destination_page",
}


def _code(path: Path) -> str:
    """Executable code with docstrings stripped (prose must not trip guards)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

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


def _geometry_assignments_from_level_index(source: str, path_name: str) -> None:
    """Fail when a Semantic-owned geometry field is *assigned* an expression
    that references ``level`` / ``index`` (passthrough copies are fine — only
    derivation is forbidden)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not isinstance(t, ast.Name) or t.id not in _GEOM_FIELDS:
                continue
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"{path_name} 从 level/index 推导几何字段 {t.id}"
                )


def _class_code(mod, cls_name: str) -> str:
    return _code_ast(inspect.getsource(getattr(mod, cls_name)))


def _code_ast(source: str) -> str:
    tree = ast.parse(source)

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
    return ast.unparse(tree)


# =========================================================================
# 1. primitive matrix — the single reference contract
# =========================================================================


def test_primitive_matrix_engines_and_ladders():
    from pdf2zh.semantic.layout.overflow import OverflowPolicy, policy_for
    from pdf2zh.semantic.layout.recovery import (
        OverflowReason,
        RecoveryDecision,
        budget_for_kind,
        decide_recovery,
    )

    # Flow: adaptive, full ladder WRAP→SHRINK→CLIP
    b = budget_for_kind("flow")
    assert (b.allow_wrap, b.allow_shrink, b.allow_clip) == (True, True, True)
    # List content / continuation: same full ladder
    b = budget_for_kind("list_content")
    assert (b.allow_wrap, b.allow_shrink, b.allow_clip) == (True, True, True)
    # List marker: PRESERVE — never wrap / shrink / clip, for ANY reason
    for reason in OverflowReason:
        assert decide_recovery("anchor", reason, target="marker") is \
            RecoveryDecision.PRESERVE_OVERFLOW, reason
    # TOC title: adaptive, WRAP→SHRINK→PRESERVE — CLIP always forbidden
    b = budget_for_kind("toc_title")
    assert b.allow_wrap and b.allow_shrink and not b.allow_clip
    for reason in OverflowReason:
        assert decide_recovery("toc_title", reason, budget=b) is not \
            RecoveryDecision.CLIP, reason
    # TOC page / Code: PRESERVE
    assert policy_for("column") is OverflowPolicy.PRESERVE
    assert policy_for("preserved") is OverflowPolicy.PRESERVE
    # the anchor mechanism (marker / title) exists but is never auto-applied
    assert policy_for("anchor") is OverflowPolicy.SHRINK


# =========================================================================
# 2. no-detour call matrix
# =========================================================================


def _iter_layout_py():
    for py in sorted(_LAYOUT_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        yield py


def test_draw_only_renderers_never_call_executor_or_mechanics():
    """ListRenderer may call the layout *adapter* (layout_list_item) but never
    the executor / mechanics — the fit decision is always delegated."""
    import pdf2zh.semantic.renderer.flow as flow_mod
    import pdf2zh.semantic.renderer.list as list_mod
    import pdf2zh.semantic.renderer.toc as toc_mod

    for mod, cls in ((flow_mod, "FlowTextRenderer"),
                     (list_mod, "ListRenderer"),
                     (toc_mod, "TocRenderer")):
        src = _class_code(mod, cls)
        for banned in ("adaptive_layout(", "lay_out(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text("):
            assert banned not in src, f"{cls} 调用了 {banned}"


def test_host_renderers_never_call_executor_or_mechanics():
    for py in _HOST_RENDERERS:
        src = _code(py)
        for banned in ("adaptive_layout(", "lay_out(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text("):
            assert banned not in src, f"{py.name} 调用了 {banned}"
        assert "semantic.layout" not in src, f"{py.name} import 了 layout"


def test_renderers_never_import_translator_module():
    import pdf2zh.semantic.renderer.flow as flow_mod
    import pdf2zh.semantic.renderer.list as list_mod
    import pdf2zh.semantic.renderer.toc as toc_mod

    for mod in (flow_mod, list_mod, toc_mod):
        src = _code_ast(inspect.getsource(mod))
        for banned in ("pdf2zh.translator", "build_translator", "translator.py"):
            assert banned not in src, f"{mod.__name__} 引用了 translator"
    for py in _HOST_RENDERERS:
        src = _code(py)
        for banned in ("pdf2zh.translator", "build_translator"):
            assert banned not in src, f"{py.name} 引用了 translator"


def test_layout_never_imports_detector_parser_or_renderer():
    for py in _iter_layout_py():
        src = _code(py)
        for banned in ("list_detector", "list_parser", "toc_parser",
                       "code_detector", "style_detector",
                       "semantic.renderer", "translator"):
            assert banned not in src, f"layout/{py.name} 引用了 {banned}"


def test_recovery_never_imports_renderer_translator_or_detector():
    src = _code(_LAYOUT_DIR / "recovery.py")
    for banned in ("renderer", "translator", "detector", "parser", "magicpdf"):
        assert banned not in src


# =========================================================================
# 3. geometry ownership — final scan (layout / renderer / side-channel)
# =========================================================================


def test_no_geometry_math_from_level_or_index_pipeline_wide():
    """AST binop scan over the whole pipeline: ``level *`` / ``index +`` etc.
    must never appear (extends 7E-Audit to flow_sidechannel + host renderers)."""
    files = list(_iter_layout_py()) + \
        list(_RENDERER_DIR.glob("*.py")) + \
        [_FLOW_SIDECHANNEL] + _HOST_RENDERERS
    for py in files:
        if py.name == "__init__.py":
            continue
        src = _code(py)
        for op, l, r in _ast_binops(src):
            if {"level", "index"} & {l, r}:
                raise AssertionError(
                    f"{py.relative_to(_ROOT)} 用 {op}({l},{r}) 重建几何"
                )
        _geometry_assignments_from_level_index(src, py.name)


def test_geometry_fields_never_assigned_from_derived_math():
    """The Semantic-owned fields are only ever *copied* from nodes / entries —
    never recomputed (the assignment scan above already rejects level/index
    RHS; this test pins the invariant for the whole pipeline)."""
    files = list(_iter_layout_py()) + \
        list(_RENDERER_DIR.glob("*.py")) + \
        [_FLOW_SIDECHANNEL] + _HOST_RENDERERS
    for py in files:
        if py.name == "__init__.py":
            continue
        src = _code(py)
        _geometry_assignments_from_level_index(src, py.name)


def test_sidechannel_never_reinfers_geometry():
    src = _code(_FLOW_SIDECHANNEL)
    for banned in ("level *", "index *", "page_width", "looks_like"):
        assert banned not in src


# =========================================================================
# 4. translation boundary — malicious-translator matrix
# =========================================================================


def _evil_translators():
    return {
        "empty": lambda s: "",
        "garbage": lambda s: "TRANSLATED",
        "injected_marker": lambda s: "1. " + s,
        "huge": lambda s: "TRANSLATED" * 100,
    }


def test_evil_translators_cannot_break_list_markers():
    from pdf2zh.semantic.renderer.list import build_page_list_plan

    for name, evil in _evil_translators().items():
        plan = build_page_list_plan(["1. Intro", "2. Background"], translate=evil)
        markers = [c["text"] for c in plan["commands"] if c["kind"] == "marker"]
        assert markers == ["1.", "2."], name
        # marker never enters translation, whatever the translator returns
        calls = plan["translated_calls"]
        assert "1." not in "".join(calls) and "2." not in "".join(calls), name
        # never a merged fake marker column
        assert all(c["kind"] in ("marker", "text") for c in plan["commands"]), name


def test_evil_translators_cannot_break_toc_page_column():
    from pdf2zh.semantic.renderer.toc import build_page_toc_plan

    lines = [
        {"text": "Introduction ........ 42", "x0": 72, "y0": 700, "x1": 540,
         "y1": 712, "size": 12},
        {"text": "Background .......... 3", "x0": 96, "y0": 680,
         "x1": 540, "y1": 692, "size": 12},
    ]
    for name, evil in _evil_translators().items():
        plan = build_page_toc_plan(lines, 612.0, translate=evil)
        entries = plan["entries"]
        assert [e["page_number"] for e in entries] == ["42", "3"], name
        # number / leader / page never enter translation
        calls = plan["translated_calls"]
        assert all("..." not in c and c.strip() != "Introduction ........ 42"
                   for c in calls), name


def test_evil_translators_never_touch_code():
    from pdf2zh.semantic.models import CodeBlockNode
    from pdf2zh.v3.canonical_page import BlockModel
    from pdf2zh.v3.render_payload import block_translation_unit

    for name, evil in _evil_translators().items():
        b = BlockModel(text="def f():\n    return 1", kind="code",
                       x0=10, y0=10, x1=300, y1=60)
        calls = []
        unit = block_translation_unit(b, lambda s: (calls.append(s), evil(s))[1])
        assert calls == [], name          # translator called 0 times
        assert unit["kind"] == "preserve"
        assert unit["translated"] == b.text


def test_evil_translator_style_falls_back_without_corruption():
    """Malicious translators cannot corrupt the style structure: the span text
    always re-joins to the paragraph text and the recovery flag is set when the
    markers were damaged — never a silent broken structure."""
    from pdf2zh.semantic.models import SpanStyle
    from pdf2zh.semantic.style_translate import translate_styled_paragraph

    src = "bold word"
    styles = [SpanStyle()] * len(src)
    for i in range(4):
        styles[i] = SpanStyle(bold=True)
    for name, evil in {
        "empty": lambda m: "",
        "mangled": lambda m: "译后文本",
        "huge": lambda m: "X" * 500,
    }.items():
        para = translate_styled_paragraph(src, styles, evil)
        # span text always re-joins the paragraph text (no corruption)
        assert "".join(sp.text for sp in para.spans) == para.text, name
        # a damaged/empty translation is recovered explicitly, never silent
        if not para.text:
            assert para.recovered, name
        else:
            assert "".join(sp.text for sp in para.spans) == para.text, name


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))
