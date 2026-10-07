"""页级纵向重排（reflow）：译文排得比源框高时，下推后继块而不是让它压上去。

机制
----
每个块都按源框独立排版，源框之间的纵向关系被原样继承；但译文常比源文长（中文尤其），
于是一块排得比源框高就会伸进下一块的地盘。实测 mp2e page 15：``p15_7`` 占
521.5~584、``p15_0`` 占 531.2~583，两块严重交错。

本模块按顶边从上到下扫描，维护"当前最低可用下沿"；某块顶边越过了它就整块下移，
并让位移**级联**给后续所有块。

实测效果（``doc/7p4_reflow_verify.py``，真实 50 页计划，其余环节全不变）::

    reflow OFF   overlap pages=16  outside=0
    reflow ON    overlap pages= 3  outside=0   shifted=118 blocks / 25 pages

写这些测试时踩了三个坑，全部写进了对应断言的注释里：比错边、取反的页底夹取、
以及过小的位移上限 —— 三者都会让函数"看起来在跑、实际一个块都没推"。
"""

from __future__ import annotations

import pytest

from pdf2zh.v3.magicpdf_renderer import (
    _REFLOW_BOTTOM_MARGIN,
    _REFLOW_MAX_SHIFT,
    _REFLOW_MIN_GAP,
    _entry_extent,
    _reflow_page_entries,
    _shift_entry_down,
)

PAGE_H = 666.0


def _flow(bid, top, n_lines, fs, page=0):
    """构造一个 flow 块：命令 y 从 ``top`` 起，每行步长 ``fs * 1.4``（向下）。"""
    step = fs * 1.4
    cmds = [
        {
            "kind": "flow-text",
            "text": f"line{i}",
            "x": 80.0,
            "y": top - i * step,
            "line": i,
            "font_size": fs,
        }
        for i in range(n_lines)
    ]
    return {
        "block_id": bid,
        "page": page,
        "kind": "paragraph",
        "text": "src",
        "translated": "译文",
        "render_path": "translate_refit",
        "src_box": [80.0, top - 40, 500.0, top],
        "dst_box": [80.0, top - 40, 500.0, top],
        "font_size": fs,
        "render_payload": {"kind": "flow", "commands": cmds, "lines": ["x"] * n_lines},
    }


def _tops(out):
    return [
        [c["y"] for c in (e.get("render_payload") or {}).get("commands") or []]
        for e in out
    ]


# --------------------------------------------------------------------------
# 1. 基本行为
# --------------------------------------------------------------------------
def test_a_block_that_clears_the_previous_one_is_not_moved():
    a = _flow("a", 600.0, 2, 10.0)  # 占 576..600
    b = _flow("b", 560.0, 2, 10.0)  # 占 536..560，整块在 a 之下
    stats = {}
    out = _reflow_page_entries([a, b], PAGE_H, stats)

    assert out[0] is a and out[1] is b, "没有碰撞就不该动"
    assert stats.get("reflow_shifted_blocks") in (None, 0)


def test_a_block_intruding_into_the_previous_one_is_pushed_down():
    """核心行为：顶边越过了前块下沿就下推。"""
    a = _flow("a", 600.0, 4, 10.0)  # 占 548..600
    b = _flow("b", 598.0, 2, 10.0)  # 顶边 598 落在 a 的内部 → 撞
    stats = {}
    out = _reflow_page_entries([a, b], PAGE_H, stats)

    assert stats.get("reflow_shifted_blocks") == 1
    b_top = out[1]["render_payload"]["commands"][0]["y"]
    a_bottom = _entry_extent(out[0])[1]
    assert (
        b_top <= a_bottom - _REFLOW_MIN_GAP
    ), f"pushed to {b_top}, but the previous block reaches down to {a_bottom}"


def test_the_shift_cascades_to_everything_below():
    """推一块必须连带推它下面的全部块，否则下沿不变、碰撞照旧。

    夹具刻意排成 a/b/c/d 逐个咬住前一个的下沿：b 让开 a 之后，c 就撞上了 b 的新位置，
    以此类推 —— 只有"位移级联"才能一次全清。
    """
    a = _flow("a", 600.0, 4, 10.0)  # bottom 548
    b = _flow("b", 598.0, 2, 10.0)  # 撞 a
    c = _flow("c", 530.0, 2, 10.0)  # 撞 b 移完之后的下沿
    d = _flow("d", 520.0, 2, 10.0)  # 撞 c 移完之后的下沿
    stats = {}
    out = _reflow_page_entries([a, b, c, d], PAGE_H, stats)

    assert stats.get("reflow_shifted_blocks") == 3, stats
    extents = sorted((_entry_extent(e) for e in out), key=lambda t: -t[0])
    for (_, prev_bottom), (top, _) in zip(extents, extents[1:]):
        assert (
            top <= prev_bottom - _REFLOW_MIN_GAP + 1e-6
        ), f"block at {top} still overlaps the one reaching down to {prev_bottom}"


def test_the_push_is_exactly_enough():
    """不多推。推多了会在页面上留下可见的空隙。"""
    a = _flow("a", 600.0, 4, 10.0)
    b = _flow("b", 598.0, 2, 10.0)
    out = _reflow_page_entries([a, b], PAGE_H, {})
    want = _entry_extent(out[0])[1] - _REFLOW_MIN_GAP
    got = out[1]["render_payload"]["commands"][0]["y"]
    assert got == pytest.approx(want, abs=0.01), f"want {want}, got {got}"


# --------------------------------------------------------------------------
# 2. 安全边界
# --------------------------------------------------------------------------
def test_nothing_is_pushed_past_the_page_bottom():
    """下沿不得低于页底余量 —— v3 是 y 向上，页底在 y≈0。

    这里曾经把 floor 写成 ``page_height - margin``（≈664，也就是**页顶**），于是夹取
    条件对几乎所有块恒成立、delta 被算成负数、整轮一个块都没推。
    """
    a = _flow("a", 600.0, 4, 10.0)
    b = _flow("b", 598.0, 8, 10.0)
    out = _reflow_page_entries([a, b], PAGE_H, {})
    for e in out:
        bottom = _entry_extent(e)[1]
        assert (
            bottom >= _REFLOW_BOTTOM_MARGIN - 1e-6
        ), f"{e['block_id']} pushed to y={bottom}, past the page bottom"


def test_a_runaway_shift_is_capped_and_counted():
    """病态块不得把整页推出页面；撞上限必须**显式计数**，否则是看不见的失败。"""
    a = _flow("a", 600.0, 40, 10.0)  # 极深，占到 600-556=44
    b = _flow("b", 598.0, 2, 10.0)
    stats = {}
    out = _reflow_page_entries([a, b], PAGE_H, stats)

    assert out[1].get("reflow_shift_pt") == pytest.approx(_REFLOW_MAX_SHIFT, abs=0.01)
    assert (
        stats.get("reflow_shift_clamped") == 1
    ), f"a clamped push must be counted, not silently accepted: {stats}"


def test_the_limit_keeps_the_lower_of_the_two_when_a_push_is_clamped():
    """回归：位移被上限截断时，``limit`` 必须取**两者更低**的那个。

    这条钉的是 ``min(limit, new_bottom)``。写成只跟 ``new_bottom`` 时，一块被截断
    没能推到位，它那较高的下沿会把后续块的判据抬高 —— 真正压到页面深处的块反而被
    判成"不撞"。

    注意这里**不能**断言"完全让开"：a 高达 556pt，b 光是让开它就需要 555pt，已超过
    120pt 的上限，此时 b/c 注定仍与 a 重叠 —— 那正是上限的用途。可判别的是
    **c 仍在被尽力下推**（撞满上限），而不是"以为没事、只推了 7pt"。
    """
    a = _flow("a", 600.0, 40, 10.0)  # 极深，bottom = 44
    b = _flow("b", 598.0, 2, 10.0)  # 需要推 555pt -> 撞上限
    c = _flow("c", 460.0, 2, 10.0)  # 判据若跟着 b 的高位走，就只会推 7pt
    stats: dict = {}
    out = _reflow_page_entries([a, b, c], PAGE_H, stats)

    assert out[1]["reflow_shift_pt"] == pytest.approx(_REFLOW_MAX_SHIFT, abs=0.01)
    assert out[2]["reflow_shift_pt"] == pytest.approx(_REFLOW_MAX_SHIFT, abs=0.01), (
        "c stopped being pushed -- the limit followed the clamped block's higher "
        "bottom instead of a's real one"
    )
    assert stats.get("reflow_shift_clamped", 0) >= 2, stats


def test_a_push_that_would_leave_the_page_is_clamped_to_the_bottom_margin():
    """回归：页底夹取。块本身贴着页底时，再推就越界了。

    需要真的构造出"推下去就越界"的几何：a 在页面很下方，b 紧随其后且更高 —— b 让开 a
    之后会掉到 y<0。夹取必须把它按在页底余量上，而不是任其推出页面。
    """
    a = _flow("a", 120.0, 4, 10.0)  # bottom ≈ 68
    b = _flow("b", 118.0, 8, 10.0)  # 让开 a 后 bottom 会变成负数
    stats = {}
    out = _reflow_page_entries([a, b], PAGE_H, stats)

    for e in out:
        bottom = _entry_extent(e)[1]
        assert (
            bottom >= _REFLOW_BOTTOM_MARGIN - 1e-6
        ), f"{e['block_id']} ended at y={bottom}, off the page"
    assert stats.get("reflow_shifted_blocks") == 1, stats


def test_the_cap_is_big_enough_for_real_cases():
    """上限要够真实场景用。

    实测 mp2e page 15 的 ``p15_0`` 需要下推 **62.48pt** 才能让开 ``p15_7``。上限取 60
    会让它"推了但没推够" —— 叠印没解决，账面上却像是处理过了。
    """
    assert _REFLOW_MAX_SHIFT >= 63.0


# --------------------------------------------------------------------------
# 3. 不变量
# --------------------------------------------------------------------------
def test_the_input_is_not_mutated():
    """返回新列表/新字典，不改调用方的原始 plan —— 诊断脚本要能拿原样再跑对照。

    列表本身也要查：``out = entries``（而非 ``list(entries)``）再 ``out[i] = ...``
    同样不会改动条目的字典，却会把调用方那个列表就地改掉 —— 单元级断言只看字典会漏。
    """
    a = _flow("a", 600.0, 4, 10.0)
    b = _flow("b", 598.0, 2, 10.0)
    entries = [a, b]
    before = [list(e["render_payload"]["commands"][0].items()) for e in entries]
    _reflow_page_entries(entries, PAGE_H, {})
    after = [list(e["render_payload"]["commands"][0].items()) for e in entries]
    assert before == after, "reflow mutated the input entries"
    assert entries == [a, b], "reflow mutated the caller's list in place"


def test_horizontal_geometry_is_untouched():
    a = _flow("a", 600.0, 4, 10.0)
    b = _flow("b", 598.0, 2, 10.0)
    out = _reflow_page_entries([a, b], PAGE_H, {})
    for e in out:
        assert e["dst_box"][0] == 80.0 and e["dst_box"][2] == 500.0
        for c in e["render_payload"]["commands"]:
            assert c["x"] == 80.0


def test_a_single_entry_page_is_a_no_op():
    a = _flow("a", 600.0, 4, 10.0)
    stats = {}
    assert _reflow_page_entries([a], PAGE_H, stats) == [a]
    assert "reflow_shifted_blocks" not in stats


def test_entries_without_usable_geometry_are_left_alone():
    good = _flow("good", 400.0, 2, 10.0)
    broken = {"block_id": "broken", "page": 0, "translated": "x"}
    out = _reflow_page_entries([broken, good], PAGE_H, {})
    assert out[0] is broken


def test_shift_entry_down_records_the_amount():
    a = _flow("a", 600.0, 3, 10.0)
    y0 = a["render_payload"]["commands"][0]["y"]
    b = _shift_entry_down(a, 12.5)
    assert b["render_payload"]["commands"][0]["y"] == pytest.approx(y0 - 12.5)
    assert b["reflow_shift_pt"] == pytest.approx(12.5)
    assert a["render_payload"]["commands"][0]["y"] == y0, "original untouched"


def test_a_zero_shift_is_a_no_op():
    a = _flow("a", 600.0, 3, 10.0)
    assert _shift_entry_down(a, 0.0) is a


def test_entry_extent_uses_the_payload_not_the_source_box():
    """回归：估高必须用 payload 里的真实行位置，而不是 ``dst_box``。

    mp2e page 15 的 ``p15_7`` 的 ``dst_box`` 高达 526pt（几乎整页，明显是布局留下的
    坏框），而它实际只占 5 行 ≈ 66pt。用 box 估高会把整页都判成被它占满。
    """
    a = _flow("a", 600.0, 5, 10.0)
    a["dst_box"] = [80.0, 100.0, 500.0, 626.0]  # 坏框
    top, bottom = _entry_extent(a)
    assert top == pytest.approx(600.0)
    assert bottom > 500.0, f"extent came from the bogus box: bottom={bottom}"


def test_entry_extent_returns_none_for_unusable_geometry():
    assert _entry_extent({}) is None
    assert _entry_extent({"dst_box": [1, 2]}) is None
    assert _entry_extent({"dst_box": [1, 10, 1, 10]}) is None  # 零高
    assert _entry_extent({"dst_box": ["a", "b", "c", "d"]}) is None


# --------------------------------------------------------------------------
# 8. 接线：reflow 必须真的在渲染循环里生效
# --------------------------------------------------------------------------
def test_the_render_loop_actually_applies_the_reflow():
    """集成：两个相撞的块经 ``render_plan_to_pdf`` 之后不再叠印。

    单元测试全都直接调 ``_reflow_page_entries``，所以"根本没接到渲染循环里"这类
    改动一个都杀不掉 —— 这里必须走完整管线。
    """
    import pymupdf

    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

    a = _flow("a", 600.0, 4, 10.0)  # 占 548..600
    b = _flow("b", 598.0, 3, 10.0)  # 顶边 598 落在 a 内部
    plan = [a, b]

    pdf, stats = render_plan_to_pdf(plan, page_sizes={0: [540.0, PAGE_H]})
    assert (
        stats.get("reflow_shifted_blocks") == 1
    ), f"the render loop never called reflow: {stats}"

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    spans = [
        sp
        for blk in doc[0].get_text("dict")["blocks"]
        if blk.get("type") == 0
        for line in blk["lines"]
        for sp in line["spans"]
        if (sp.get("text") or "").strip()
    ]
    doc.close()
    assert len(spans) >= 2
    boxes = [s["bbox"] for s in spans]
    for i, x in enumerate(boxes):
        for y in boxes[i + 1 :]:
            assert not (
                x[0] < y[2] - 3
                and y[0] < x[2] - 3
                and x[1] < y[3] - 3
                and y[1] < x[3] - 3
            ), f"drawn spans still overlap: {x} vs {y}"


def test_every_line_still_renders_after_reflow():
    """重排之后**所有**行都还在页面上 —— 推块不能吃掉文字。

    刻意**不**断言"重排必须在擦除之后"这个顺序：``_erase_rect_for`` 用的是
    ``src_box``（7N-FIX-3B 把它与 ``dst_box``/commands 解耦，正是为了让 shift 块
    不误抹邻行），而 reflow 只改 ``dst_box`` 与命令 y，两者本就不交互。把它当顺序
    不变量会是**等价变异** —— 钉一个假不变量比不钉更糟。
    """
    import pymupdf

    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

    a = _flow("a", 600.0, 4, 10.0)
    b = _flow("b", 598.0, 3, 10.0)
    pdf, _ = render_plan_to_pdf([a, b], page_sizes={0: [540.0, PAGE_H]})
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "".join(doc[0].get_text().split())
    doc.close()
    assert text.count("line") == 7, f"expected all 7 lines drawn, got {text!r}"


def test_min_gap_is_small_but_positive():
    """零间隙会把源 PDF 里本就几乎相接的行盒也算成碰撞。"""
    assert 0.0 < _REFLOW_MIN_GAP <= 3.0
