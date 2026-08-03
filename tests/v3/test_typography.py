"""阶段七 Adaptive Typography Engine — unit tests.

Run with:
    python -m pytest tests/v3/test_typography.py -v
"""
from __future__ import annotations
import json

import pytest

from pdf2zh.v3.typography import (
    is_cjk, GlyphMetric, TypographyMetrics, GlyphProbe, AdaptiveTypography,
)


def test_is_cjk_detection():
    assert is_cjk("机")
    assert is_cjk("中")
    assert is_cjk("あ")
    assert is_cjk("한")
    assert is_cjk("（")
    assert not is_cjk("a")
    assert not is_cjk(" ")
    assert not is_cjk("")


def test_cjk_wider_than_ascii():
    assert GlyphProbe.char_width("中", 12.0) > GlyphProbe.char_width("a", 12.0)


def test_text_width_scales_with_font_size():
    w12 = GlyphProbe.text_width("hello world", 12.0)
    w24 = GlyphProbe.text_width("hello world", 24.0)
    assert w24 == pytest.approx(2 * w12)


def test_cjk_fraction():
    assert GlyphProbe.cjk_fraction("机器学习") == 1.0
    assert GlyphProbe.cjk_fraction("abc") == 0.0
    assert GlyphProbe.cjk_fraction("AI智能") == 0.5
    assert GlyphProbe.cjk_fraction("") == 0.0


def test_break_lines_fits_container():
    text = "This is a fairly long English sentence that should wrap"
    lines = GlyphProbe.break_lines(text, container_width=150.0, font_size=12.0)
    assert len(lines) >= 2
    for ln in lines:
        assert GlyphProbe.text_width(ln, 12.0) <= 150.0 + 1e-6


def test_line_height_cjk_higher_than_latin():
    cjk_h = AdaptiveTypography.line_height_for("中文", 12.0)
    latin_h = AdaptiveTypography.line_height_for("abc", 12.0)
    assert cjk_h > latin_h
    assert cjk_h == pytest.approx(12.0 * 1.45)
    assert latin_h == pytest.approx(12.0 * 1.20)


def test_paragraph_spacing_scaled():
    spacing = AdaptiveTypography.paragraph_spacing_for("中文", 12.0)
    assert spacing == pytest.approx(12.0 * 1.45 * 0.5)


def test_expansion_ratio_cjk_growth():
    ratio = AdaptiveTypography.expansion_ratio(
        "机器学习模型正在快速演进", "Machine learning")
    assert ratio > 1.0
    assert AdaptiveTypography.expansion_ratio("x", None) == 1.0
    assert AdaptiveTypography.expansion_ratio("", "") == 1.0


def test_metrics_cjk_dominant():
    ty = AdaptiveTypography(container_width=450.0, font_size=12.0)
    m = ty.metrics("机器学习模型", source="Machine learning model")
    assert isinstance(m, TypographyMetrics)
    assert m.is_cjk_dominant
    assert m.line_height > 12.0
    assert m.block_height == pytest.approx(len(m.lines) * m.line_height)
    assert m.estimated_width > 0.0
    json.dumps(m.to_dict())  # JSON-able snapshot


def test_auto_fit_shrinks_font_on_overflow():
    ty = AdaptiveTypography(container_width=80.0, font_size=12.0)
    long_text = ("This sentence is far too long for the narrow container "
                 "and must be shrunk so it fits inside the small box")
    fit = ty.auto_fit_font_size(long_text, font_size=12.0,
                                container_width=80.0, max_lines=3)
    assert fit < 12.0
    for ln in GlyphProbe.break_lines(long_text, 80.0, fit):
        assert GlyphProbe.text_width(ln, fit) <= 80.0 + 1e-6
    for ln in GlyphProbe.break_lines(long_text, 80.0, fit):
        assert GlyphProbe.text_width(ln, fit) <= 80.0 + 1e-6


def test_auto_fit_respects_max_lines():
    ty = AdaptiveTypography(container_width=200.0, font_size=12.0)
    long_text = "one two three four five six seven eight nine ten"
    fit = ty.auto_fit_font_size(long_text, max_lines=1)
    lines = GlyphProbe.break_lines(long_text, 200.0, fit)
    assert len(lines) <= 1
    assert fit >= AdaptiveTypography.MIN_FONT_SIZE


def test_baseline_metrics_cjk():
    ty = AdaptiveTypography()
    bm = ty.baseline_metrics("中文")
    assert bm["cjk_dominant"]
    assert bm["ascent"] == pytest.approx(12.0 * 0.88)
    assert bm["descent"] == pytest.approx(12.0 * 0.12)
    assert bm["baseline_offset"] == bm["ascent"]


def test_baseline_metrics_latin():
    ty = AdaptiveTypography()
    bm = ty.baseline_metrics("abc")
    assert not bm["cjk_dominant"]
    assert bm["latin_baseline_offset"] == 0.0


def test_glyph_metric_dataclass():
    g = GlyphMetric(char="中", width=12.0, is_cjk=True)
    assert g.char == "中"
    assert g.is_cjk
