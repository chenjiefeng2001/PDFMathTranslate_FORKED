# -*- coding: utf-8 -*-
"""Commit 7F-6c-5 — recovery architecture audit.

Locks the 7F-6c unification (one executor, not three):

1. **One public executor** — :func:`adaptive_layout` is the only ``adaptive_*``
   entry point; no module defines a second hand-rolled recovery loop.
2. **List / TOC route through it** — ``list_layout`` / ``toc_layout`` call
   ``adaptive_layout`` (differences come from kind / target / budget); the
   TOC hand-rolled ladder is gone.
3. **Draw-only renderers never execute recovery** — ``FlowTextRenderer`` /
   ``ListRenderer`` / ``TocRenderer`` and the host ``magicpdf_renderer`` never
   call ``adaptive_layout`` / ``wrap_lines`` / ``shrink_to_fit`` /
   ``clip_text``.
4. **Policy layer stays pure** — ``recovery.py`` references no renderer /
   translator / detector / parser.
5. **Geometry ownership** — ``list_layout`` / ``toc_layout`` never rebuild an
   x coordinate from ``level`` / ``index``; the marker and page channels stay
   on raw ``lay_out`` (never adaptive).
"""

import ast
import inspect
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_LAYOUT_DIR = _ROOT / "pdf2zh" / "semantic" / "layout"
_RENDERER_DIR = _ROOT / "pdf2zh" / "semantic" / "renderer"
_MAGICPDF = _ROOT / "pdf2zh" / "v3" / "magicpdf_renderer.py"


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


def _class_code(mod, cls_name: str) -> str:
    src = inspect.getsource(getattr(mod, cls_name))
    return _code_ast(src)


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


# ---------------------------------------------------------------------------
# 1. one public executor
# ---------------------------------------------------------------------------


def test_adaptive_layout_is_the_only_public_executor():
    """No module (layout or renderer) defines a second ``adaptive_*`` entry
    point — differences come from kind / target / budget, not new executors."""
    for d in (_LAYOUT_DIR, _RENDERER_DIR):
        for py in sorted(d.glob("*.py")):
            if py.name == "__init__.py":
                continue
            src = _code(py)
            for stmt in ast.walk(ast.parse(src)):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if stmt.name.startswith("adaptive") and stmt.name != "adaptive_layout":
                        raise AssertionError(
                            f"{py.relative_to(_ROOT)} 定义了第二个 executor: {stmt.name}"
                        )


def test_adaptive_layout_still_exported():
    import pdf2zh.semantic.layout.adaptive as mod

    assert callable(mod.adaptive_layout)


# ---------------------------------------------------------------------------
# 2. List / TOC route through adaptive_layout; no hand-rolled ladder
# ---------------------------------------------------------------------------


def test_list_layout_calls_adaptive_layout():
    src = _code(_LAYOUT_DIR / "list_layout.py")
    assert "adaptive_layout(" in src
    assert "budget_for_kind(" in src and "list_content" in src  # 共享 list_content budget


def test_toc_layout_calls_adaptive_layout_no_own_ladder():
    src = _code(_LAYOUT_DIR / "toc_layout.py")
    assert "adaptive_layout(" in src
    # the hand-rolled TOC ladder is gone (moved into the single executor)
    assert "_fit_toc_title" not in src
    assert "_SHRINK_STEP" not in src
    assert "_title_shrink_floor" not in src


def test_marker_and_page_stay_on_raw_lay_out():
    """The immovable channels never enter the adaptive executor: list marker
    is a plain ``lay_out`` call; TOC page stays a FixedColumn PRESERVE."""
    list_src = _code(_LAYOUT_DIR / "list_layout.py")
    assert "marker = lay_out(" in list_src
    toc_src = _code(_LAYOUT_DIR / "toc_layout.py")
    assert "FixedColumn(text=page_text" in toc_src
    # page channel is not adaptive: no adaptive_layout call on the page prim
    assert "page_result = _lay(" in toc_src  # plain lay_out via _lay


# ---------------------------------------------------------------------------
# 3. draw-only renderers never execute recovery
# ---------------------------------------------------------------------------


def test_flow_renderer_class_never_executes_recovery():
    import pdf2zh.semantic.renderer.flow as mod

    src = _class_code(mod, "FlowTextRenderer")
    for banned in ("adaptive_layout(", "wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src


def test_list_renderer_never_executes_recovery():
    import pdf2zh.semantic.renderer.list as mod

    src = _class_code(mod, "ListRenderer")
    for banned in ("adaptive_layout(", "wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src


def test_toc_renderer_never_executes_recovery():
    import pdf2zh.semantic.renderer.toc as mod

    src = _class_code(mod, "TocRenderer")
    for banned in ("adaptive_layout(", "wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src


def test_magicpdf_renderer_never_executes_recovery():
    src = _code(_MAGICPDF)
    for banned in ("adaptive_layout(", "wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in src
    assert "semantic.layout" not in src  # never imports the layout/recovery layer


# ---------------------------------------------------------------------------
# 4. policy layer stays pure
# ---------------------------------------------------------------------------


def test_recovery_policy_imports_only_policy_stack():
    src = _code(_LAYOUT_DIR / "recovery.py")
    for banned in ("renderer", "translator", "detector", "parser", "magicpdf"):
        assert banned not in src


# ---------------------------------------------------------------------------
# 5. geometry ownership — no level/index math in the layout adapters
# ---------------------------------------------------------------------------


def test_list_layout_no_level_index_geometry_math():
    src = _code(_LAYOUT_DIR / "list_layout.py")
    for op, l, r in _ast_binops(src):
        if {"level", "index"} & {l, r}:
            raise AssertionError(f"list_layout 用 {op}({l},{r}) 重建几何")


def test_toc_layout_no_level_index_geometry_math():
    src = _code(_LAYOUT_DIR / "toc_layout.py")
    for op, l, r in _ast_binops(src):
        if {"level", "index"} & {l, r}:
            raise AssertionError(f"toc_layout 用 {op}({l},{r}) 重建几何")


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))
