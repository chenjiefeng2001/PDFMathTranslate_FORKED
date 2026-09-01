"""Commit 6A 测试：Visual TOC 检测 + 解析（纯数据，不翻译、不渲染）。

覆盖 ``pdf2zh.semantic.toc_detector`` + ``pdf2zh.semantic.toc_parser``：

- 单页 TOC / 多页 TOC；
- 编号条目 / 无编号条目；
- 嵌套层级（缩进推断，不只靠 1 / 1.1）；
- dot leader 与页码列；
- 多行条目（continuation 归属同一 TOCEntryNode）；
- TOC 后普通段落不并入条目；
- 负例：普通段落、数字结尾段、List、Heading、References 不误判；
- 打印页码绝不进入 title；
- ``page_number`` 与 ``destination_page`` 两个独立字段。
"""

from pdf2zh.semantic.models import TOCEntryNode
from pdf2zh.semantic.toc_detector import detect_anchors, detect_header, match_entry
from pdf2zh.semantic.toc_parser import parse_toc

PAGE_W = 612.0


def L(text, x0=72.0, x1=540.0, y0=700.0, size=12.0):
    """人造页面行：{text, x0, y0, x1, y1, size}。"""
    return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y0 + size, "size": size}


def parse(lines):
    return parse_toc([{**ln} for ln in lines], page_width=PAGE_W)


# ── 1. 单页 TOC ─────────────────────────────────────────────
def test_single_page_toc():
    lines = [
        L("Contents"),
        L("Introduction ............ 1"),
        L("Methods ................. 5"),
        L("Results ................. 9"),
    ]
    node = parse(lines)
    assert node is not None
    assert node.is_toc_page and node.has_header
    assert [e.title for e in node.entries] == ["Introduction", "Methods", "Results"]
    assert [e.page_number for e in node.entries] == ["1", "5", "9"]
    # 页码列在页面右侧（几何保留）
    assert all(e.page_x > 0.7 * PAGE_W for e in node.entries)


# ── 2. 多页 TOC ─────────────────────────────────────────────
def test_multipage_toc():
    page5 = parse(
        [
            L("Contents"),
            L("Chapter One ........... 1"),
            L("Chapter Two ........... 23"),
        ]
    )
    page6 = parse(
        [
            L("Chapter Two ........... 23"),
            L("Chapter Three ......... 45"),
        ]
    )
    assert page5 is not None and len(page5.entries) == 2
    assert page6 is not None and len(page6.entries) == 2
    # 第二页（续页）无需 header，仅靠 2+ 条目即为 TOC
    assert page6.has_header is None or page6.has_header is False
    assert page6.is_toc_page
    assert page6.entries[0].title == "Chapter Two"


# ── 3. 编号条目（标题保留编号前缀） ──────────────────────────
def test_numbered_entries():
    lines = [
        L("1 Introduction ........ 1"),
        L("2 Method .............. 5"),
        L("3 Results ............. 9"),
    ]
    node = parse(lines)
    assert node is not None
    assert [e.title for e in node.entries] == [
        "1 Introduction",
        "2 Method",
        "3 Results",
    ]
    # 编号与页码都从原始文本提取，互不混淆
    assert [e.page_number for e in node.entries] == ["1", "5", "9"]


# ── 4. 无编号条目 ───────────────────────────────────────────
def test_unnumbered_entries():
    node = parse(
        [
            L("Introduction .......... 1"),
            L("Background ............ 3"),
            L("Motivation ............ 4"),
        ]
    )
    assert node is not None
    assert [e.title for e in node.entries] == [
        "Introduction",
        "Background",
        "Motivation",
    ]
    assert all(e.leader_present for e in node.entries)


# ── 5. 嵌套层级（缩进推断，不只靠编号） ──────────────────────
def test_nested_levels_from_indentation():
    lines = [
        L("1 Introduction ........ 1"),
        L("2 Method .............. 5"),
        L("2.1 Dataset ........... 6", x0=96.0),
        L("2.2 Training .......... 7", x0=96.0),
        L("3 Results ............. 9"),
    ]
    node = parse(lines)
    assert node is not None
    levels = [e.level for e in node.entries]
    # 缩进簇 {72,96}：深层条目 level 更高
    assert levels[:2] == [0, 0]
    assert levels[2:4] == [1, 1]
    assert levels[4] == 0
    # 同缩进簇（方法项）level 一致，绝不假分层
    assert node.entries[1].level == node.entries[0].level


def test_dotted_number_depth_when_indentation_uniform():
    # 缩进完全一致（单簇），仅靠编号 1 / 1.1 / 1.1.1 的深度作为 level（次级信号）
    node = parse(
        [
            L("1 Introduction ......... 1"),
            L("1.1 Background .......... 3"),
            L("1.1.1 Motivation ........ 4"),
        ]
    )
    assert node is not None
    assert [e.level for e in node.entries] == [0, 1, 2]


# ── 6. dot leader ───────────────────────────────────────────
def test_dot_leader_captured():
    node = parse([L("Alpha ......... 3"), L("Beta .......... 4")])
    assert node is not None
    for e in node.entries:
        assert e.leader_present
        assert e.dot_leader  # 引导线原始保留


# ── 7. 页码列（右侧空列，无点线） ───────────────────────────
def test_empty_column_page_number():
    # 无点线、右侧空列页码：“Conclusion 42” → 页码列在右缘即锚
    node = parse([L("Contents"), L("Conclusion         42")])
    assert node is not None and len(node.entries) == 1
    assert node.entries[0].title == "Conclusion"
    assert node.entries[0].page_number == "42"
    assert node.entries[0].page_x > 0.7 * PAGE_W


# ── 8. 多行条目 ─────────────────────────────────────────────
def test_multiline_entry_single_node():
    lines = [
        L("Contents"),
        L("1. A very long table of contents entry that", x0=72.0),
        L("   continues on the next line ......... 12", x0=96.0),
    ]
    node = parse(lines)
    assert node is not None
    assert len(node.entries) == 1
    e = node.entries[0]
    assert (
        e.title
        == "1. A very long table of contents entry that continues on the next line"
    )
    assert e.page_number == "12"
    assert e.title.endswith("continues on the next line")


# ── 9. TOC 後普通段落不并入条目 ─────────────────────────────
def test_paragraph_after_toc_not_absorbed():
    node = parse(
        [
            L("Contents"),
            L("Intro ......... 1"),
            L("More .......... 2"),
            L("This trailing plain paragraph is not part of the TOC."),
        ]
    )
    assert node is not None
    assert [e.title for e in node.entries] == ["Intro", "More"]
    assert "trailing plain paragraph" not in " ".join(e.title for e in node.entries)


# ── 负例 ────────────────────────────────────────────────────
def test_heading_or_plain_numbered_not_toc():
    # 普通段落中出现 “1 Introduction” → 无 leader/页码 → 不构成 TOC
    assert parse([L("1 Introduction")]) is None
    assert parse([L("1 Introduction"), L("A normal paragraph follows.")]) is None
    # 数字结尾的普通段落 → 非右侧、无 leader → 不是条目
    assert match_entry(L("The model improves accuracy by 5"), PAGE_W) is None


def test_list_not_toc():
    assert parse([L("1. Alpha"), L("2. Beta"), L("3. Gamma")]) is None
    assert parse([L("bulleted one"), L("bulleted two")]) is None


def test_references_not_toc():
    # 单条 References 带页码：既无 header 也无 2+ 条目 → 不判为 TOC
    assert parse([L("References ............ 45")]) is None
    # References 标题 + 普通段落（无条目形态）
    assert parse([L("References"), L("This is the references body text.")]) is None


def test_header_plus_plain_ending_number_not_toc():
    # 有 Contents 标题，但随后的文字只是以普通词/数结尾的正文段（非 leader、非右侧页码列）
    lines = [L("Contents"), L("The accuracy improved to 99 percent.")]
    node = parse(lines)
    assert node is None  # 无任何 TOC 条目形态 → 不判为目录页


# ── 12. 打印页码绝不进入 title ──────────────────────────────
def test_page_number_never_in_title():
    node = parse([L("Alpha ......... 3"), L("Beta .......... 4")])
    for e in node.entries:
        assert e.page_number in ("3", "4")
        assert not e.title.endswith(e.page_number)
    # title 是剥离 leader+page 后的干净文本
    assert node.entries[0].title == "Alpha"


# ── page_number 与 destination_page 独立字段 ────────────────
def test_page_number_and_destination_independent():
    e = TOCEntryNode(title="Intro", page_number="3", destination_page=41)
    assert e.page_number == "3"
    assert e.destination_page == 41
    assert e.page_number != e.destination_page
    # JSON 快照保留两者（不丢失各自语义）
    d = e.to_dict()
    assert d["page_number"] == "3"
    assert d["destination_page"] == 41


# ── --debug-toc：真实 PDF → debug JSON（自包含、JSON-safe） ───
def test_dump_toc_debug_schema(tmp_path):
    import json
    import pymupdf

    pdf_path = tmp_path / "mini.pdf"
    doc = pymupdf.Document()
    page = doc.new_page(width=612.0, height=792.0)
    y = 100.0
    for t in (
        "Contents",
        "Introduction .............. 1",
        "Methods ................... 5",
    ):
        page.insert_text((72.0, y), t, fontsize=12.0)
        y += 20.0
    doc.save(str(pdf_path))
    doc.close()

    from pdf2zh.semantic.toc_debug import dump_toc_debug

    out = tmp_path / "out"
    payload = dump_toc_debug(str(pdf_path), str(out))
    assert payload["pages"], "期望检测到至少一页 TOC"
    p1 = payload["pages"]["1"]
    assert p1["is_toc_page"] is True
    titles = [e["title"] for e in p1["entries"]]
    assert "Introduction" in titles and "Methods" in titles
    # 每一条目都携带平面标量字段（自包含）
    for e in p1["entries"]:
        for key in ("title", "level", "page_number", "indent", "title_x", "page_x"):
            assert key in e
    # JSON-safe：roundtrip 保持一致
    assert json.loads(json.dumps(payload)) == payload
    # 落盘
    assert (out / "toc.json").exists()


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__]))
