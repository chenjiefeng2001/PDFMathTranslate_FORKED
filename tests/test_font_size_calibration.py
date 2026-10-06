"""字号校准：以源 PDF 的真实字号为准，让既有 SHRINK 阶梯兜底溢出。

对应 ``doc/7p2_font_chain_audit.md`` 的根因：MinerU 的 span ``size`` 恒为 0，唯一信号是
span 框高，而 ``magicpdf_bridge`` 用固定的 ``框高 × 0.85`` 猜字号 —— 实测 mp2e 正文
9.96pt 被推成 7.65pt，成品译文比原文小 23%。

要求是「**不溢出的前提下尽量视觉保真**」。做法不是取消溢出保护，而是把**起点**换成
事实：查表给出的是源 PDF 自己的字号，而源文档就是用这个字号排进那个框的，所以它不可能
比原文更容易溢出；放不下的块仍由 ``pdf2zh.semantic.layout.adaptive`` 的
WRAP→SHRINK→CLIP 阶梯缩回去。

这些测试同时钉住两半：**校准要真的命中源字号**，以及**放不下时必须缩**。

实测得到的三条事实（改设计时别再违反）：

1. 源 PDF 里「字号 ≈ 行框高」：比值 1.00，264 行、每档纯度约 100%。
2. MinerU 的 span 框高 ≈ 源的行框高：delta ≈ 0.0。
3. MinerU 对同一段 9.96pt 正文，框高时而报 9.0、时而报 10.0（±0.5 量化抖动）。
   —— 事实 3 是"必须查表、不能用单一系数"的全部理由：任何系数都无法同时把 9.0 和
   10.0 映射到 9.96。实测页级比例方案把平均误差从 9.9% 恶化到 15.3%。
"""

from __future__ import annotations

import pytest

from pdf2zh.magicpdf_adapter import MagicPdfParseResult, _annotate_size_scale
from pdf2zh.v3.canonical_page import annotate_style
from pdf2zh.v3.magicpdf_bridge import (
    DEFAULT_SIZE_SCALE,
    SIZE_LOOKUP_TOL,
    SIZE_SCALE_MAX,
    SIZE_SCALE_MIN,
    MagicPdfBridge,
    calibrate_size_map,
)

SRC = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"


def _result(
    page_num: int = 14, box: float = 9.0, n_lines: int = 20
) -> MagicPdfParseResult:
    lines = []
    for i in range(n_lines):
        y = 76.0 + i * box
        lines.append(
            {
                "bbox": [118.0, y, 459.0, y + box],
                "spans": [
                    {
                        "bbox": [118.0, y, 459.0, y + box],
                        "content": "sample body text",
                        "type": "text",
                    }
                ],
            }
        )
    return MagicPdfParseResult(
        page_num=page_num,
        width=539.0,
        height=665.0,
        raw={},
        blocks=[
            {
                "type": "text",
                "cls": "text",
                "bbox": [118.0, 76.0, 459.0, 611.0],
                "lines": lines,
                "text": "x",
                "latex": None,
                "img": None,
            }
        ],
        backend="mineru",
    )


def _font_size(res) -> float:
    page = MagicPdfBridge().convert(res)
    annotate_style(page)
    return page.blocks[0].font_size


# --------------------------------------------------------------------------
# 查表：核心是"±0.5 量化抖动也要命中"
# --------------------------------------------------------------------------
def test_both_quantized_boxes_land_on_the_source_size():
    """同一段 9.96pt 正文，MinerU 报 9.0 或 10.0 —— 两者都必须得到 9.96。

    这是整个改动的存在理由。任何单一系数都过不了这条。
    """
    cal = calibrate_size_map(SRC, 14, [9.0] * 90 + [10.0] * 50)
    assert cal.size_for(9.0) == pytest.approx(9.96, abs=0.01)
    assert cal.size_for(10.0) == pytest.approx(9.96, abs=0.01)


def test_end_to_end_font_size_equals_the_source_size():
    res = _result(box=9.0)
    assert _font_size(res) == pytest.approx(7.65, abs=0.01), "precondition: 0.85 guess"

    _annotate_size_scale([res], SRC)

    assert _font_size(res) == pytest.approx(
        9.96, abs=0.01
    ), "calibrated size must land on the source's real glyph size"


def test_the_translation_is_no_longer_smaller_than_the_original():
    """用户可见的判据：译文不再比原文小一截。"""
    res = _result(box=9.0)
    _annotate_size_scale([res], SRC)
    assert _font_size(res) >= 9.96 * 0.99


def test_each_size_band_keeps_its_own_size():
    """混合字号的页：图注/标题不能被正文档位吞掉。"""
    cal = calibrate_size_map(
        SRC, 14, [9.0] * 90 + [10.0] * 50 + [15.0] * 5 + [21.0] * 4
    )
    assert cal.size_for(21.0) == pytest.approx(21.92, abs=0.01), "big heading kept"
    assert cal.size_for(15.0) == pytest.approx(13.45, abs=0.01), "sub-heading kept"
    assert cal.size_for(9.0) == pytest.approx(9.96, abs=0.01), "body kept"


# --------------------------------------------------------------------------
# 兜底比例：算错会让小字被放大，制造溢出
# --------------------------------------------------------------------------
def test_fallback_scale_is_not_pinned_at_the_clamp():
    """回归：``src_mean`` 必须按出现次数加权。

    用「表值未加权平均」时，只出现 4 次的 21.9pt 大标题和出现 150 次的 9.96pt 正文
    等重，均值被拉到 13.82、比例 1.45 被钳到上限 1.35 —— 那样 60pt 的框会拿到 81pt
    的字号，直接溢出。
    """
    cal = calibrate_size_map(SRC, 14, [9.0] * 90 + [10.0] * 50)
    assert cal.fallback_scale < SIZE_SCALE_MAX, (
        f"fallback scale pinned at the clamp ({cal.fallback_scale}): the source mean "
        "is almost certainly unweighted"
    )
    assert SIZE_SCALE_MIN < cal.fallback_scale < SIZE_SCALE_MAX


def test_a_box_far_from_every_source_band_uses_the_fallback_ratio():
    """查表未命中 → 用比例（猜测），不是硬套最近的档位。"""
    cal = calibrate_size_map(SRC, 14, [9.0] * 90 + [10.0] * 50)
    # 表里最大档是 21.9；40.0 远超容差，应走比例而不是跳到 21.92
    far = cal.size_for(40.0)
    assert far != pytest.approx(21.92, abs=0.01), "must not snap to a far-off band"
    assert far == pytest.approx(40.0 * cal.fallback_scale, abs=0.01)


def test_lookup_tolerance_covers_the_measured_quantization_jitter():
    """容差必须盖住 ±0.5 抖动 + 源/解析最大 delta 1.4pt。"""
    assert SIZE_LOOKUP_TOL >= 1.4, "too tight: real delta measured at 1.4pt"
    assert (
        SIZE_LOOKUP_TOL <= 2.5
    ), "too loose: a 6pt caption would match a 21pt heading and get inflated"


def test_a_zero_height_box_stays_zero():
    cal = calibrate_size_map(SRC, 14, [9.0])
    assert cal.size_for(0.0) == 0.0
    assert cal.size_for(-1.0) == 0.0


def test_extreme_box_means_are_clamped():
    """钳位必须有测试，否则它形同虚设。

    解析框高全部异常（0.1 或 100.0）时，两侧比值会跑到 100 / 0.1 量级。不钳位的话，
    兜底比例会让这些页的字号凭空放大/缩小一个数量级。
    """
    tiny = calibrate_size_map(SRC, 14, [0.1] * 40)
    assert tiny.fallback_scale == SIZE_SCALE_MAX

    huge = calibrate_size_map(SRC, 14, [100.0] * 40)
    assert huge.fallback_scale == SIZE_SCALE_MIN


# --------------------------------------------------------------------------
# 建表语义：每个框高取众数，不是最大值
# --------------------------------------------------------------------------
def test_each_box_band_uses_the_majority_size_not_the_largest(monkeypatch):
    """同一行里混有多种字号时，按**众数**建档，而不是取最大。

    ``max`` 会被一个上标/加粗引导词带跑，把整行的小字放大。mp2e 真实页面上按框高
    聚合后 modal 与 max 常常一致（这个差异被聚合抹平了），所以直接在 I/O 边界打桩、
    喂一条混合字号的行才能区分 —— 但语义必须钉住，否则一次「顺手改成 max」就会在
    别的文档上静默放大正文。

    （这里打桩而不是造 PDF：pymupdf 会把分开 ``insert_text`` 的不同字号 span 拆成
    独立的行，造不出「一行两种字号」。）
    """
    import pymupdf

    line = {
        "bbox": [20.0, 50.0, 300.0, 76.0],  # 行框高 26.0
        "spans": [
            {"text": "aaaaaaaaaa", "size": 6.0},
            {"text": "bbbbbbbb", "size": 6.0},
            {"text": "BBB", "size": 20.0},  # 干扰项：必须 ≥3 字符，否则被长度过滤
        ],
    }
    page = type(
        "P",
        (),
        {"get_text": lambda self, kind: {"blocks": [{"type": 0, "lines": [line]}]}},
    )()
    doc = type(
        "D",
        (),
        {
            "page_count": 1,
            "__getitem__": lambda self, i: page,
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        },
    )()
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: doc)

    cal = calibrate_size_map("ignored.pdf", 0, [26.0])
    got = cal.size_for(26.0)
    assert got == pytest.approx(
        6.0, abs=1.0
    ), f"expected the 6pt majority, got {got}pt -- the map took max instead of modal"


def test_short_spans_do_not_win_the_modal_vote(monkeypatch):
    """孤立短字符（页码、标点、上标）不得左右整行字号。

    不足 3 个字符的 span 被排除在建档统计之外。少了这个过滤，一行里两个 30pt 的孤立
    字符就能靠数量压过一个整行的 10pt 正文，把整行放大三倍。
    """
    import pymupdf

    line = {
        "bbox": [20.0, 50.0, 300.0, 76.0],
        "spans": [
            {"text": "aaaaaaaaaa", "size": 10.0},  # 整行正文
            {"text": "B", "size": 30.0},  # 孤立短字符
            {"text": "C", "size": 30.0},
        ],
    }
    page = type(
        "P",
        (),
        {"get_text": lambda self, kind: {"blocks": [{"type": 0, "lines": [line]}]}},
    )()
    doc = type(
        "D",
        (),
        {
            "page_count": 1,
            "__getitem__": lambda self, i: page,
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        },
    )()
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: doc)

    got = calibrate_size_map("ignored.pdf", 0, [26.0]).size_for(26.0)
    assert got == pytest.approx(
        10.0, abs=1.0
    ), f"expected the 10pt body, got {got}pt -- short spans skewed the modal"


# --------------------------------------------------------------------------
# 接线：adapter 必须收 span 框高，且 parse() 的两个分支都要校准
# --------------------------------------------------------------------------
def test_adapter_collects_span_boxes_not_line_boxes():
    """回归：收的是 **span** 框高。

    bridge 里 ``size = span bbox 高``，所以兜底比例的分母必须是 span 框高。误收行框高
    会把比例算小（本例 0.6 vs 1.11），进而把查表未命中的块缩得远小于源字号。
    """
    res = _result(box=9.0)
    for line in res.blocks[0]["lines"]:
        line["bbox"] = [118.0, 76.0, 459.0, 166.0]  # 行框 90.0，span 仍是 9.0
    assert _annotate_size_scale([res], SRC) == 1
    cal = res.raw["size_map"]
    assert (
        cal.fallback_scale > 0.9
    ), f"fallback {cal.fallback_scale} implies line boxes were collected, not span boxes"


def _stubbed_adapter(monkeypatch, page_num: int = 0):
    """打桩掉昂贵的 MinerU 调用，返回一个已接好线的 adapter。"""
    import pdf2zh.magicpdf_adapter as adapter_mod

    ad = adapter_mod.MagicPdfAdapter()
    monkeypatch.setattr(ad, "backend", lambda: "mineru")
    monkeypatch.setattr(
        ad, "_parse_by_backend", lambda *a, **k: [_result(page_num=page_num)]
    )
    return ad


def test_parse_calibrates_the_slice_path(monkeypatch):
    """切片解析分支也必须校准。

    ``parse(pages=[...])`` 会先切 PDF、把页号还原成原页号，再校准。校准漏在这个分支里，
    真实使用（带页面选择）就完全不生效。
    """
    seen: dict = {}
    ad = _stubbed_adapter(monkeypatch)

    def fake_parse(backend, pdf_path, **k):
        seen["pdf_path"] = pdf_path
        return [_result(page_num=0)]

    monkeypatch.setattr(ad, "_parse_by_backend", fake_parse)

    out = ad.parse(SRC, pages=[14])

    assert seen["pdf_path"] != SRC, "precondition: the slice really happened"
    assert len(out) == 1
    assert (
        out[0].raw.get("size_map") is not None
    ), "slice path skipped calibration -- font sizes stay 23% too small in real use"


def test_parse_calibrates_the_full_document_path(monkeypatch):
    """不切片（整篇解析）的分支同样必须校准 —— 这是最常见的用法。"""
    import pdf2zh.magicpdf_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod, "_slice_pdf_for_pages", lambda *a, **k: (None, None)
    )
    ad = _stubbed_adapter(monkeypatch, page_num=14)  # 第 15 页：确定有正文可校准

    out = ad.parse(SRC)

    assert len(out) == 1
    assert (
        out[0].raw.get("size_map") is not None
    ), "full-document path skipped calibration"


# --------------------------------------------------------------------------
# 兜底：校不准就用默认，绝不崩
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pdf", ["", None, "does-not-exist.pdf"])
def test_unreadable_source_falls_back_to_the_default_scale(pdf):
    res = _result()
    assert _annotate_size_scale([res], pdf) == 0
    assert "size_map" not in res.raw
    assert _font_size(res) == pytest.approx(7.65, abs=0.01)


def test_page_without_spans_falls_back():
    res = _result()
    res.blocks[0]["lines"] = []
    assert _annotate_size_scale([res], SRC) == 0
    assert "size_map" not in res.raw


def test_out_of_range_page_falls_back():
    assert _annotate_size_scale([_result(page_num=99999)], SRC) == 0


def test_empty_results_do_not_crash():
    assert _annotate_size_scale([], SRC) == 0
    assert _annotate_size_scale(None, SRC) == 0


def test_malformed_span_boxes_do_not_crash():
    res = _result()
    res.blocks[0]["lines"] = [
        {"spans": [{"bbox": [1, 2]}, {"bbox": None}, {"bbox": ["a", "b", "c", "d"]}]},
        {"spans": [{"bbox": [1, 2, 3, 4], "content": "x"}]},
        {"bbox": [1, 2, 3, 4], "spans": []},
    ]
    assert _annotate_size_scale([res], SRC) in (0, 1)  # 不崩即可


def test_a_calibration_blowup_never_breaks_parsing(monkeypatch):
    """最硬的契约：校准是锦上添花，它炸了也不能让整个解析失败。

    源 PDF 可能是加密的、截断的、正在被写的。把这个 except 改成 ``raise``，
    下面的测试就会红 —— 这正是它存在的理由。
    """
    import pdf2zh.v3.magicpdf_bridge as bridge

    def boom(*a, **k):
        raise RuntimeError("source pdf is truncated / encrypted / locked")

    monkeypatch.setattr(bridge, "calibrate_size_map", boom)

    res = _result()
    assert _annotate_size_scale([res], SRC) == 0, "blowup must be swallowed"
    assert "size_map" not in res.raw
    assert _font_size(res) == pytest.approx(
        7.65, abs=0.01
    ), "a failed calibration must leave the default in place, not corrupt the page"


def test_a_calibration_blowup_does_not_stop_later_pages(monkeypatch):
    """一页炸了不能连累后面的页 —— 逐页容错，不是整批放弃。"""
    import pdf2zh.v3.magicpdf_bridge as bridge

    real = bridge.calibrate_size_map

    def flaky(path, page, boxes):
        if page == 14:
            raise RuntimeError("transient I/O error")
        return real(path, page, boxes)

    monkeypatch.setattr(bridge, "calibrate_size_map", flaky)

    good = _result(page_num=15)
    assert _annotate_size_scale([_result(page_num=14), good], SRC) == 1
    assert good.raw["size_map"] is not None
    # 第 16 页自己有一档 9.5pt 源行框（→9.46pt），框高 9.0 的最近邻就是它
    assert _font_size(good) == pytest.approx(9.46, abs=0.01)


# --------------------------------------------------------------------------
# 溢出兜底：放不下时布局层必须缩
# --------------------------------------------------------------------------
def test_the_shrink_ladder_still_handles_a_block_that_cannot_fit():
    """字号变大后，放不下的块由布局层缩回去 —— 这才是"不溢出"的实现。"""
    from pdf2zh.semantic.renderer.flow import render_flow_text

    res = _result(box=9.0, n_lines=1)
    _annotate_size_scale([res], SRC)
    size = _font_size(res)
    assert size == pytest.approx(9.96, abs=0.01), "precondition: calibrated up"

    out = render_flow_text(
        "A very long translated sentence that cannot possibly fit "
        "into a thirty point wide box without shrinking. "
        "中文译文同样长，也必须缩放才能放下。",
        origin=(118.0, 76.0),
        max_width=30.0,
        max_height=14.0,
        line_height=1.4,
        font_size=size,
    )
    assert (
        out["font_size"] < size
    ), f"font stayed at {size}pt in a 30x14pt box -- that overflows"
    assert out["overflow"] is True, "an un-honest 'fits' report is worse"


def test_a_generous_box_keeps_the_calibrated_size():
    """框够大时不应无谓缩小 —— 保真优先。"""
    from pdf2zh.semantic.renderer.flow import render_flow_text

    res = _result()
    _annotate_size_scale([res], SRC)
    size = _font_size(res)

    out = render_flow_text(
        "Short line.",
        origin=(118.0, 76.0),
        max_width=400.0,
        max_height=400.0,
        line_height=1.4,
        font_size=size,
    )
    assert out["font_size"] == pytest.approx(
        size, abs=0.2
    ), f"shrank to {out['font_size']} in a box that easily fits {size}"


def test_calibrated_size_never_exceeds_its_own_span_box_by_a_lot():
    """兜底比例也不能把字号抬到远超框高 —— 那是溢出。

    源 PDF 自身的「字号 / 行框高」是 1.00；给 1.4 的余量仍远低于出问题的 1.35 倍率。
    """
    res = _result(box=9.0)
    _annotate_size_scale([res], SRC)
    assert _font_size(res) <= 9.0 * 1.4 + 0.01


# --------------------------------------------------------------------------
# 前提钉子：源是什么字号
# --------------------------------------------------------------------------
def test_the_source_body_size_is_still_what_we_think_it_is():
    """钉住前提：源正文 9.96pt。语料换了这条会先失败。"""
    import pymupdf

    sizes: dict[float, int] = {}
    with pymupdf.open(SRC) as doc:
        for p in range(0, 30):
            for blk in doc[p].get_text("dict").get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for line in blk.get("lines", []):
                    for sp in line.get("spans", []):
                        if len((sp.get("text") or "").strip()) >= 8:
                            k = round(float(sp.get("size") or 0), 2)
                            sizes[k] = sizes.get(k, 0) + 1
    assert max(sizes, key=lambda k: sizes[k]) == pytest.approx(9.96, abs=0.01)


def test_default_scale_is_still_the_conservative_fallback():
    """兜底系数不能被"顺手"调大 —— 它只在读不到源 PDF 时生效，必须偏小不溢出。"""
    assert DEFAULT_SIZE_SCALE == 0.85
    assert DEFAULT_SIZE_SCALE < SIZE_SCALE_MIN or SIZE_SCALE_MIN <= DEFAULT_SIZE_SCALE
    assert DEFAULT_SIZE_SCALE < 1.0, "an uncalibrated guess must not inflate"
