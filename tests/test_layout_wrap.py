# -*- coding: utf-8 -*-
"""Commit 7C — pure wrapping mechanics tests.

Covers ``pdf2zh.semantic.layout.wrap``:

- short text → single line
- long English → multiple lines within max_width
- CJK wraps at any glyph boundary
- mixed CJK + English wraps correctly
- a single overlong token never loops (and is kept whole, flagged by caller)

Uses a deterministic measurer (latin ~0.5em, CJK ~1em) so width bounds are
exact without touching any font.
"""

from pdf2zh.semantic.layout.wrap import (
    clip_text,
    shrink_to_fit,
    tokenize,
    wrap_lines,
)

_SIZE = 8.0  # latin 4pt, CJK 8pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80 or ch in " ":
            w += size if ord(ch) >= 0x2E80 else size * 0.5
        else:
            w += size * 0.5
    return w


def _bound(text):
    return lambda s: _measure(s)


def _strip(s):
    return "".join(s.split())


def _all_lines_within(lines, max_width):
    return all(w <= max_width + 1e-6 for _, w in lines)


# -- short text ------------------------------------------------------------


def test_short_text_single_line():
    lines = wrap_lines("Hello", _bound("Hello"), 200.0)
    assert len(lines) == 1
    assert lines[0][0] == "Hello"
    assert lines[0][1] <= 200.0


def test_short_cjk_single_line():
    lines = wrap_lines("中文", _bound("中文"), 200.0)
    assert len(lines) == 1
    assert lines[0][0] == "中文"


# -- long English ----------------------------------------------------------


def test_long_english_wraps():
    text = "The quick brown fox jumps over the lazy dog"
    lines = wrap_lines(text, _bound(text), 60.0)
    assert len(lines) >= 2
    assert _all_lines_within(lines, 60.0)
    # reconstruction preserves every character (single-spaced input)
    assert _strip(" ".join(ln for ln, _ in lines)) == _strip(text)


# -- CJK -------------------------------------------------------------------


def test_cjk_wraps_at_glyph_boundaries():
    text = "中文测试换行"
    max_w = 24.0  # 3 CJK glyphs per line at 8pt
    lines = wrap_lines(text, _bound(text), max_w)
    assert len(lines) >= 2
    assert _all_lines_within(lines, max_w)
    assert "".join(ln for ln, _ in lines) == text  # every glyph preserved


def test_long_cjk_no_infinite_break():
    text = "很" * 300  # 300 full-width glyphs
    lines = wrap_lines(text, _bound(text), 40.0)
    # terminates (5 glyphs per line) and preserves all glyphs
    assert len(lines) == 60
    assert "".join(ln for ln, _ in lines) == text
    assert _all_lines_within(lines, 40.0)


# -- mixed CJK + English ---------------------------------------------------


def test_mixed_cjk_english_wraps():
    text = "This 是一个 test 测试 混合句子"
    lines = wrap_lines(text, _bound(text), 40.0)
    assert len(lines) >= 2
    assert _all_lines_within(lines, 40.0)
    assert _strip(" ".join(ln for ln, _ in lines)) == _strip(text)


# -- overlong single token (no infinite loop) ------------------------------


def test_overlong_token_kept_whole():
    """A single token wider than max_width stays intact on one line — it must
    never be split into nonsense and must never loop."""
    word = "veryveryveryveryveryveryverylongidentifier"  # 47 chars @ 4pt = 188pt
    text = f"{word} tail"
    lines = wrap_lines(text, _bound(text), 30.0)
    assert len(lines) == 2
    assert word in lines[0][0]
    assert lines[1][0] == "tail"  # the rest wraps after the overlong token
    # the overlong line exceeds max_width -> that is the overflow the caller reports
    assert lines[0][1] > 30.0
    tail_w = lines[1][1]
    assert tail_w <= 30.0


def test_overlong_token_stress_terminates():
    """1000-char single token with a tiny width still returns promptly."""
    word = "x" * 1000
    lines = wrap_lines(word, _bound(word), 2.0)
    assert len(lines) == 1
    assert lines[0][0] == word


def test_whitespace_only_narrow():
    lines = wrap_lines("   ", _bound("   "), 10.0)
    assert len(lines) == 1


def test_empty_text_single_empty_line():
    assert wrap_lines("", _bound(""), 100.0) == [("", 0.0)]


def test_no_max_width_returns_single_line():
    lines = wrap_lines("hello world", _bound("hello world"), 0.0)
    assert len(lines) == 1
    assert lines[0][0] == "hello world"


# -- tokenize --------------------------------------------------------------


def test_tokenize_cjk_splits_each_glyph():
    toks = tokenize("a中文 b")
    kinds = [k for k, _ in toks]
    assert kinds == ["word", "cjk", "cjk", "space", "word"]
    # an adjacent CJK glyph is not swallowed by the latin run
    assert [k for k, _ in toks if k != "space"] == ["word", "cjk", "cjk", "word"]


# -- shrink_to_fit (mechanism) ---------------------------------------------


def test_shrink_when_fits_stays_same():
    size, over = shrink_to_fit("Hi", _measure, 20.0, _SIZE)
    assert size == _SIZE
    assert over is False


def test_shrink_reduces_font_to_fit():
    # "这是很长很长的中文内容" is far wider than width at 8pt
    text = "这是很长很长的中文内容句子"
    size, over = shrink_to_fit(text, _measure, 60.0, _SIZE, min_font_size=2.0)
    assert size < _SIZE
    assert over is False
    assert _measure(text, size) <= 60.0 + 1e-6


def test_shrink_clamps_to_min_font():
    text = "中文中文中文中文中文中文中文"
    size, over = shrink_to_fit(text, _measure, 4.0, _SIZE, min_font_size=5.0)
    assert size == 5.0
    assert over is True  # even min font cannot fit 4pt width


# -- clip_text (last resort) -----------------------------------------------


def test_clip_fitting_text_untouched():
    out, over = clip_text("Hello", _bound("Hello"), 200.0)
    assert out == "Hello"
    assert over is False


def test_clip_truncates_and_reports_overflow():
    text = "这是一个非常长的需要被截断的中文标题文字内容"
    out, over = clip_text(text, _bound(text), 50.0)
    assert over is True  # never silent
    assert len(out) < len(text)  # actually truncated
    # the clip (with ellipsis) fits the width
    assert _bound(text)(out) <= 50.0 + 1e-6
