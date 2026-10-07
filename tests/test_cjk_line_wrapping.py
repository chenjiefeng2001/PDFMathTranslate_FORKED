"""中文译文的换行：不能再按空格切词（doc/7p0 P1）。

7P0 实测（``doc/7p0_real_load_report.md`` 缺陷 R2）：块 ``p38_2`` 的 244 字中文
译文在成品里只剩 99 字（41%），尾部 7 个 6-gram 命中 0，日志报
``145 of 244 characters dropped``。

根因不是框不够高，而是**换行器不会在中文中间断**：

    译文 244 字，其中空格 0 个
    text.split(" ")          -> 1 个 token
    该 token 宽 244 x 7.65 = 1867pt，框宽仅 338pt（5.5 倍）
    -> 只能排成一行 -> _draw_line 按页宽裁掉 145 字

也就是说 ``_insert_text_wrapped`` 的换行对中文**完全失效**，只对以空格分词的
语言有效。而中文译文恰好一个空格都没有 —— 这是最坏的一类输入撞上了只处理最好
一类的算法。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from pdf2zh.v3.magicpdf_renderer import _insert_text_wrapped, _wrap_tokens

#: 实测 p38_2 的译文，**从真实运行产物里读**，不手抄。
#:
#: 手抄会掩盖长度与标点差异，而这次缺陷恰恰与「0 个空格」「244 字」「含中文
#: 句号」直接相关 —— 一旦抄错，这组测试就在验证另一个字符串。证据缺失时
#: :func:`_measured_cjk_244` 直接 skip，而不是拿近似文本冒充真实输入。
_EVIDENCE = (
    Path(__file__).resolve().parent.parent / "doc" / "7p0-load50" / "render_plan.json"
)


def _measured_cjk_244() -> str:
    if not _EVIDENCE.is_file():  # pragma: no cover
        pytest.skip("7p0 render_plan evidence not present")
    for b in json.loads(_EVIDENCE.read_text(encoding="utf-8")):
        if b.get("block_id") == "p38_2":
            return b["translated"]
    pytest.skip("p38_2 not found in evidence")


#: 无空格纯中文的最小复现（不依赖证据文件，保证本组测试始终有覆盖）
CJK_NOSPACE = "互斥可能是多处理器编程中最常见的协调形式。" * 12


#: 真实页面宽度（实测 ``_page_width`` 返回 595.0）。**必须给 FakePage 提供**：
#: 取不到页宽时 :func:`_page_width` 返回 0.0，而 :func:`_draw_line` 在页宽为 0 时
#: 走「不做页面内收敛」分支，于是无论框多窄都只排一行 —— 那会让换行测试假通过。
PAGE_W = 595.0
PAGE_H = 842.0


class FakePage:
    """记录落笔的假 page，避免真的开 PDF。"""

    def __init__(self) -> None:
        self.drawn: list[tuple[float, str, float | None]] = []
        self.rect = pymupdf.Rect(0.0, 0.0, PAGE_W, PAGE_H)

    def insert_text(self, pt, text, fontsize=None, fontname=None):
        self.drawn.append((pt[1], text, fontsize))

    @property
    def text(self) -> str:
        return "".join(t for _, t, _ in self.drawn)


def _render(box, text, font_size=7.65, font="china-ss"):
    pg = FakePage()
    stats = {"blocks": 0, "glyphs": 0}
    _insert_text_wrapped(pg, pymupdf.Rect(*box), text, font_size, font, stats)
    return pg, stats


def _squash(s):
    """去掉所有纯排版空白，只留下真正要出现在页面上的字符。

    空格在断行处会被丢弃、``\\n`` 被换行布局消化成行边界 —— 两者都不算内容丢失。
    比逐个 case 写豁免更可靠：任何**字形**字符少一个，这里就会炸。
    """
    return "".join(ch for ch in (s or "") if not ch.isspace())


# --------------------------------------------------------------------------
# 切词本身
# --------------------------------------------------------------------------
def test_space_free_cjk_is_not_one_giant_token():
    """整段无空格中文必须被逐字切开，而不是变成一个超宽 token。"""
    toks = _wrap_tokens(CJK_NOSPACE)
    assert len(toks) == len(CJK_NOSPACE), (
        "space-free CJK must be split per character; one token means the "
        "wrapper can only ever emit a single over-wide line"
    )
    assert "".join(toks) == CJK_NOSPACE


def test_latin_words_are_still_kept_whole():
    """不能为了修中文把英文单词也逐字拆开 —— 那会毁掉英文的换行质量。"""
    toks = _wrap_tokens("the quick brown fox jumps")
    assert toks == ["the", " ", "quick", " ", "brown", " ", "fox", " ", "jumps"]


def test_mixed_cjk_and_latin_keeps_latin_words_whole():
    toks = _wrap_tokens("mutex 互斥锁 spinlock")
    assert "mutex" in toks and "spinlock" in toks
    assert "互" in toks and "斥" in toks and "锁" in toks


@pytest.mark.parametrize(
    "s",
    [
        "hello world",
        "锁 lock 锁",
        "a",
        "",
        "   ",
        "中",
        "the quick brown fox jumps over the lazy dog",
        "互斥可能是最常见的协调形式。",
        "混合 mixed 文本 text 结束。",
        "带数字 123 和符号 ***",
    ],
)
def test_tokenisation_preserves_the_whole_string(s):
    """切词必须是**无损**的：拼回去要一字不差。

    丢字发生在切词层比发生在换行层更难查 —— 换行层至少有 ``fit_clipped`` 计数，
    而切词丢字会一路静默到成品。
    """
    assert "".join(_wrap_tokens(s)) == s


def test_word_boundaries_survive_wrapping():
    """词边界不能在换行时被吃掉。

    断行处那个空格是被**换行本身取代**的，所以整串拼回去必然少一个空格 ——
    断言整串相等是错的（实测 ``'alpha beta gamma delta'`` + ``'epsilon zeta'``，
    视觉上完全正确）。真正要保证的是：词与词之间不会粘在一起，且行内空格仍在。
    若实现顺手 ``replace(" ", "")`` 或 ``rstrip`` 过头，行内空格会一起消失，
    英文就变成 ``thedeltaepsilon``。
    """
    text = "alpha beta gamma delta epsilon zeta"
    pg, _ = _render([100.0, 100.0, 220.0, 700.0], text, font_size=10.0, font="helv")
    assert len(pg.drawn) > 1, "must actually wrap for this to mean anything"

    words_drawn: list[str] = []
    for _, line, _ in pg.drawn:
        words_drawn.extend(line.split())
    assert words_drawn == text.split(), f"words merged or lost: {words_drawn}"

    for _, line, _ in pg.drawn:
        assert (
            line == line.rstrip()
        ), f"the break-point space must not be drawn at the line end: {line!r}"
        assert "  " not in line, f"double space inside a line: {line!r}"


def test_latin_is_measured_with_real_font_metrics_not_one_em_per_char():
    """拉丁字符必须按**真实字体宽度**度量，不能一律按 1em 估。

    ``china-ss`` 对拉丁字符的 advance 约 1em，而 helv 下窄得多（``i``/``l``
    约 0.28em）。混排行若按 1em 估，行会被判得过宽而过早换行 —— 表现为
    「英文行莫名变短、行数偏多」。这条断言用一个窄字符密集的样本把差异放大：
    按 1em 估与按 helv 估的行数不同。
    """
    narrow = "iiiiilillll" * 12  # 全是 helv 下很窄的字符
    box = [100.0, 100.0, 200.0, 800.0]
    pg, _ = _render(box, narrow, font_size=9.0, font="helv")
    real_w = pymupdf.get_text_length("i" * 44, fontname="helv", fontsize=9.0)
    assert pg.drawn, "nothing drawn"
    first = pg.drawn[0][1]
    first_w = pymupdf.get_text_length(first, fontname="helv", fontsize=9.0)
    # 若按 1em/字 收敛，行会被塞到 ~44 字（9pt 下 44*9=396pt > 100pt，不可能），
    # 所以真正的判据是：行内字数应远多于「按 1em 估」的上限
    per_char_em_estimate = 100.0 / 9.0
    assert len(first) > per_char_em_estimate, (
        f"only {len(first)} chars per line ({first_w:.1f}pt wide) -- looks like "
        f"the 1em-per-char estimate is being used for latin text"
    )
    assert real_w > 0


def test_no_line_exceeds_the_box_width():
    """每一行的绘制宽度都必须在框内 —— 这是「不丢字」的充分条件。

    逐字符切分的意义就在于让这件事恒成立；一旦某行超出，剩下的字符就会被
    :func:`_draw_line` 的页宽裁剪悄悄吃掉。用**真实字体度量**而不是字符数近似：
    helv 下的拉丁字符宽度并不相等。
    """
    cases = [
        ("A" * 300, 100.0, 200.0, 9.0, "helv"),
        ("互斥可能是最常见的协调形式。" * 10, 119.0, 457.0, 7.65, "china-ss"),
        ("alpha beta gamma delta " * 8, 100.0, 220.0, 10.0, "helv"),
    ]
    for text, x0, x1, fs, font in cases:
        pg, _ = _render([x0, 100.0, x1, 800.0], text, font_size=fs, font=font)
        assert pg.drawn, "nothing drawn"
        for _, line, d_fs in pg.drawn:
            w = pymupdf.get_text_length(line, fontname=font, fontsize=d_fs or fs)
            assert w <= (x1 - x0) + 1.5, (
                f"line of width {w:.1f}pt exceeds the {x1 - x0:.1f}pt box: "
                f"{line[:40]!r}"
            )


# --------------------------------------------------------------------------
# 端到端：不能丢字
# --------------------------------------------------------------------------
def test_the_measured_block_no_longer_loses_characters():
    """p38_2 的真实译文 + 真实几何：必须一字不少地落笔。

    修复前该块只剩 99/244 字，尾部 7 个 6-gram 命中 0。
    """
    text = _measured_cjk_244()
    box = [119.0, 335.0, 457.0, 476.0]
    assert len(text) == 244, "the evidence changed; re-derive the expectation"

    pg, stats = _render(box, text)

    assert pg.text == text, (
        f"lost {len(text) - len(pg.text)} of 244 characters "
        f"(tail: {text[len(pg.text):][:20]!r})"
    )
    assert stats.get("fit_clipped", 0) == 0


def test_dense_cjk_acknowledgement_block_keeps_every_character():
    """实测 p17_2：1072 字、20 行、框 189pt 高。

    它有换行丢失，但丢的**全部是行尾空格**（65/65，见全量核对），不是内容。
    这条断言要求内容一字不少。
    """
    box = [118.0, 152.0, 456.0, 341.0]
    text = (
        "我们还要感谢许多发勘误表帮助改进本书的人员，包括：Matthew Allen、"
        "Karolos Antoniadis、Liran Barsisa、Cristin Berman、Konstantin Boudnik、"
        "Bjoern Branden Cackett、Mario Calha、Michael Champigny、Neil Cohen、"
        "Daniel B. Curtis、Gil Danziger、Venkatesh Gopalakrishnan Wan Fokkink、"
        "David Fort、Robert P. Goddard、Bart Golsteijn、K. Gopinath、"
        "Jason T. Green、Tim Halloran、Muhammad Amber Hassaan、Matt Hools、"
        "Ben Horowitz、Barak Itkin、Paulo Jané Jeon、Irena Karlinsky、"
        "Ahmed Khademzadeh、Khan、Namhyung Kim、Guy Korland、Sergey Kotov、"
        "Andrew Lawrence、Adam MacBeth、Mike Maloney、Tim Meldrum、"
        "Marek Melderis、Bartosz Milewski、Jose Pedro Oliveira、Parson、"
        "Jonathan Perry、Amir Pnueli、Pat Quinn、Raghunathan、Binoy Ravindran、"
        "Roei Raviv、Michael Rueppel、Mohamed M. Saad、Assaf Schwarz、"
        "Nathar Shah、Huang-Ti Shih、Joseph James Stout、Mark Summerfield、"
        "Deqing Sun、Chong Xing、Jaeheon Yi 和 Ruiwen Zuo。"
    )
    pg, _ = _render(box, text)
    assert pg.text.replace(" ", "") == text.replace(
        " ", ""
    ), f"lost {len(text) - len(pg.text)} characters of real content"


def test_no_risk_block_loses_real_content_across_the_measured_run():
    """全量核对：418 个「CJK 变宽」块里，内容丢失必须是 0。

    用 ``doc/7p0-load50/render_plan.json``（真实运行的产物）驱动。
    """
    if not _EVIDENCE.is_file():  # pragma: no cover - 证据缺失时跳过而非假装通过
        pytest.skip("7p0 render_plan evidence not present")
    blocks = json.loads(_EVIDENCE.read_text(encoding="utf-8"))

    def cjk(s):
        return sum(1 for c in (s or "") if ord(c) > 0x2E80)

    risky = [
        b
        for b in blocks
        if b.get("render_path") in ("translate_refit", "shift_down")
        and (b.get("translated") or "").strip()
        and cjk(b["translated"]) > cjk(b.get("text") or "")
    ]
    assert risky, "the evidence set should contain CJK-widened blocks"

    lossy = []
    for b in risky:
        text = b["translated"]
        pg, _ = _render(b["src_box"], text, b.get("font_size") or 9.0)
        # 行尾空格在换行处被丢弃是排版常态，不算内容丢失。``\n`` 同理：它由换行布局
        # 自己消化成行边界（以前是原样透传给 ``insert_text``，结果多画一行压在下一行
        # 上，见 test_wrap_no_drop 里那个 0.91pt 重影的用例），提取文本里自然没有
        # 这个字符。实测 418 个风险块里 38 个含 ``\n``，去掉换行符后与提取结果逐字相等。
        if _squash(pg.text) != _squash(text):
            lossy.append((b["block_id"], len(text) - len(pg.text)))

    assert not lossy, f"blocks losing real translated content: {lossy[:10]}"


# --------------------------------------------------------------------------
# 不能引入回归
# --------------------------------------------------------------------------
def test_latin_paragraph_still_wraps_and_is_not_regressed():
    pg, _ = _render(
        [100.0, 100.0, 400.0, 300.0],
        "the quick brown fox jumps over the lazy dog " * 3,
        font_size=10.0,
        font="helv",
    )
    assert len(pg.drawn) > 1, "latin text must still wrap into multiple lines"
    assert pg.text.replace(" ", "") == ("thequickbrownfoxjumpsoverthelazydog" * 3)


def test_empty_text_draws_nothing():
    pg, stats = _render([100.0, 100.0, 400.0, 300.0], "")
    assert pg.drawn == []
    assert stats["glyphs"] == 0


def test_single_character_text_is_drawn():
    pg, _ = _render([100.0, 100.0, 400.0, 300.0], "锁")
    assert pg.text == "锁"


def test_long_unbreakable_token_is_split_not_dropped():
    """超长 URL / 公式无空格，必须被字符级切开而不是整段丢弃。

    这里框**足够高**（400pt），所以唯一的失败模式就是「不换行 + 按页宽裁剪」。
    旧行为实测：单行 149/300 字，日志 ``151 of 300 characters dropped``。
    """
    pg, stats = _render(
        [100.0, 100.0, 200.0, 700.0], "A" * 300, font_size=9.0, font="helv"
    )
    assert len(pg.drawn) > 1, (
        f"300 chars in a 100pt-wide box must wrap; got {len(pg.drawn)} line(s) "
        f"holding {len(pg.text)} chars"
    )
    assert pg.text == "A" * 300, f"dropped {300 - len(pg.text)} characters"
    assert (
        stats.get("fit_clipped", 0) == 0
    ), "clipping must not be needed once the token can be split per character"


def test_cjk_punctuation_does_not_leave_a_line_starting_with_it():
    """避头尾：行首不应出现收尾标点。"""
    text = "测试" * 40 + "。"
    pg, _ = _render([100.0, 100.0, 160.0, 400.0], text, font_size=9.0)
    starts_bad = [t[0] for _, t, _ in pg.drawn[1:] if t and t[0] in "，。、；：？！"]
    assert not starts_bad, f"lines starting with closing punctuation: {starts_bad}"
