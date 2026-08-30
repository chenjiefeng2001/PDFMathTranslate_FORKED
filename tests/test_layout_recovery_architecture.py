"""Commit 7F — recovery-layer architecture tests.

``pdf2zh/semantic/layout/recovery.py`` is a pure **policy** layer:

    LayoutResult (overflow)
        ↓ classify_reason → OverflowReason
        ↓ decide_recovery → RecoveryDecision / OverflowDiagnosis
        ↓ Renderer executes (draw only)

Locked guarantees:

1. **No semantic detection** — never references detector / parser / `looks_like`.
2. **No renderer coupling** — never imports / references the renderer package or
   `magicpdf_renderer`.
3. **No translator** — never translates text.
4. **No geometry reconstruction** — never derives placement from ``level`` /
   ``index`` (no ``level *`` / ``index *`` math).
5. **Decision-only** — never **executes** wrap / shrink / clip directly; it only
   returns a :class:`RecoveryDecision`, and the layout side-channel actually
   runs it via ``lay_out``.
6. Imports come only from the layout layer (primitives / overflow / wrap) —
   nothing below the geometry stack.
"""

import inspect

import pdf2zh.semantic.layout.recovery as rec


# draw-only / detection-free source (docstrings stripped via AST).
def _code():
    import ast
    tree = ast.parse(inspect.getsource(rec))

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


def test_recovery_never_detects():
    for banned in ("looks_like", "detect_", "parse_", "list_parser", "toc_parser",
                   "code_detector", "list_detector", "style_detector"):
        assert banned not in _SRC, f"recovery must not reference detection ({banned})"


def test_recovery_never_references_renderer():
    for banned in ("renderer", "magicpdf", "insert_text", "draw"):
        assert banned not in _SRC, f"recovery must not draw/couple to a renderer ({banned})"


def test_recovery_never_translates():
    for banned in ("translate", "translator"):
        assert banned not in _SRC, f"recovery must not translate ({banned})"


def test_recovery_no_geometry_from_level_or_index():
    # no `level *` / `index *` and no use of those names period.
    for banned in ("level", "index", "level ", "index "):
        assert banned not in _SRC, f"recovery must not derive geometry from {banned.strip()}"


def test_recovery_is_decision_only_no_execution():
    # it returns RecoveryDecision; it never runs the mechanic itself.
    for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
        assert banned not in _SRC, f"recovery must not execute fit directly ({banned})"
    assert "RecoveryDecision" in _SRC


def test_recovery_imports_only_layout_stack():
    # imports restricted to the layout layer (primitives / overflow / wrap);
    # must not drag in semantic detectors, renderers or translator modules.
    for name in ("toc_sidechannel", "list_sidechannel", "style_translate",
                 "document_model", "semantic.renderer", "semantic.models"):
        assert name not in _SRC, f"recovery imported {name}"


def test_recovery_exports_policy_vocabulary():
    assert rec.OverflowReason
    assert rec.RecoveryDecision
    assert rec.LayoutBudget
    assert rec.OverflowDiagnosis
    assert callable(rec.classify_reason)
    assert callable(rec.decide_recovery)
    assert callable(rec.diagnose_overflow)


def test_recovery_enums_have_expected_members():
    assert {r.value for r in rec.OverflowReason} == {
        "width", "height", "unbreakable_token",
        "fixed_column_collision", "preserved_region",
    }
    assert {d.value for d in rec.RecoveryDecision} == {
        "no_action", "wrap", "shrink", "clip", "preserve_overflow",
    }


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))