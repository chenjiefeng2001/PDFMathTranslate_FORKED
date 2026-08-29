"""列表 fixture 测试集（Commit 4 验收：结构 / 负样本）。

从 ``tests/fixtures/lists/`` 读取 A–I 用例并验证：

- A：1./2./3. 平铺列表
- B：(a)(b)(c) 平铺列表
- C：• • • 平铺列表
- D：嵌套列表（1. → a./b.）
- E：长条目 + 延续行
- F：列表被普通段落打断（普通段不得吞成延续行）
- G：列表后的普通段落（不得吞成延续行）
- H：图注（Figure 1.）不得误判
- I：编号章节标题（1. Introduction）不得误判
- 负样本：2. Related Work / 3.1 Method / 1.5 × 10⁻³
"""

from pathlib import Path

from pdf2zh.semantic.list_detector import detect_list_candidates
from pdf2zh.semantic.list_parser import parse_list_tree
from pdf2zh.semantic.models import ListNode

_FIXTURES = Path(__file__).parent / "fixtures" / "lists"


def _paragraphs(name: str) -> list[str]:
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip()]


def _tree(name: str):
    paras = _paragraphs(name)
    return parse_list_tree(paras)


# ── 正样本 ─────────────────────────────────────────────────
def test_fixture_A_flat_decimal():
    tree = _tree("A_decimal_flat.txt")
    assert tree is not None
    assert len(tree.items) == 3
    assert [i.marker for i in tree.items] == ["1.", "2.", "3."]
    assert all(i.level == 0 for i in tree.items)


def test_fixture_B_alpha_parens():
    tree = _tree("B_alpha_parens.txt")
    assert tree is not None
    assert len(tree.items) == 3
    assert [i.marker for i in tree.items] == ["(a)", "(b)", "(c)"]


def test_fixture_C_bullet_flat():
    tree = _tree("C_bullet_flat.txt")
    assert tree is not None
    assert len(tree.items) == 3
    assert [i.marker_type for i in tree.items] == ["bullet"] * 3


def test_fixture_D_nested():
    tree = _tree("D_nested.txt")
    assert tree is not None
    assert len(tree.items) == 2
    child = tree.items[0].children[0]
    assert isinstance(child, ListNode)
    assert [i.marker for i in child.items] == ["a.", "b."]
    assert child.level == 1


def test_fixture_E_continuation():
    tree = _tree("E_continuation.txt")
    assert tree is not None
    assert len(tree.items) == 1
    item = tree.items[0]
    assert len(item.continuation) == 2
    # 延续行几何：continuation 渲染 x == content_x（验收标准）
    assert item.continuation[0] == "continuation line one"
    assert item.content_x == item.marker_x + item.marker_width


def test_fixture_F_interrupted_list():
    tree = _tree("F_list_interrupted.txt")
    assert tree is not None
    assert len(tree.items) == 2  # 普通段不得新增/吞并条目
    all_cont = [ln for it in tree.items for ln in it.continuation]
    assert "Normal paragraph." not in all_cont


def test_fixture_G_list_then_paragraph():
    tree = _tree("G_list_then_paragraph.txt")
    assert tree is not None
    assert len(tree.items) == 2
    all_cont = [ln for it in tree.items for ln in it.continuation]
    assert all_cont == []  # 列表后的普通段绝不成为延续行


# ── 负样本 ─────────────────────────────────────────────────
def test_fixture_H_figure_caption_not_list():
    assert _tree("H_figure_caption.txt") is None


def test_fixture_I_section_title_not_list():
    assert _tree("I_section_title.txt") is None


def test_neg_numbered_sections():
    paras = _paragraphs("neg_numbered_sections.txt")
    cands = detect_list_candidates(paras)
    # "2. Related Work" 单条无上下文 → 章节标题；3.1 / 1.5×10⁻³ 不匹配标记
    assert all(c is None for c in cands)
    assert parse_list_tree(paras) is None