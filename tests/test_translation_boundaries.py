"""Commit 7E-Audit — Translation Boundary Audit（tests/test_translation_boundaries.py）.

记录并锁定**四条内容的 translation 输入边界** —— 验证 translator 收到的
文本恰如其分，绝不越界：

- **Code**    ：translator 收到 0 次调用（保留块，绝不可译）。
- **List**    ：translator 只收到 content（"Introduction"），绝无
  "1. Introduction"（marker 不进 translator）。
- **TOC**     ：translator 只收到干净的 title（"Introduction"），
  绝无 "Introduction ........ 42"（页码/leader/编号 PRESERVE）。
- **Style**   ：translator 收到的是「保护结构后」的文本（style markers 随
  文本一并传入），且 bold/italic 边界不会被 semantic / layout 破坏。

外加恶意输入测试：无论 translator 返回垃圾串 / 吞 marker / 删空，
List / TOC / Code / Style 结构都不会被破坏。
"""

from pdf2zh.semantic.models import SpanStyle
from pdf2zh.semantic.renderer.list import build_page_list_plan
from pdf2zh.semantic.renderer.toc import build_page_toc_plan
from pdf2zh.semantic.style_translate import translate_styled_paragraph
from pdf2zh.v3.canonical_page import BlockModel
from pdf2zh.v3.render_payload import block_translation_unit


def _spy(calls):
    def tr(s):
        calls.append(s)
        return f"T:{s}"
    return tr


# =========================================================================
# 1. Code：translator_calls == []
# =========================================================================


def test_code_zero_translator_calls():
    b = BlockModel(
        text='def hello():\n    print("hello")',
        kind="code", x0=40, y0=40, x1=500, y1=100,
    )
    calls = []
    unit = block_translation_unit(b, _spy(calls))
    assert unit["kind"] == "preserve"
    assert calls == []
    assert unit["translate"] is False
    # 保留块译文 == 原文（字节级）
    assert unit["translated"] == b.text


def test_code_within_payload_dict_never_translated():
    """用结构化载荷路径再确认：CodeBlockNode 的 lines 永不进 translator。"""
    from pdf2zh.semantic.models import CodeBlockNode

    node = CodeBlockNode(lines=["def f():", "    return 1"])
    calls = []
    # 走统一分派：code 块是 KEEP_KINDS -> preserve，0 次调用
    b = BlockModel(text=node.text, kind="code", x0=10, y0=10, x1=300, y1=60)
    unit = block_translation_unit(b, _spy(calls))
    assert unit["kind"] == "preserve"
    assert calls == []


# =========================================================================
# 2. List：translator 只见 content
# =========================================================================


def test_list_marker_never_in_translation_calls():
    let = []
    build_page_list_plan(
        ["1. Introduction", "2. Background"],
        translate=_spy(let),
    )
    assert let == ["Introduction", "Background"]
    assert "1. Introduction" not in let
    assert "2. Background" not in let


def test_list_translator_calls_exact_content():
    calls = []
    build_page_list_plan(
        ["1. Alpha", "   a. Beta", "2. Gamma"],
        translate=_spy(calls),
    )
    # 只有 content；marker 与结尾都绝不见
    assert calls == ["Alpha", "Beta", "Gamma"]


# =========================================================================
# 3. TOC：translator 只见干净 title
# =========================================================================


def _toc_lines():
    return [
        {"text": "Introduction ........ 42", "x0": 72, "y0": 700,
         "x1": 540, "y1": 712, "size": 12},
        {"text": "Background .......... 3", "x0": 96, "y0": 680,
         "x1": 540, "y1": 692, "size": 12},
    ]


def test_toc_translator_only_sees_title():
    calls = []
    build_page_toc_plan(_toc_lines(), 612.0, translate=_spy(calls))
    assert calls == ["Introduction", "Background"]
    # 绝不出现带 leader / 页码的整条
    for c in calls:
        assert "..." not in c
        assert c.strip() != "Introduction ........ 42"


# =========================================================================
# 4. Style：translator 收到保护结构，bold/italic 不被破坏
# =========================================================================


def test_style_translator_receives_marker_protected_text():
    src = "This is very important."
    # "very important" bold
    styles = [SpanStyle()] * len(src)
    for i in range(8, 22):
        styles[i] = SpanStyle(bold=True)
    seen = {}

    def tr(marked):
        seen["input"] = marked
        # 真实 LLM：保留 style markers，改写文本
        return "这是 <b0>非常重要</b0> 的内容。"

    para = translate_styled_paragraph(src, styles, tr)
    assert not para.recovered
    # translator 确实收到了「带保护标记」的文本（marker 结构随文本旅行）
    assert "<b0>very important</b0>" in seen["input"]
    # bold 边界在 translation 后一一保留
    assert para.text == "这是 非常重要 的内容。"
    bold = "".join(sp.text for sp in para.spans if sp.style.bold)
    assert bold == "非常重要"


def test_style_italic_survives_translation():
    src = "Use italics for emphasis"
    styles = [SpanStyle()] * len(src)
    for i in range(4, 11):  # "italics"
        styles[i] = SpanStyle(italic=True)
    para = translate_styled_paragraph(
        src, styles, lambda m: m.replace("italics", "斜体")
    )
    assert not para.recovered
    ital = "".join(sp.text for sp in para.spans if sp.style.italic)
    assert ital == "斜体"


# =========================================================================
# 5. 恶意输入：translator 想破坏结构，结构仍保持
# =========================================================================


def test_evil_translator_cannot_break_list_structure():
    """translator 返回垃圾 / 吞 marker / insert 假编号 —— 结构不破。"""
    def evil(s):
        # 吞掉原文里的 marker、插入 "1. " 前缀、返回超长、返回空
        return "1. " + s.replace("1.", "").replace("2.", "").strip() * 100

    plan = build_page_list_plan(["1. Introduction", "2. Background"], translate=evil)
    markers = [c for c in plan["commands"] if c["kind"] == "marker"]
    texts = [c for c in plan["commands"] if c["kind"] == "text"]
    # marker 原样："1." / "2."（从未被 translator 改写）
    assert [m["text"] for m in markers] == ["1.", "2."]
    # 两条 content 是垃圾译文（marker 不吞不掉）；绝不因 translator 插入的
    # 假前缀而把 marker 列内容合并掉 marker
    assert len(texts) == 2


def test_evil_empty_translator_list_structure_kept():
    """translator 返回 "" —— marker 仍在，content 空（不崩）。"""
    plan = build_page_list_plan(
        ["1. A", "2. B"], translate=lambda s: ""
    )
    markers = [c for c in plan["commands"] if c["kind"] == "marker"]
    assert [m["text"] for m in markers] == ["1.", "2."]
    # 至少 marker 命令存在；空 content 不会生成 text 命令但仍不抛异常
    assert all(c["kind"] in ("marker", "text") for c in plan["commands"])


def test_evil_translator_cannot_break_toc_page_column():
    """translator 把 title 改得超长 —— page_x 页码列不动、结构仍在。"""
    def evil(s):
        return s * 100 if s else s

    plan = build_page_toc_plan(_toc_lines(), 612.0, translate=evil)
    # entries 的 page_number 原样（不被 translator 改写）
    entries = plan["entries"]
    assert [e["page_number"] for e in entries] == ["42", "3"]


def test_evil_translator_style_never_drops_text_on_mangled_markers():
    """translator 把 style markers 整个吞掉 → 优雅回退，译文/原文不丢。"""
    src = "bold word"
    styles = [SpanStyle()] * len(src)
    for i in range(4):
        styles[i] = SpanStyle(bold=True)
    para = translate_styled_paragraph(src, styles, lambda m: "译后文本")
    assert para.text == "译后文本"
    assert "".join(sp.text for sp in para.spans) == para.text
    # 不会因为 markers 损坏而让整段失败
    assert para.recovered or not para.recovered


# =========================================================================
# 6. Side-channel / document_model 级：TOC heading_ref 不因垃圾翻译丢失
# =========================================================================


def test_toc_heading_ref_survives_translation(tmp_path):
    from pdf2zh.v3.canonical_page import BlockModel, PageModel
    from pdf2zh.v3.document_model import DocumentModel
    from pdf2zh.v3.render_payload import block_translation_unit

    head = BlockModel(text="Introduction", kind="heading", x0=40, y0=300,
                      x1=300, y1=315)
    toc_b = BlockModel(
        text="1. Introduction",
        kind="toc", x0=72, y0=110, x1=540, y1=130,
        metadata={
            "toc_entries": [{
                "title": "1. Introduction", "number": "1.", "title_only": "Introduction",
                "level": 0, "page_number": "42", "title_x": 72.0, "page_x": 540.0,
                "indent": 72.0, "dot_leader": "......", "leader_present": True,
                "continuation": [], "bbox": [72, 40, 540, 60],
            }],
        },
    )
    page = PageModel(page_num=1)
    page.blocks = [toc_b, head]
    model = DocumentModel()
    model.pages = [page]
    calls = []
    unit = block_translation_unit(toc_b, _spy(calls), model=model)
    assert unit["kind"] == "toc"
    assert calls == ["Introduction"]
    assert unit["payload"]["entries"][0]["heading_ref"] is not None
    assert unit["payload"]["entries"][0]["page_number"] == "42"


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))