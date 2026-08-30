"""Commit 7E-3 — TOC layout architecture tests.

Locks the 7E-3 contract with source-level assertions (docstrings/comments
stripped via AST so prose can never trip them):

1.  ``semantic/layout/toc_layout.py`` (the layout adapter):
    -  **no semantic detection**: never calls ``detect_toc`` / ``parse_toc`` /
       ``looks_like`` — "what this is" was decided upstream;
    -  **no geometry inference**: never derives x from ``level``, entry
       ``index`` or a ``level * constant`` formula — ``title_x`` / ``page_x``
       come verbatim from the entry;
    -  **one fit engine**: never calls ``wrap_lines`` / ``shrink_to_fit`` /
       ``clip_text`` directly — every fit / overflow decision goes through
       ``lay_out``;
    -  **no translator**: the adapter only lays out pre-translated text.
2.  ``semantic/renderer/toc.py`` — the **draw-only** path (``TocRenderer``):
    -  **no semantic detection** (no detect/parse/looks_like);
    -  **no re-layout / re-inference** (no wrap/shrink/clip, no ``level *`` /
       ``index *`` geometry) — it consumes settled ``LayoutResult`` commands;
    -  translation reaches only the title / continuation strings.
    ``build_page_toc_plan`` is the golden *composition* chain (detect → parse
    → split → translate → render) and is intentionally out of scope here.
"""

import ast
import inspect
import unittest

from pdf2zh.semantic.layout import toc_layout as layout_mod
from pdf2zh.semantic.renderer import toc as renderer_mod


def _strip_docstrings(source: str) -> str:
    """Drop module/class/function docstrings (prose must not trip checks).

    String *constants* inside expressions are kept — e.g. the ``"title_x"``
    keys read from entries — only standalone docstring statements go.
    """
    tree = ast.parse(source)

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


def _code(mod):
    return _strip_docstrings(inspect.getsource(mod))


def _renderer_draw_path():
    """Only the draw-only class of the renderer module (not the golden
    composition chain ``build_page_toc_plan``)."""
    return _strip_docstrings(inspect.getsource(renderer_mod.TocRenderer))


class TestTocLayoutArchitecture(unittest.TestCase):
    def test_layout_adapter_never_detects_or_parses(self):
        src = _code(layout_mod)
        for banned in ("detect_toc(", "parse_toc(", "looks_like("):
            self.assertNotIn(banned, src)

    def test_layout_adapter_never_infers_geometry_from_level_or_index(self):
        src = _code(layout_mod)
        for banned in ("level *", "index *"):
            self.assertNotIn(banned, src)
        # the only per-entry geometry sources are the verbatim fields
        # (ast.unparse normalises quotes to single)
        self.assertIn("_entry_value(entry, 'title_x'", src)
        self.assertIn("_entry_value(entry, 'page_x'", src)

    def test_layout_adapter_routes_all_fit_decisions_through_lay_out(self):
        src = _code(layout_mod)
        for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
            self.assertNotIn(banned, src)
        self.assertIn("lay_out(", src)

    def test_layout_adapter_never_touches_a_translator(self):
        src = _code(layout_mod)
        self.assertNotIn("translate(", src)
        self.assertNotIn("translator", src)


class TestTocRendererArchitecture(unittest.TestCase):
    def test_renderer_never_detects_or_parses(self):
        src = _renderer_draw_path()
        for banned in ("detect_toc(", "parse_toc(", "looks_like("):
            self.assertNotIn(banned, src)

    def test_renderer_never_relayouts_or_reinfers_geometry(self):
        src = _renderer_draw_path()
        for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
            self.assertNotIn(banned, src)
        for banned in ("level *", "index *", "page_width *"):
            self.assertNotIn(banned, src)

    def test_renderer_consumes_settled_layout_result(self):
        src = _renderer_draw_path()
        self.assertIn("layout_toc_entry(", src)
        self.assertIn("toc_layout_commands(", src)

    def test_renderer_translation_reaches_only_title_and_continuation(self):
        src = _renderer_draw_path()
        # the only tr() call sites are the title and continuation strings
        self.assertIn("tr(title_only)", src)
        self.assertIn("tr(c.strip())", src)


if __name__ == "__main__":
    unittest.main()
