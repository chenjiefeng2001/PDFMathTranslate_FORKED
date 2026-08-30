# -*- coding: utf-8 -*-
"""Commit 7F-4 — adaptive execution-layer architecture tests.

``pdf2zh/semantic/layout/adaptive.py`` is the **execution** layer sitting on top
of ``recovery.py`` (policy):

    LayoutResult overflow → classify/decide (recovery.py) → adaptive_layout (this layer)

Locked guarantees:

1. **No semantic detection** — never references detector / parser / `looks_like`.
2. **No translator** — never translates text.
3. **No renderer coupling** — never references renderer / magicpdf / drawing.
4. **No geometry reconstruction** — never derives placement from ``level`` /
   ``index``.
5. **Execution via a single fit engine** — never calls
   ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text`` directly; only ``lay_out``.
6. **Finite by construction** — recovery stages are a fixed, sequenced ladder
   (WRAP → SHRINK → CLIP), each executed at most once.
"""

import ast
import inspect

import pdf2zh.semantic.layout.adaptive as adap


def _code():
    tree = ast.parse(inspect.getsource(adap))

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


_SRC = _code()


def test_adaptive_never_detects():
    for banned in ("looks_like", "detect_", "parse_", "list_parser", "toc_parser",
                   "list_detector", "code_detector", "style_detector"):
        assert banned not in _SRC, f"adaptive must not detect ({banned})"


def test_adaptive_never_translates():
    for banned in ("translate", "translator"):
        assert banned not in _SRC, f"adaptive must not translate ({banned})"


def test_adaptive_never_references_renderer():
    for banned in ("renderer", "magicpdf", "insert_text", "draw"):
        assert banned not in _SRC, f"adaptive must not draw/couple to renderer ({banned})"


def test_adaptive_no_geometry_from_level_or_index():
    for banned in ("level", "index"):
        assert banned not in _SRC, f"adaptive must not derive geometry from {banned}"


def test_adaptive_executes_only_through_lay_out():
    # only fit engine used is lay_out; the 7C mechanics are never invoked here.
    assert "lay_out(" in _SRC
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in _SRC, f"adaptive must not call fit mechanic directly ({banned})"


def test_adaptive_is_finite_state_machine():
    # exactly one WRAP / SHRINK / CLIP stage block each; no `while overflow`
    assert "while " not in _SRC
    assert "Stage 1: WRAP" in _SRC or "stage" in _SRC.lower()
    for stage in ("WRAP", "SHRINK", "CLIP"):
        assert stage in _SRC, f"missing recovery stage {stage}"


def test_adaptive_imports_only_layout_stack():
    for name in ("toc_sidechannel", "list_sidechannel", "style_translate",
                 "document_model", "semantic.renderer", "semantic.models"):
        assert name not in _SRC, f"adaptive imported {name}"


def test_adaptive_exposes_single_entry_point():
    assert callable(adap.adaptive_layout)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__]))