"""单元测试：粗体/斜体检测 + 样式标记保护（Phase 2）。

覆盖 ``pdf2zh.semantic.style_detector``：
- detect_span_style：字体名 + flags 双信号（名含 Bold/Italic/Oblique，ForcedBold 位）；
- inject/extract 往返：译文干净文本 + 逐字样式表；
- 对齐失败 / 无标记时优雅降级。
"""

from pdf2zh.semantic.models import SpanStyle
from pdf2zh.semantic.style_detector import (
    FORCED_BOLD_FLAG,
    detect_span_style,
    extract_style_markers,
    inject_style_markers,
)


# ── detect_span_style ───────────────────────────────────────
def test_bold_from_name():
    assert detect_span_style("TimesNewRomanPS-BoldMT") == SpanStyle(bold=True)
    assert detect_span_style("ABC+NotoSans-Black") == SpanStyle(bold=True)


def test_italic_from_name():
    assert detect_span_style("Arial-ItalicMT") == SpanStyle(italic=True)
    assert detect_span_style("Helvetica-Oblique") == SpanStyle(italic=True)


def test_plain_font_not_styled():
    assert detect_span_style("ArialMT") == SpanStyle()


def test_bold_and_italic_together():
    assert detect_span_style("Times-BoldItalicMT") == SpanStyle(bold=True, italic=True)


def test_forced_bold_flag():
    assert detect_span_style("SomeWeirdFont", FORCED_BOLD_FLAG) == SpanStyle(bold=True)
    assert detect_span_style("ArialMT") != SpanStyle(bold=True)


# ── inject / extract roundtrip ──────────────────────────────
def _styles_for(text, runs):
    """runs = [(start_end, SpanStyle), ...] → per-char list."""
    out = [SpanStyle()] * len(text)
    for (s, e), st in runs:
        for i in range(s, e):
            out[i] = st
    return out


def test_roundtrip_simple_bold():
    text = "This is important text."
    styles = _styles_for(text, [((8, 17), SpanStyle(bold=True))])
    marked = inject_style_markers(text, styles)
    clean, got = extract_style_markers(marked)
    assert clean == text
    assert got == styles


def test_roundtrip_nested_bold_italic():
    text = "word"
    styles = _styles_for(text, [((0, 4), SpanStyle(bold=True, italic=True))])
    marked = inject_style_markers(text, styles)
    assert "<b0>" in marked and "<i0>" in marked
    clean, got = extract_style_markers(marked)
    assert clean == text
    assert got == styles  # 逐字样式：整词粗+斜


def test_roundtrip_mixed_runs():
    text = "A bold and italic."
    styles = _styles_for(
        text,
        [
            ((2, 6), SpanStyle(bold=True)),
            ((10, 16), SpanStyle(italic=True)),
        ],
    )
    marked = inject_style_markers(text, styles)
    clean, got = extract_style_markers(marked)
    assert clean == text
    assert got == styles


def test_translator_preserves_markers_parse():
    # 模拟翻译服务：保留样式标记、翻译内部文字
    marked = "<b0>粗体</b0> 与 <i1>斜体</i1>"
    clean, styles = extract_style_markers(marked)
    assert clean == "粗体 与 斜体"  # 标记被剥离
    assert len(styles) == len(clean)
    # "粗体" → 粗（index 0-1）；"斜体" → 斜（index 5-6）
    assert styles[0] == SpanStyle(bold=True)
    assert styles[1] == SpanStyle(bold=True)
    assert styles[5] == SpanStyle(italic=True)
    assert styles[6] == SpanStyle(italic=True)


def test_misaligned_styles_no_injection():
    text = "abc"
    marked = inject_style_markers(text, [SpanStyle(bold=True)])  # 长度不符
    assert marked == text


def test_no_style_runs_returns_unchanged():
    text = "plain"
    styles = _styles_for(text, [])
    assert inject_style_markers(text, styles) == text


def test_extract_empty_input():
    assert extract_style_markers("") == ("", [])


def test_unknown_tags_kept_verbatim():
    # 恶意/被搅乱的标签不匹配精确语法 → 原样保留，不误删文本
    marked = "<X>text<X>"
    clean, styles = extract_style_markers(marked)
    assert clean == marked
