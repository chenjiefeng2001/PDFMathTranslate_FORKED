"""Commit 5 集成测试：样式保持翻译（SpanStyle → translation → styled spans）。

覆盖 ``pdf2zh.semantic.style_translate`` 的整条链：
``inject_style_markers → translator → extract_style_markers → collapse``：

- bold / italic / bold+italic 在翻译后落到正确的 span 边界；
- 跨 span 翻译（翻译器重排/重组文本）样式随文本移动，边界不丢失；
- 标记损坏（吞掉 </bK>）→ 优雅回退为纯文本，绝不让整段失败；
- 标记被整体吞掉 → 译文仍完整保留；
- 恒等翻译器 → 恢复原始字体样式（字体恢复）；
- 无样式 / 样式错位 / 空输入 → 直接翻译原文。

验收目标（来自计划）：source bold run → 译后 bold span 一一对应，
``"".join(spans) == text``。
"""

from pdf2zh.semantic.models import SpanStyle
from pdf2zh.semantic.style_translate import (
    StyledParagraph,
    collapse_styled_spans,
    translate_styled_paragraph,
)


def _styles_for(text, runs):
    """runs = [(start_end, SpanStyle), ...] → per-char style list."""
    out = [SpanStyle()] * len(text)
    for (s, e), st in runs:
        for i in range(s, e):
            out[i] = st
    return out


def _join(para):
    return "".join(sp.text for sp in para.spans)


# ── 单个样式：bold / italic ──────────────────────────────────
def test_bold_survives_translation():
    src = "This is very important for us."
    styles = _styles_for(src, [((8, 22), SpanStyle(bold=True))])  # "very important"
    para = translate_styled_paragraph(
        src, styles, lambda m: m.replace("very important", "非常重要")
    )
    assert not para.recovered
    assert para.text == "This is 非常重要 for us."
    assert _join(para) == para.text
    bold = "".join(sp.text for sp in para.spans if sp.style.bold)
    assert bold == "非常重要"
    # 边界：前后 span 无样式，中间 span 粗体
    styles_run = [sp.style for sp in para.spans]
    assert SpanStyle(bold=True) in styles_run


def test_italic_survives_translation():
    src = "Italics are used here"
    styles = _styles_for(src, [((0, 7), SpanStyle(italic=True))])  # "Italics"
    para = translate_styled_paragraph(
        src, styles, lambda m: m.replace("Italics", "斜体")
    )
    assert not para.recovered
    assert "斜体" in para.text
    ital = "".join(sp.text for sp in para.spans if sp.style.italic)
    assert ital == "斜体"
    assert _join(para) == para.text


def test_bold_and_italic_combined_nested():
    src = "word"
    styles = _styles_for(src, [((0, 4), SpanStyle(bold=True, italic=True))])
    para = translate_styled_paragraph(src, styles, lambda m: m.replace("word", "词语"))
    assert not para.recovered
    assert para.text == "词语"
    assert len(para.spans) == 1
    assert para.spans[0].text == "词语"
    assert para.spans[0].style == SpanStyle(bold=True, italic=True)


def test_chinese_bold_translation_boundary():
    """计划的示例：``This is **very important** …`` → ``这是 **非常重要** …``。"""
    src = "This is very important for the model."
    styles = _styles_for(src, [((8, 22), SpanStyle(bold=True))])
    # 模拟真实 LLM：保留 <bN> 包裹、词序不变的中文译文
    para = translate_styled_paragraph(
        src, styles, lambda m: "这是 <b0>非常重要</b0> 的模型。"
    )
    assert not para.recovered
    assert para.text == "这是 非常重要 的模型。"
    assert _join(para) == para.text
    bold = "".join(sp.text for sp in para.spans if sp.style.bold)
    assert bold == "非常重要"


# ── 跨 span 翻译：样式随文本移动 / 重排 ──────────────────────
def test_cross_span_reorder_preserves_style():
    src = "A bold then plain"
    styles = _styles_for(src, [((2, 6), SpanStyle(bold=True))])  # "bold"

    def tr(marked):
        # 翻译器把实际内容重排：加粗部分移到句首，marker 随文本移动
        assert "<b0>bold</b0>" in marked
        return "<b0>加粗</b0> 然后是普通"

    para = translate_styled_paragraph(src, styles, tr)
    assert not para.recovered
    assert para.text == "加粗 然后是普通"
    assert _join(para) == para.text
    assert "".join(sp.text for sp in para.spans if sp.style.bold) == "加粗"


def test_cross_span_split_text_boundaries_kept():
    src = "normal bold1 bold2 normal"
    # bold 覆盖 "bold1 bold2" 两个词，翻译后仍是同一加粗区（同样式相邻 run 合并）
    styles = _styles_for(src, [((7, 18), SpanStyle(bold=True))])
    para = translate_styled_paragraph(
        src, styles, lambda m: m.replace("bold1 bold2", "加粗内容")
    )
    assert not para.recovered
    assert para.text == "normal 加粗内容 normal"
    assert "".join(sp.text for sp in para.spans if sp.style.bold) == "加粗内容"


# ── marker 损坏 fallback ─────────────────────────────────────
def test_dropped_closing_tag_falls_back_unstyled():
    src = "A very bold tail"
    styles = _styles_for(src, [((7, 12), SpanStyle(bold=True))])

    def tr(marked):
        assert "<b0>" in marked
        return marked.replace("</b0>", "")  # 模型吞掉关闭标记 → 不平衡

    para = translate_styled_paragraph(src, styles, tr)
    assert para.recovered
    assert para.text == "A very bold tail"  # 残余 <b0> 被剥离，译文完整
    assert _join(para) == para.text
    assert all(not sp.style.styled for sp in para.spans)  # 回退为纯文本


def test_marker_fully_dropped_keeps_translation():
    src = "bold word"
    styles = _styles_for(src, [((0, 4), SpanStyle(bold=True))])
    para = translate_styled_paragraph(src, styles, lambda m: "译文文本")
    assert para.text == "译文文本"
    assert _join(para) == para.text
    assert all(not sp.style.styled for sp in para.spans)


# ── 字体恢复 / 恒等翻译器 ────────────────────────────────────
def test_identity_translator_recovers_original_style():
    src = "This has italic emphasis"
    styles = _styles_for(src, [((9, 15), SpanStyle(italic=True))])  # "italic"
    para = translate_styled_paragraph(src, styles, lambda m: m)
    assert not para.recovered
    assert para.text == src
    assert _join(para) == src
    assert "".join(sp.text for sp in para.spans if sp.style.italic) == "italic"


# ── 无样式 / 错位 / 空输入 → 直接翻译原文 ────────────────────
def test_plain_paragraph_no_style_no_markers():
    src = "just text"
    styles = _styles_for(src, [])
    calls: list = []
    para = translate_styled_paragraph(
        src, styles, lambda s: (calls.append(s) or "原文")
    )
    assert para.text == "原文" and not para.recovered
    assert calls == ["just text"]  # 无样式 → 不进标记注入，直接翻译原文


def test_misaligned_styles_plain_translation():
    para = translate_styled_paragraph(
        "abc", [SpanStyle(bold=True)], lambda s: s.upper()
    )
    assert para.text == "ABC" and not para.recovered


def test_empty_input_returns_empty():
    para = translate_styled_paragraph("", [], lambda s: s)
    assert para.text == "" and para.spans == []


# ── collapse 与 styled_text 辅助 ─────────────────────────────
def test_collapse_merges_adjacent_same_style():
    spans = collapse_styled_spans(
        "aXb", [SpanStyle(), SpanStyle(bold=True), SpanStyle()]
    )
    assert [s.style for s in spans] == [SpanStyle(), SpanStyle(bold=True), SpanStyle()]
    assert "".join(s.text for s in spans) == "aXb"
    merged = collapse_styled_spans("abb", [SpanStyle(bold=True)] * 3)
    assert len(merged) == 1 and merged[0].text == "abb"


def test_collapse_misaligned_returns_empty():
    assert collapse_styled_spans("ab", [SpanStyle(bold=True)]) == []


def test_styled_text_extracts_style_payload():
    src = "A bold and plain"
    styles = _styles_for(src, [((2, 6), SpanStyle(bold=True))])  # "bold"
    para = translate_styled_paragraph(src, styles, lambda s: s)
    assert para.styled_text(SpanStyle(bold=True)) == "bold"
    assert para.styled_text(SpanStyle(italic=True)) == ""


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__]))
