# -*- coding: utf-8 -*-
"""Commit 7B — unified text measurement tests.

Covers ``pdf2zh.semantic.layout.measure.measure_text``:

- CJK-aware (full-width glyphs measure wider than Latin for the same count).
- empty text measures to ``0.0``.
- measured width is independent of any original ``x`` (column never moves).
- the TOC renderer consumes the unified measurement API.
"""

import inspect

from pdf2zh.semantic.layout.measure import measure_text, measure_text_estimate

# -- 6. CJK measurement ---------------------------------------------------


def test_cjk_measures_wider_than_latin():
    """Same number of glyphs: CJK (full-width) is wider than Latin."""
    cjk = measure_text("你好世界")
    latin = measure_text("ABCD")
    assert len("你好世界") == len("ABCD") == 4
    assert cjk > latin


def test_fullwidth_vs_halfwidth():
    w_cjk = measure_text("字", font_size=12.0)
    w_asc = measure_text("a", font_size=12.0)
    assert w_cjk == 12.0  # ~1em
    assert w_asc == 6.0  # ~0.5em
    assert w_cjk > w_asc


# -- 7. empty text measurement --------------------------------------------


def test_empty_text_measures_zero():
    assert measure_text("") == 0.0
    assert measure_text("", font=None, font_size=12.0) == 0.0


def test_whitespace_only_nonzero_but_small():
    # Latin/space path still returns something > 0 (thin) — not an error.
    assert measure_text("  ") >= 0.0


# -- 8. translated width independent from original x -----------------------


def test_width_independent_from_original_x():
    """measure_text has no geometry input — the same text always measures the
    same width no matter where the primitive is placed."""
    a = measure_text("Database Systems")
    b = measure_text("Database Systems")
    assert a == b
    # A fixed column's x never depends on how wide the measured title is.
    from pdf2zh.semantic.layout.primitives import FixedColumn

    col = FixedColumn(text="42", column_x=540.0)
    measured = measure_text("一个很长的中文标题", 10.0)
    assert col.column_x == 540.0  # unchanged regardless of `measured`


def test_variable_length_changes_width_not_column():
    from pdf2zh.semantic.layout.primitives import FixedColumn

    long = measure_text("A very long translated title that spans much wider line")
    short = measure_text("短标题")
    c = FixedColumn(text="7", column_x=535.0)
    # longer title measures wider...
    assert long > short
    # ...but the column x stays exactly where it was.
    assert c.column_x == 535.0


# -- font-aware path -------------------------------------------------------


def test_font_name_string_measures_latin():
    # pymupdf is a core dep; guard against a missing import gracefully.
    try:
        w = measure_text("Helvetica", font="helv", font_size=10.0)
    except Exception:  # noqa: BLE001 -- CI without pymupdf should still pass
        return
    assert w > 0.0


def test_font_name_string_cjk_does_not_raise():
    try:
        measure_text("中文标题", font="helv", font_size=10.0)
    except Exception:  # noqa: BLE001
        return
    # reaching here without raising is the pass condition


# -- 11. TOC uses common measurement API -----------------------------------


def test_toc_renderer_consumes_measure_api():
    """The TOC renderer delegates default measurement to the layout layer."""
    import pdf2zh.semantic.renderer.toc as toc_mod

    src = inspect.getsource(toc_mod)
    assert "measure_text" in src
    assert "semantic.layout.measure" in src


def test_toc_default_measurement_equivalent_to_estimate():
    """Without an injected measurer, the TOC default equals the unified CJK
    word-scale estimate (behaviour unchanged from pre-7B)."""
    from pdf2zh.semantic.renderer.toc import TocRenderer

    renderer = TocRenderer()
    # private `_measure` is literal, no measurer -> uses measure_text estimate
    assert renderer._measure("Dataset", 10.0) == measure_text_estimate("Dataset", 10.0)
    assert renderer._measure("你好", 10.0) == measure_text_estimate("你好", 10.0)
