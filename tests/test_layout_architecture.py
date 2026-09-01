# -*- coding: utf-8 -*-
"""Commit 7B — layout-layer architecture assertions.

The layout layer must stay a pure geometry carrier, never the semantic
detector:

1. No ``looks_like_*`` heuristics — semantic detection ended in the semantic
   layer.
2. No ``detect_code`` / ``detect_list`` / ``detect_toc`` entry points.
3. Geometry is never re-generated from ``level * constant`` / ``index *
   width`` — original geometry flows through verbatim.
4. No import of semantic detectors / parsers from the layout package.
5. Reuses the renderers' "no looks_like / no isinstance" guard from 7A on the
   layout modules too.
"""

import inspect
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LAYOUT_DIR = _HERE.parent / "pdf2zh" / "semantic" / "layout"


def _layout_modules():
    for py in sorted(_LAYOUT_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        yield py, py.stem


def _code_lines(src: str) -> list:
    """Return code lines, excluding docstrings (incl. multi-line bodies).

    Mirrors ``test_architecture_7a._code_lines`` so the docstring bodies of
    the layout modules (which legitimately *describe* the forbidden patterns)
    don't trip the assertions.
    """
    out = []
    in_doc = None
    for ln in src.splitlines():
        s = ln.strip()
        if not s:
            continue
        if in_doc:
            if s.endswith(in_doc):
                in_doc = None
            continue
        if s.startswith('"""') or s.startswith("'''"):
            in_doc = '"' if s.startswith('"""') else "'"
            if s.endswith(in_doc) and len(s) > 3:
                in_doc = None
            continue
        if s.startswith("#"):
            continue
        out.append(ln)
    return out


def _all_layout_code_lines():
    lines = []
    import importlib

    # non-init layout modules (imported to also catch their imports via source)
    for _py, name in _layout_modules():
        mod = importlib.import_module(f"pdf2zh.semantic.layout.{name}")
        lines.extend(_code_lines(inspect.getsource(mod)))
    # __init__.py participates in the token guard too
    init = _LAYOUT_DIR / "__init__.py"
    lines.extend(_code_lines(init.read_text(encoding="utf-8")))
    return lines


# -- 1/2. No semantic detection entry points -------------------------------


def test_no_looks_like_heuristics_in_layout():
    src = "\n".join(_all_layout_code_lines())
    assert "looks_like" not in src


def test_no_detect_entry_points_in_layout():
    src = "\n".join(_all_layout_code_lines())
    for token in ("detect_code", "detect_list", "detect_toc", "detect_span"):
        assert token not in src


# -- 3. Geometry never re-derived from level/index -------------------------


def test_no_level_times_constant_in_layout():
    src = "\n".join(_all_layout_code_lines())
    assert not any(pat in src for pat in ("level *", "* level", "level_level"))


def test_no_index_times_width_in_layout():
    src = "\n".join(_all_layout_code_lines())
    assert not any(pat in src for pat in ("index *", "* index"))


# -- 4. No import of semantic detectors / parsers --------------------------


def test_layout_does_not_import_semantic_parsers():
    import importlib

    forbidden_subs = (
        "list_detector",
        "list_parser",
        "toc_parser",
        "code_detector",
        "style_detector",
    )
    for _py, name in _layout_modules():
        mod = importlib.import_module(f"pdf2zh.semantic.layout.{name}")
        src = inspect.getsource(mod)
        for token in forbidden_subs:
            assert token not in src, f"{name}.py must not reference {token}"


# -- 5. Renderers keep the 7A no-heuristics / no-isinstance guard ----------


def test_toc_renderer_still_no_looks_like_no_isinstance():
    import pdf2zh.semantic.renderer.toc as toc_mod

    src = inspect.getsource(toc_mod)
    assert "looks_like" not in src
    assert "isinstance(" not in src


def test_layout_package_has_expected_modules():
    names = {name for _py, name in _layout_modules()}
    assert {
        "primitives",
        "constraints",
        "measure",
        "mapping",
        "wrap",
        "overflow",
    } <= names


# -- 7C: Code is locked out of the wrapping path ---------------------------


def test_code_primitive_is_always_preserve_never_wrapped():
    """Code (PreservedRegion) must never be fed into the generic wrapping
    algorithm — its policy is PRESERVE and ``lay_out`` returns a single
    verbatim line, geometry immutable."""
    from pdf2zh.semantic.layout.overflow import OverflowPolicy, lay_out
    from pdf2zh.semantic.layout.primitives import PreservedRegion

    text = "def very_long_function_name_that_nobody_should_break():"
    prim = PreservedRegion(text=text, bbox=(10.0, 10.0, 300.0, 26.0))
    result = lay_out(prim, measure=lambda s, sz: len(s) * sz)
    assert result.policy is OverflowPolicy.PRESERVE
    assert result.lines == [text]  # verbatim, not split into fragments
    assert result.bbox == (10.0, 10.0, 300.0, 26.0)


def test_overflow_engine_does_not_auto_shrink_anchors():
    """7C mechanism check: SHRINK exists but is not auto-applied to anchors,
    so TOC titles / list items are not silently resized this commit."""
    from pdf2zh.semantic.layout.overflow import OverflowPolicy, lay_out
    from pdf2zh.semantic.layout.primitives import FixedAnchor

    prim = FixedAnchor(
        text="一个很长的标题", x=72.0, y=30.0, max_width=30.0, role="title_x"
    )
    r = lay_out(prim, measure=lambda s, sz: len(s) * sz, font_size=10.0)
    assert r.policy is OverflowPolicy.SHRINK
    assert r.font_size == 10.0  # not shrunk by default
    assert r.overflow is True
