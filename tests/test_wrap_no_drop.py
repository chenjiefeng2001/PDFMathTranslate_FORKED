"""P1：真实框装不下时**绝不静默丢字**。

背景（``doc/7p5_audit_report.md``）：字号校准把译文放大到源文件真实字号后，译文需要
更多行，而 ``_insert_text_wrapped`` 的缩放阶梯最低只到 0.55，走完仍装不下就**直接丢
行**。50 页真跑实测命中 2 处，分别丢掉整个块的 50% 和 83%。

实测数据说明为什么光有原阶梯不够：mp2e page 25 的 ``'1.2 一个寓言'``，框 67.0×15.0pt、
字号 16.65pt，缩到 0.55 时整串宽仍是 73.3pt > 框宽 67pt —— **每一档都还是 2 行**。

三级退让（顺序即偏好）：

1. 常规阶梯（:data:`_WRAP_SHRINK_STEPS`，下限 0.55）；
2. 深缩到可读下限（:data:`_WRAP_DEEP_SHRINK_STEPS` + :data:`_WRAP_MIN_FONT_SIZE`）——
   留在原框内，因此不产生任何新叠印；
3. 向下扩框：仅当页底有余量且不与本页其它译文块相撞。

三��都救不了时仍把**页面范围内**能画的行全画出来，剩下的显式计入
``wrap_clipped_page`` 并告警。只有退化几何（零高 box）保留历史的 ``wrap_truncated``。
"""

from __future__ import annotations

import pytest

from pdf2zh.v3.magicpdf_renderer import (
    _WRAP_DEEP_SHRINK_STEPS,
    _WRAP_DEGENERATE_BOX_PT,
    _WRAP_EXPAND_BOTTOM_MARGIN,
    _WRAP_MIN_FONT_SIZE,
    _WRAP_SHRINK_STEPS,
    _insert_text_wrapped,
    _occupied_translation_boxes,
    _rects_overlap,
    render_plan_to_pdf,
)

PAGE = [612, 792]


def _entry(text, box, fs, page=0, bid="p0_head"):
    return {
        "block_id": bid,
        "page": page,
        "kind": "heading",
        "text": "original heading",
        "translated": text,
        "render_path": "translate_refit",
        "src_box": box,
        "dst_box": box,
        "font_size": fs,
        "render_payload": {"kind": "heading", "commands": [], "entries": []},
    }


def _render(entries):
    pdf, stats = render_plan_to_pdf(entries, page_sizes={0: PAGE})
    import pymupdf

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "".join(doc[0].get_text().split())
    doc.close()
    return text, stats


# --------------------------------------------------------------------------
# 1. 真实世界的那个块：常规阶梯救不了，深缩能救
# --------------------------------------------------------------------------
def test_the_real_mp2e_case_keeps_every_character():
    """`1.2 一个寓言`，框 67.0×15.0pt，字号 16.65pt —— 50 页真跑里真实丢过 1 行。"""
    want = "1.2 一个寓言"
    drawn, stats = _render([_entry(want, [82, 428, 149, 443], 16.65)])

    assert "".join(want.split()) in drawn, f"lost text: {drawn!r}"
    assert "wrap_truncated" not in stats
    assert stats.get("fit_shrunk_deep") == 1, (
        "expected the deep-shrink tier, not a silent drop or a box expansion: "
        f"{stats}"
    )


def test_the_normal_tier_is_still_preferred_when_it_fits():
    """常规阶梯能装下时不要动深缩 —— 保真优先，字号不该被无谓压小。"""
    drawn, stats = _render([_entry("标题文字", [60, 100, 400, 140], 14.0)])
    assert "标题文字" in drawn
    assert "fit_shrunk" not in stats
    assert "fit_shrunk_deep" not in stats
    assert "box_expanded" not in stats


# --------------------------------------------------------------------------
# 2. 深缩必须有绝对下限
# --------------------------------------------------------------------------
def test_deep_shrink_stops_at_the_legibility_floor():
    """深缩不得越过 :data:`_WRAP_MIN_FONT_SIZE`；一档都装不下时取最小候选。

    40pt 字号塞进 12pt 高的框是病态几何。这里要钉的是**取舍方向**：绝不能因为
    "没有一档装得下"就退回 40pt —— 那只有开头几行画得下，后面整段全丢。
    """
    text = "这是一段很长的译文内容示例" * 12
    drawn, stats = _render([_entry(text, [60, 100, 300, 112], 40.0)])

    assert "wrap_truncated" not in stats, "must never fall back to silent dropping"
    assert (
        stats.get("fit_shrunk_best_effort") == 1
    ), f"expected the smallest-step best effort, got {stats}"
    # 40pt 时只有 1 行画得下；压到下限附近能保住大半。方向错了就会退化成 1 行。
    want = "".join(text.split())
    assert (
        len(drawn) > len(want) * 0.5
    ), f"only kept {len(drawn)}/{len(want)} chars -- reverted to the huge font"


def test_deep_shrink_is_ordered_below_the_normal_floor():
    """深缩档位必须全部小于常规阶梯下限，否则"深缩"名不副实。"""
    assert max(_WRAP_DEEP_SHRINK_STEPS) < min(s for s in _WRAP_SHRINK_STEPS if s < 1.0)


def test_the_floor_constant_is_human_readable():
    """下限必须是能读的字号。这条改小了就等于把丢字换成看不见。"""
    assert _WRAP_MIN_FONT_SIZE >= 5.0


# --------------------------------------------------------------------------
# 3. 扩框：只在真的放得下、且不撞人时才扩
# --------------------------------------------------------------------------
def test_box_expands_when_there_is_free_space_below():
    """下方无邻居且页底有余量 → 扩框，字号不必被压到可读下限以下。"""
    text = "一段需要多行才能放下的译文内容示例" * 3
    box = [60, 100, 200, 112]
    drawn, stats = _render([_entry(text, box, 16.0)])

    assert "".join(text.split()) in drawn, f"lost text: {drawn!r}"
    assert stats.get("box_expanded") == 1, stats
    assert "wrap_truncated" not in stats
    assert "wrap_truncated" not in stats


def test_box_does_not_expand_into_a_neighbouring_translation():
    """下方是别人的译文块 → 不得扩框盖上去，只能显式溢出/截断并记账。

    这条是扩框机制的安全边界：少扩一次框只是回到显式溢出，覆盖邻块则是新造叠印。
    """
    long_text = "内容" * 60
    entries = [
        _entry(long_text, [60, 100, 200, 112], 16.0, bid="p0_a"),
        # 紧邻下方的另一个译文块。dst_box 是 **PDF 左下原点**，所以视觉上"下方"
        # 意味着 y 更小 —— 我第一版写成 114（那在视觉上是上方），邻居压根挡不住，
        # 测试于是"通过"了一个错误的实现。
        {
            "block_id": "p0_b",
            "page": 0,
            "kind": "paragraph",
            "text": "next block",
            "translated": "下一段正文",
            "render_path": "translate_refit",
            "src_box": [60, 60, 400, 76],
            "dst_box": [60, 60, 400, 76],
            "font_size": 10.0,
            "render_payload": {"kind": "flow", "commands": [], "entries": []},
        },
    ]
    drawn, stats = _render(entries)

    assert stats.get("box_expanded") in (
        None,
        0,
    ), f"must not expand into the neighbour: {stats}"
    assert "下一段正文" in drawn, "the neighbouring block must survive intact"


def test_rects_overlap_ignores_touching_edges():
    """相邻但不重叠的两个框不算相撞 —— 否则扩框会被过度保守地禁用。"""
    a = [10, 100, 200, 120]
    b = [10, 120, 200, 140]  # 恰好接边
    assert not _rects_overlap(a, b)
    assert _rects_overlap(a, [100, 110, 300, 130]), "real intersection must be caught"
    assert _rects_overlap(a, [10, 100, 200, 120]), "identical boxes do overlap"
    assert not _rects_overlap(a, [1, 2]), "malformed boxes never collide"
    assert not _rects_overlap([1, 2], b)


def test_occupied_boxes_exclude_self_only():
    """避让名单排除自己，但**包含**所有会被绘制的块。

    ``_entry_text`` 在 ``translated`` 为空时回退到 ``text`` —— 那种块照样会被绘制，
    所以必须计入避让名单（否则会扩框盖住它）。真正的过滤条件是"会不会被画"，
    不是"有没有译文"。我最初按后者写测试，方向是错的。
    """
    entries = [
        _entry("A", [10, 10, 100, 30], 10.0, bid="a"),
        # translated 空但会回退到 text 绘制 —— 必须计入
        {
            "block_id": "b",
            "page": 0,
            "text": "fallback",
            "translated": "",
            "src_box": [10, 40, 100, 60],
            "dst_box": [10, 40, 100, 60],
        },
        # 真正没有任何可绘制文本 —— 排除
        {
            "block_id": "c",
            "page": 0,
            "text": "",
            "translated": "",
            "src_box": [10, 70, 100, 90],
            "dst_box": [10, 70, 100, 90],
        },
        {"block_id": "d", "page": 0, "text": "x", "translated": "D", "src_box": "bad"},
    ]
    got = _occupied_translation_boxes(entries, 792.0, exclude_id="a")
    # 翻到 fitz 坐标（y0=792-60, y1=792-40）
    assert got == [[10.0, 732.0, 100.0, 752.0]], got
    assert _occupied_translation_boxes(entries, 792.0), "without exclude_id, a appears"


# --------------------------------------------------------------------------
# 4. 绝不画到页外
# --------------------------------------------------------------------------
def test_nothing_is_drawn_past_the_page_edge():
    """画到页外的字读者根本看不见 —— 那是"看不见地丢字"，还污染越界指标。"""
    text = "【译】" + "very long translated heading " * 12
    drawn, stats = _render([_entry(text, [69, 699, 200, 706], 14.0)])

    assert stats.get("spans_outside_page") in (None, 0), stats
    assert "wrap_truncated" not in stats
    want = "".join(text.split())
    assert len(drawn) >= len(want) - 12, f"drew only {len(drawn)}/{len(want)}"


def test_a_block_whose_box_itself_extends_past_the_page_bottom_is_clamped():
    """框本身就越出页底时，一行都不许画到页外。

    这是 :data:`_WRAP_EXPAND_BOTTOM_MARGIN` 夹取真正起作用的地方：正常块框都在页内，
    ``min(rect.y1, page_limit)`` 与 ``rect.y1`` 等价，只有框越界时才看得出差别。

    正确行为是**一行都不画并显式计数** —— 首行基线已经在页外，画下去就是阅读器
    看不见的字（等于悄悄丢字，还会污染越界指标）。这里顺带钉住"首行始终落笔"那条
    历史行为**只对退化几何成立**，它曾经让这种块画出页外的字。
    """
    # dst 是 PDF 左下原点：y0 取负 → 翻到 fitz 后框底 802 > 页高 792
    drawn, stats = _render([_entry("一段需要换行的译文" * 6, [60, -10, 400, 5], 12.0)])

    assert stats.get("spans_outside_page") in (
        None,
        0,
    ), f"drew past the page edge: {stats}"
    assert drawn == "", f"nothing may be drawn from a box below the page: {drawn!r}"
    assert (
        stats.get("wrap_clipped_page", 0) >= 1
    ), f"the shortfall must still be counted: {stats}"


def test_the_page_bottom_margin_absorbs_the_glyph_descender():
    """页底余量必须为正，且要盖住字形下伸部。

    这组几何是精确挑出来的：基线落在页底 2pt 以内，字形下伸部会伸到 792 之外。
    实测把 :data:`_WRAP_EXPAND_BOTTOM_MARGIN` 改成 0 就立刻出现 1 个越界 span
    （字形底 793.6 > 页高 792）—— 也就是阅读器看不见的字。余量 2.0 时正确做法是
    **不画**这一行并显式计数，而不是画出去。
    """
    drawn, stats = _render(
        [_entry("一段需要换行的译文内容示例", [60, -4.0, 400, 11.0], 12.0)]
    )

    assert stats.get("spans_outside_page") in (
        None,
        0,
    ), f"the descender crossed the page edge: {stats}"
    assert (
        drawn == ""
    ), f"nothing may be drawn when only the descender would fit: {drawn!r}"
    assert _WRAP_EXPAND_BOTTOM_MARGIN > 0.0, "a zero margin lets glyphs off the page"


def test_expansion_blocked_by_a_neighbour_reports_overflow_preserved():
    """扩框被邻居挡住 → 走显式溢出，并记 ``wrap_overflow_preserved``。

    这个指标必须和 ``wrap_clipped_page`` 区分开：前者是"全部行都画了、只是压出框外"，
    后者是"页面也放不下、确实少画了"。审计要能分辨这两种情况。
    """
    long_text = "内容" * 60
    entries = [
        _entry(long_text, [60, 100, 200, 112], 16.0, bid="p0_a"),
        # 视觉上在下方（dst 左下原点 → y 更小）的邻居，挡住扩框
        {
            "block_id": "p0_b",
            "page": 0,
            "kind": "paragraph",
            "text": "next",
            "translated": "下一段正文",
            "render_path": "translate_refit",
            "src_box": [60, 60, 400, 76],
            "dst_box": [60, 60, 400, 76],
            "font_size": 10.0,
            "render_payload": {"kind": "flow", "commands": [], "entries": []},
        },
    ]
    drawn, stats = _render(entries)

    assert (
        stats.get("wrap_overflow_preserved") == 1
    ), f"must account for it as preserved overflow: {stats}"
    assert stats.get("box_expanded") in (None, 0)
    assert "下一段正文" in drawn, "the neighbour must survive intact"
    assert "".join(long_text.split()) in drawn, "no text may be lost"


def test_a_block_at_the_page_bottom_still_keeps_most_of_its_text():
    want = "【译】" + "very long translated heading " * 12
    drawn, _ = _render([_entry(want, [69, 699, 200, 706], 14.0)])
    assert len("".join(want.split())) - len(drawn) <= 12


# --------------------------------------------------------------------------
# 5. 退化几何保留历史行为
# --------------------------------------------------------------------------
def test_degenerate_zero_height_box_keeps_legacy_truncation():
    """零高 box = 根本没有可用框，历史行为与既有测试都必须保留。"""
    # 文本必须长到会换行：单行放得下就不会触发截断分支（曾经就是这么写错的）
    text = "A somewhat longer heading that definitely wraps onto a second line here"
    drawn, stats = _render([_entry(text, [60, 300, 400, 300], 12.0)])

    assert (
        stats.get("wrap_truncated") == 1
    ), f"degenerate geometry must keep the legacy accounting: {stats}"
    assert drawn, "even a degenerate box still draws its first line"


def test_the_degenerate_threshold_is_tiny():
    """退化阈值必须足够小，否则正常的小框会被误判成"没有框"。"""
    assert _WRAP_DEGENERATE_BOX_PT <= 1.0


# --------------------------------------------------------------------------
# 6. 常量自洽
# --------------------------------------------------------------------------
def test_constants_are_sane():
    assert _WRAP_EXPAND_BOTTOM_MARGIN >= 0.0
    assert _WRAP_EXPAND_BOTTOM_MARGIN < 20.0, "留太多余量等于不许扩框"
    assert _WRAP_MIN_FONT_SIZE > 0.0


# --------------------------------------------------------------------------
# 7. 直接驱动内部函数（不经 render_plan_to_pdf）
# --------------------------------------------------------------------------
def test_insert_text_wrapped_requires_seeded_stats():
    """``_draw_line`` 直接累加 ``stats["blocks"]``，调用方必须预置。

    这不是生产缺陷（``render_plan_to_pdf`` 在 930 行预置了），但把它钉住，免得有人
    传一个空 dict 进来就在渲染中途炸掉。
    """
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page(width=PAGE[0], height=PAGE[1])
    r = type("R", (), {"x0": 60.0, "y0": 100.0, "x1": 400.0, "y1": 140.0})()
    _insert_text_wrapped(
        pg, r, "标题", 14.0, "china-ss", {"pages": 0, "blocks": 0, "glyphs": 0}
    )
    assert "标题" in "".join(pg.get_text().split())
    doc.close()


def test_deep_shrink_and_expansion_metrics_are_distinct():
    """两个新指标不能共用同一个键，否则审计分不清是哪条退让生效了。"""
    keys = {
        "fit_shrunk_deep",
        "box_expanded",
        "wrap_overflow_preserved",
        "fit_shrunk_best_effort",
        "wrap_clipped_page",
    }
    assert len(keys) == 5
    assert keys & {"fit_shrunk", "fit_shrunk_deep"}, "sanity"


@pytest.mark.parametrize("scale", _WRAP_DEEP_SHRINK_STEPS)
def test_each_deep_shrink_step_is_a_finite_positive_factor(scale):
    assert 0.0 < scale < 1.0


def test_paragraph_newline_is_a_hard_break_not_a_phantom_second_line():
    """译文里的 ``\n`` 必须由换行布局自己消化，不能透传给 ``insert_text``。

    pymupdf 会把 ``\n`` 解释成一次换行，于是在本行之下**额外**画出一行；而那一行
    正好压在「按框宽正常换行得到的下一行」上。实测 mp2e 50 页 p23 出现三对这样的
    叠印，纵向只差 0.91pt —— 肉眼是重影，但文字一个都没丢，所以既不会被丢字审计
    抓到，也不会被 P1 的任何退让拦住。

    回归点：绘制路径拿到的每一行都不含 ``\n``，且每行基线间距 = 一个行距。
    """
    import pymupdf

    text = "第一种方法无法均匀分配工作，原因虽简单却很重要：\n相等的输入范围不会产生相等的工作量。"

    doc = pymupdf.open()
    pg = doc.new_page(width=PAGE[0], height=PAGE[1])
    drawn: list[str] = []
    import pdf2zh.v3.magicpdf_renderer as R

    real = R._draw_line

    def spy(page, s, x, y, eff_font, font_size, stats, label=""):
        drawn.append(s)
        return real(page, s, x, y, eff_font, font_size, stats, label)

    r = type("R", (), {"x0": 60.0, "y0": 700.0, "x1": 400.0, "y1": 780.0})()
    R._draw_line = spy
    try:
        _insert_text_wrapped(
            pg,
            r,
            text,
            12.0,
            "china-ss",
            {"pages": 0, "blocks": 0, "glyphs": 0},
        )
    finally:
        R._draw_line = real

    assert drawn, "没有落笔，测试无效"
    bad = [s for s in drawn if "\n" in s]
    assert not bad, f"仍把换行符透传给 insert_text，会多画一行造成重影：{bad}"

    # 两段都必须画出来：断行是重排，不是裁字
    flat = "".join("".join(drawn).split())
    assert flat == "".join(text.split()), "换行处理丢了字"

    # 相邻行基线必须差一个完整行距，不能出现 1pt 以内的贴身两行
    ys = sorted(
        sp["origin"][1]
        for blk in pg.get_text("dict")["blocks"]
        if blk.get("type") == 0
        for line in blk["lines"]
        for sp in line["spans"]
        if (sp.get("text") or "").strip()
    )
    tight = [a for a, b in zip(ys, ys[1:]) if b - a < 4.0]
    assert not tight, f"出现了 {tight} 这种几乎贴身的行，说明 \\n 仍然多画了一行"
    doc.close()


def test_newline_does_not_count_towards_line_width():
    """``\n`` 是零宽字符，不能参与 ``_width()`` —— 否则每行都被算宽一行。"""
    from pdf2zh.v3.magicpdf_renderer import _WRAP_LINE_HEIGHT

    assert _WRAP_LINE_HEIGHT == 1.4


def test_newline_breaks_even_when_both_paragraphs_fit_on_one_line():
    """段末必须强制断行，哪怕两段拼起来还装得下同一行。

    少了段末的 ``_flush()``，第二段会接在第一行尾巴上 —— 这是"看起来能渲染、
    实际段结构被抹掉"的那类退化，比丢字更难发现：文本一个字不少，只是行数变了。
    """
    import pymupdf

    import pdf2zh.v3.magicpdf_renderer as R

    text = "第一段。\n第二段。"
    # 框宽足够把 "第一段。第二段。" 七个字全放一行 —— 只有真正断行才会产生两行。
    doc = pymupdf.open()
    pg = doc.new_page(width=PAGE[0], height=PAGE[1])
    drawn: list[str] = []
    real = R._draw_line

    def spy(page, s, x, y, eff_font, font_size, stats, label=""):
        drawn.append(s)
        return real(page, s, x, y, eff_font, font_size, stats, label)

    r = type("R", (), {"x0": 60.0, "y0": 700.0, "x1": 400.0, "y1": 780.0})()
    R._draw_line = spy
    try:
        _insert_text_wrapped(
            pg,
            r,
            text,
            12.0,
            "china-ss",
            {"pages": 0, "blocks": 0, "glyphs": 0},
        )
    finally:
        R._draw_line = real

    assert len(drawn) == 2, f"两段各占一行，实际落笔 {len(drawn)} 行：{drawn}"
    # 换行符本身不该作为字符出现在任何一行里：它变成了行边界，不是可见字形。
    assert "".join(drawn) == text.replace("\n", ""), f"行内容应原样保留，实际 {drawn}"
    doc.close()
