"""P2：LaTeX 主导的块必须判成公式，不能冒充标题。

因果链（``doc/7p2_locate_formula.py`` 可在真实 mp2e 上复现）：

1. MinerU 把行间公式塞进某个 **span 的 content**（block 级 ``text`` 里没有 —— 两者不一致，
   只扫 block text 会漏掉这一层）；
2. 该 span 的框高 16.0pt，明显高于同页正文的 9~11pt；
3. 字号校准把它如实映射成 15.95pt（**这一步是对的**，公式本来就比正文高）；
4. ``apply_layout_splits`` 把这一行单独拆成块；
5. ``StructureClassifier`` 见字号远大于正文 → 判成 ``heading``。

于是公式冒充标题被送去翻译，再被当成标题塞回 16pt 的框里：P1 只能把它缩到 5.58pt
才放下。根因在第 5 步，不在渲染。P2 在 ``annotate_roles`` 里加一道守卫。

守卫的判据被真实样本逼着换过两次，两次都写进了注释 —— 三个指标各自都会误判：

- **有无反斜杠**：正文里行内上标 ``M ^ { \\prime }`` 也有反斜杠，会把整段正文判成
  公式、整段不翻译。
- **标记之间的跨度**：某正文块里有**两处** ``\\prime``（一前一后），"首末标记之间的
  区域"横跨整段 174 字符散文，占比算成 0.78 → 整段正文被判成公式。
- **字符密度**：``M ^ { \\prime }`` 只有 7 个非空白字符，纯靠密度必然高（0.29）→ 误判。

最终用「**带花括号参数的 LaTeX 命令**（结构）+ **公式区字符密度**（密度）」两者同时成立。
"""

from __future__ import annotations

import pytest

from pdf2zh.v3.document_model import (
    _LATEX_BRACED,
    _LATEX_DOMINANCE,
    _LATEX_FORMULA_CHARS,
    _LATEX_STRUCTURE,
    _is_latex_dominated,
    annotate_roles,
)

BS = "\\"

#: 真实样本：mp2e page 36 的公式块（实测 0.45 密度）。
REAL_FORMULA_BLOCK = (
    "on N, the number of cores used. The dependency is"
    + BS
    + "begin{array} { r } { C a c h e M i s s = "
    + BS
    + "frac { N ^ { "
    + BS
    + "bullet } } { N + 1 0 } } "
    + BS
    + "end{array}. Profiling"
)

#: 真实样本：同页正文夹两处行内上标（实测 0.03 密度）。整段必须继续当正文。
REAL_PROSE_WITH_INLINE = (
    "Suppose M accounts for 40% of the program's execution time. You hire a "
    "programmer to replace M withM ^ { "
    + BS
    + "prime }, the parallel replacement for M, has a four-fold speedup. What frac-"
    + BS
    + "n tion of the overall execution time must M account for if replacing it withM ^ { "
    + BS
    + "prime }"
    + BS
    + "doubles the program's speedup?"
)


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------
def test_the_real_formula_block_is_detected():
    assert _is_latex_dominated(REAL_FORMULA_BLOCK) is True


def test_the_real_prose_with_inline_math_is_not_a_formula():
    """这条最重要：误判会把整段正文变成保留块、**完全不翻译**。"""
    assert _is_latex_dominated(REAL_PROSE_WITH_INLINE) is False


def test_a_sole_inline_superscript_is_not_a_formula():
    """单处 ``M ^ { \\prime }`` 只有 7 个字符，密度天然很高 —— 靠密度判必然误判。"""
    text = (
        "You hire a programmer to replace M withM ^ { " + BS + "prime }, and ship it."
    )
    assert _is_latex_dominated(text) is False


@pytest.mark.parametrize(
    "text",
    [
        REAL_FORMULA_BLOCK,
        BS + "begin{equation}E = mc^2" + BS + "end{equation}",
        BS + "frac{a}{b}",
        BS + "sqrt{x + y}",
    ],
)
def test_genuine_formulas_are_detected(text):
    assert _is_latex_dominated(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1.2 A Fable",
        "Exercise 1.7. You are given a program that includes a method M.",
        "Hint: The prisoners need not all do the same thing.",
        REAL_PROSE_WITH_INLINE,
        # 常见转义不是 LaTeX 命令：曾经被 `\\[a-zA-Z]+` 误当成公式命令
        "first line" + BS + "n" + "second line",
        "tab" + BS + "t" + "here",
        "path C:" + BS + "Users" + BS + "test",
    ],
)
def test_prose_is_not_detected_as_formula(text):
    assert _is_latex_dominated(text) is False


def test_two_small_inline_fractions_separated_by_prose_stay_prose():
    """两处小行内分数被大段散文隔开 → 仍是正文。

    这条专门钉住**密度**这一半守卫。它和「结构」那一半互补：结构信号
    （``\\frac{``）在这里是满足的，只有密度能把这一块判回正文。反过来
    （:func:`test_the_real_formula_block_is_detected`）是密度满足、结构也满足。

    两次都是必要的 —— 去掉任一条，另一条测试就会红。
    """
    text = (
        "The ratio is given by "
        + BS
        + "frac{a}{b} in the paper, and we reuse that convention throughout. "
        "The complementary ratio is "
        + BS
        + "frac{c}{d}, which appears in every later chapter of the book."
    )
    assert (
        _LATEX_BRACED.search(text) is not None
    ), "precondition: the structure half fires"
    assert (
        _is_latex_dominated(text) is False
    ), "two tiny fractions inside a paragraph must not make the whole block a formula"


def test_plain_backslash_sequences_never_match_the_structure_regex():
    """``\\n`` / ``\\t`` 这类转义绝不能进结构白名单。

    一旦进去，"公式区域"会从一个 ``\\n`` 跨到块尾，整段正文被吞成公式。
    """
    for esc in ("n", "t", "r", "f", "v", "a", "b", "0", "s", "w", "d"):
        assert _LATEX_STRUCTURE.search(BS + esc + " rest") is None, esc


def test_the_threshold_leaves_room_between_the_two_real_samples():
    """门槛必须落在两个真实样本之间 —— 否则就是照着结果凑数。"""

    def density(text):
        marks = list(_LATEX_STRUCTURE.finditer(text))
        start = text.rfind(BS, 0, marks[0].start() + 1)
        end = max(marks[-1].end(), text.rfind("}") + 1)
        region = text[start:end]
        dense = sum(1 for c in region if not c.isspace())
        return sum(1 for c in region if _LATEX_FORMULA_CHARS.match(c)) / dense

    assert density(REAL_FORMULA_BLOCK) > _LATEX_DOMINANCE
    assert density(REAL_PROSE_WITH_INLINE) < _LATEX_DOMINANCE


def test_the_braced_command_requirement_is_what_separates_the_two():
    """``\\frac{a}{b}`` 有参数 → 公式；``^ { \\prime }`` 没有参数 → 不是。"""
    assert _LATEX_BRACED.search(BS + "frac{a}{b}") is not None
    assert _LATEX_BRACED.search("M ^ { " + BS + "prime }") is None


# --------------------------------------------------------------------------
# 守卫接进 annotate_roles
# --------------------------------------------------------------------------
class _Block:
    def __init__(self, text, font_size=9.96):
        self.kind = "paragraph"
        self.text = text
        self.font_size = font_size
        self.bbox = (100.0, 100.0, 400.0, 120.0)
        self.lines = []
        self.metadata = {}


class _Page:
    def __init__(self, blocks):
        self.blocks = blocks
        self.page_num = 0


def test_annotate_roles_marks_a_latex_block_as_formula_not_heading():
    """公式块的字号远大于正文（这正是它被误判成 heading 的原因）。"""
    blk = _Block(REAL_FORMULA_BLOCK, font_size=15.95)
    _Page([blk])  # 明确构造意图
    hits = annotate_roles(_Page([blk]))
    assert hits >= 1
    assert blk.kind == "formula", f"got {blk.kind}"
    assert blk.metadata["role"] == "formula"
    assert blk.metadata["latex"] == blk.text
    assert blk.metadata["latex_source"] == "mineru_span_content"
    assert blk.metadata["role_confidence"] == 1.0


def test_annotate_roles_leaves_inline_math_prose_alone():
    blk = _Block(REAL_PROSE_WITH_INLINE, font_size=9.96)
    annotate_roles(_Page([blk]))
    assert blk.kind != "formula", f"prose was swallowed: {blk.kind}"
    assert "latex" not in blk.metadata


def test_the_guard_does_not_need_a_high_confidence_to_apply():
    """守卫是硬判定，不受 StructureClassifier 的 0.65 置信度门槛影响。"""
    blk = _Block(BS + "frac{a}{b}", font_size=9.96)
    annotate_roles(_Page([blk]))
    assert blk.kind == "formula"


def test_the_guard_skips_blocks_that_are_not_paragraphs():
    """已经定型的块（formula/code/table…）不该被守卫改写。"""
    for kind in ("code", "formula", "table"):
        blk = _Block(REAL_FORMULA_BLOCK, font_size=15.95)
        blk.kind = kind
        annotate_roles(_Page([blk]))
        assert blk.kind == kind, f"{kind} was overwritten"


def test_the_guard_tolerates_empty_blocks():
    blk = _Block("", font_size=0.0)
    assert annotate_roles(_Page([blk])) == 0
    assert blk.kind == "paragraph"
