"""单元测试：列表检测 + 嵌套列表解析（Phase 3 第一版）。

覆盖 ``pdf2zh.semantic.list_detector`` 与 ``pdf2zh.semantic.list_parser``：
- 标记识别（decimal / alpha / roman / bullet / 私用区字形 bullet）；
- 证据融合计分（marker 不是必要条件；连续性/几何加分）；
- 连续序号（1.→2.）得分提升；
- 嵌套列表（1. → a. → i.）与延续行；
- debug JSON 快照结构。
"""

from pdf2zh.semantic.list_detector import (
    ListCandidate,
    detect_list_candidates,
    list_debug_dict,
    markers_are_sequential,
    match_marker,
)
from pdf2zh.semantic.list_parser import parse_list_tree
from pdf2zh.semantic.models import ListNode


# ── 标记识别 ────────────────────────────────────────────────
def test_match_decimal():
    assert match_marker("1. Introduction") == ("1.", "decimal", "Introduction")
    assert match_marker("10) Results") == ("10)", "decimal", "Results")
    assert match_marker("1、引言") == ("1、", "decimal", "引言")


def test_match_alpha():
    assert match_marker("a. Background") == ("a.", "lower_alpha", "Background")
    assert match_marker("(b) Motivation") == ("(b)", "lower_alpha", "Motivation")


def test_match_roman_and_bullet():
    assert match_marker("ii. Training") == ("ii.", "lower_roman", "Training")
    assert match_marker("• item") == ("•", "bullet", "item")
    assert match_marker("\uf0b7 item") == ("\uf0b7", "bullet", "item")  # PDF 私用区字形


def test_no_marker():
    assert match_marker("This is a plain paragraph.") is None
    assert match_marker("123456") is None


# ── 负样本：编号小节 / 十进制数（不得误判为列表标记）────────────
def test_numbered_section_not_marker():
    assert match_marker("3.1 Method") is None
    assert match_marker("1.5 × 10⁻³") is None
    assert match_marker("2. Related Work") is not None  # 纯编号仍可能是列表项


def test_figure_caption_not_marker():
    assert match_marker("Figure 1. xxx") is None


# ── 序号连续性 ──────────────────────────────────────────────
def test_sequential_decimal():
    assert markers_are_sequential("1.", "decimal", "2.", "decimal")
    assert not markers_are_sequential("1.", "decimal", "3.", "decimal")


def test_sequential_alpha():
    assert markers_are_sequential("a.", "lower_alpha", "b.", "lower_alpha")
    assert not markers_are_sequential("(a)", "lower_alpha", "(c)", "lower_alpha")


def test_sequential_bullet_any():
    assert markers_are_sequential("•", "bullet", "•", "bullet")


def test_sequential_roman():
    assert markers_are_sequential("i.", "lower_roman", "ii.", "lower_roman")
    assert markers_are_sequential("ii.", "lower_roman", "iii.", "lower_roman")


# ── 证据融合检测 ────────────────────────────────────────────
def test_detect_flat_decimal_list():
    paras = ["1. First item", "2. Second item", "3. Third item"]
    cands = detect_list_candidates(paras)
    assert all(c is not None for c in cands)
    assert [c.marker for c in cands] == ["1.", "2.", "3."]
    # 连续性 + previous + same_indent → 高置信
    assert cands[1].score >= 6.0
    assert "next_marker_sequential" in cands[1].reasons
    assert "previous_is_list_item" in cands[1].reasons


def test_plain_paragraphs_not_candidates():
    paras = ["Some prose paragraph with no markers.", "Another ordinary paragraph."]
    cands = detect_list_candidates(paras)
    assert all(c is None for c in cands)


def test_single_ordered_marker_without_context_is_section_title():
    # 单条有序 marker 且无任何上下文 → 疑似章节标题（"1. Introduction"），不判为列表项
    cands = detect_list_candidates(["1. Introduction"])
    assert cands[0] is None
    # 有邻居上下文（列表项）时正常识别
    cands2 = detect_list_candidates(["1. Introduction", "2. Related Work"])
    assert cands2[0] is not None and cands2[1] is not None


def test_single_bullet_without_context_is_candidate():
    # bullet 豁免：单独一个 • 仍是列表项
    cands = detect_list_candidates(["• A single bullet item"])
    assert cands[0] is not None
    assert cands[0].marker_type == "bullet"


def test_bullet_list_with_private_use_glyph():
    paras = ["\uf0b7 alpha", "\uf0b7 beta", "\uf0b7 gamma"]
    cands = detect_list_candidates(paras)
    assert all(c is not None and c.marker_type == "bullet" for c in cands)
    assert cands[1].score >= 6.0


def test_geometry_signals_boost_score():
    paras = ["1. one", "2. two"]
    geom = [
        {"x0": 50.0, "x1": 500.0, "size": 10.0},
        {"x0": 50.0, "x1": 502.0, "size": 10.0},
    ]
    cands = detect_list_candidates(paras, geom=geom)
    assert cands[1] is not None
    assert "same_indent" in cands[1].reasons
    assert "same_line_height" in cands[1].reasons


# ── 嵌套解析 ────────────────────────────────────────────────
def test_parse_flat_list():
    paras = ["1. First", "2. Second"]
    tree = parse_list_tree(paras)
    assert tree is not None
    assert len(tree.items) == 2
    assert tree.items[0].content == "First"
    assert tree.items[1].content == "Second"
    assert tree.items[0].level == 0


def test_parse_nested_list():
    paras = ["1. Intro", "   a. Background", "   b. Motivation", "2. Method"]
    tree = parse_list_tree(paras)
    assert tree is not None
    assert len(tree.items) == 2
    # item 1 下挂一个嵌套列表，含两个子 item
    assert len(tree.items[0].children) == 1
    child = tree.items[0].children[0]
    assert isinstance(child, ListNode)
    assert len(child.items) == 2
    assert child.items[0].marker_type == "lower_alpha"
    assert child.items[0].level == 1


def test_parse_continuation_lines():
    paras = ["1. First item", "    continuation text", "2. Second item"]
    tree = parse_list_tree(paras)
    assert tree is not None
    assert len(tree.items) == 2
    assert tree.items[0].continuation == ["continuation text"]
    # 延续行不产生独立 item
    assert tree.items[1].content == "Second item"


def test_parse_three_level_nested():
    paras = ["1. a", "   a. b", "      i. c", "2. d"]
    tree = parse_list_tree(paras)
    assert tree is not None
    assert len(tree.items) == 2
    lvl1 = tree.items[0].children[0]
    assert isinstance(lvl1, ListNode) and len(lvl1.items) == 1
    lvl2 = lvl1.items[0].children[0]
    assert isinstance(lvl2, ListNode) and len(lvl2.items) == 1
    assert lvl2.items[0].marker == "i."


def test_parse_no_list_returns_none():
    assert parse_list_tree(["just prose", "more prose"]) is None


# ── debug JSON ──────────────────────────────────────────────
def test_list_debug_dict_structure():
    paras = ["1. one", "2. two", "plain paragraph"]
    d = list_debug_dict(paras)
    assert len(d["paragraphs"]) == 3
    assert d["candidates"][0] is not None
    assert d["candidates"][0]["marker"] == "1."
    assert d["candidates"][2] is None
    assert d["tree"] is not None
    assert len(d["tree"]["items"]) == 2
