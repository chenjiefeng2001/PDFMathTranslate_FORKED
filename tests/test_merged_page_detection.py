"""「多个源页被并进同一 render 页」的检出（doc/7p0 P0）。

背景与真实证据见 ``doc/7p0_real_load_report.md``：MinerU 把 mp2e **源第 7~14 页
共 8 页目录**压成第 7 页的一个 ``index`` 块 / 326 个 line，而该页高只有 665pt。
我们照单展开成 201 个 render 块后，87 块共享 35 个重复的顶部 y（最多 5 块 y 完全
相同），即 8 个源页的行叠在同一坐标上，成品里目录条目的原文与译文都查不到。

判据是**物理的、与引擎无关**的：行距不可能是 2pt，一页装不下 6 页的行高。所以这里
用真实测出来的几何造样本，而不是编数字 —— 编出来的数字会落在阈值另一侧，什么都
证明不了。
"""

from __future__ import annotations

import pytest

from pdf2zh.magicpdf_adapter import MagicPdfParseResult, detect_merged_pages

#: mp2e 实测页高（doc/7p0-load50/output 的 page_num=6 元素 height=665.0）
PAGE_H = 665.0
#: mp2e 实测正常页行距区间 15.11~16.62pt，中位数 ~16
NORMAL_LEADING = 15.5


def _lines(n: int, leading: float = NORMAL_LEADING, top: float = 76.0):
    """造 n 个行 bbox，行距 leading，起点 top。"""
    out = []
    for i in range(n):
        y0 = top + i * leading
        out.append({"bbox": [118.0, y0, 459.0, y0 + leading], "spans": []})
    return out


def _page(page_num: int, blocks, height: float = PAGE_H) -> MagicPdfParseResult:
    return MagicPdfParseResult(
        page_num=page_num,
        width=539.0,
        height=height,
        raw={},
        blocks=blocks,
        backend="mineru",
    )


def _text_block(n_lines: int, leading: float = NORMAL_LEADING) -> dict:
    return {
        "type": "text",
        "cls": "text",
        "bbox": [118.0, 76.0, 459.0, 611.0],
        "lines": _lines(n_lines, leading),
        "text": "x",
        "latex": None,
        "img": None,
    }


# --------------------------------------------------------------------------
# 真实损坏形态
# --------------------------------------------------------------------------
def test_flags_the_measured_eight_page_toc_collapse():
    """326 行挤进 665pt 的一页 —— 实测值，不是构造出来的。

    实测该页：327 lines / 2.03pt per line / 隐含 5.83 页。
    """
    merged = _text_block(326, leading=1.45)  # 326 行覆盖 y76~549，与实测 bbox 一致
    page = _page(6, [merged])

    suspects = detect_merged_pages([page])

    assert len(suspects) == 1, suspects
    s = suspects[0]
    assert s["page_num"] == 6
    assert s["lines"] == 326
    assert s["pt_per_line"] == pytest.approx(665.0 / 326, abs=0.01)
    assert s["pt_per_line"] < 6.0


def test_flags_the_measured_shape_title_plus_index_block():
    """必须是**所有块**的行数之和 —— 实测的折叠页正是两个块。

    真实结构（``*_magicpdf.json`` page_num=6）：block0 = ``title`` "Contents"
    只有 1 行，block1 = ``index`` 带 326 行。只累加第一个块的实现会得出
    「1 行 / 665pt」，完全正常，于是**漏掉这个真实缺陷**。这条测试按实测形状
    构造，不是按理想形状。
    """
    title = {
        "type": "title",
        "cls": "title",
        "bbox": [118.0, 76.0, 203.0, 100.0],
        "lines": _lines(1, leading=24.0),
        "text": "Contents",
        "latex": None,
        "img": None,
    }
    index = {
        "type": "index",
        "cls": "index",
        "bbox": [117.0, 137.0, 459.0, 611.0],
        "lines": _lines(326, leading=1.45),
        "text": "Preface .xvAcknowledgmentsxix...",
        "latex": None,
        "img": None,
    }
    page = _page(6, [title, index])

    suspects = detect_merged_pages([page])

    assert len(suspects) == 1, (
        "line count must accumulate across blocks; the measured collapse has "
        "the big index block second"
    )
    assert suspects[0]["lines"] == 327
    assert suspects[0]["blocks"] == 2


def test_flags_even_when_only_the_implied_pages_signal_fires():
    """两个指标是冗余的：任何一项越界都要报。

    这里给足行距（不触发 pt/line），但行高总和超过页高 2 倍 —— 仍然是折叠。
    """
    # 40 行、每行 40pt 高、步进 16pt：pt/line 正常，但行高总和 = 1600pt > 2 页
    lines = []
    for i in range(40):
        y0 = 40.0 + i * 16.0
        lines.append({"bbox": [118.0, y0, 459.0, y0 + 40.0], "spans": []})
    page = _page(
        9,
        [
            {
                "type": "index",
                "cls": "index",
                "bbox": [117.0, 40.0, 459.0, 640.0],
                "lines": lines,
                "text": "x",
                "latex": None,
                "img": None,
            }
        ],
    )

    assert detect_merged_pages([page]), (
        "line-height sum over 2x the page height must be caught even when the "
        "per-line leading looks plausible"
    )


# --------------------------------------------------------------------------
# 不能误伤正常页
# --------------------------------------------------------------------------
def test_normal_text_page_is_not_flagged():
    """实测最差的正常页是 44 行 / 15.11pt 每行 —— 阈值必须放过它。"""
    page = _page(31, [_text_block(44)])
    assert detect_merged_pages([page]) == []


def test_a_full_density_page_is_not_flagged():
    """行距压到 8pt 的密排正文仍不该被报（阈值 6.0 留了余量）。"""
    page = _page(12, [_text_block(80, leading=8.0)])
    assert detect_merged_pages([page]) == []


def test_dense_toc_page_with_real_leading_is_not_flagged():
    """真实的单页目录约 40 行、行距 11pt —— 这是正常形态，不是折叠。"""
    page = _page(7, [_text_block(40, leading=11.0)])
    assert detect_merged_pages([page]) == []


def test_thresholds_leave_at_least_two_x_headroom_over_the_measured_case():
    """把实测值钉住，避免有人调阈值调到刚好擦边。

    实测折叠页 2.03pt/行、次差正常页 15.11pt/行 —— 中间有 7 倍空间，阈值必须
    落在两侧至少各 2 倍处，任意一侧被改动都能被这个测试看见。
    """
    collapsed = detect_merged_pages([_page(6, [_text_block(326, leading=1.45)])])
    worst_normal = detect_merged_pages([_page(31, [_text_block(44)])])
    assert collapsed, "the measured collapse must be flagged at default thresholds"
    assert not worst_normal, "the measured worst normal page must stay silent"
    # 默认阈值 6.0 相对实测 2.03 有 >=2 倍余量
    assert 6.0 / collapsed[0]["pt_per_line"] >= 2.0


# --------------------------------------------------------------------------
# 边界与健壮性
# --------------------------------------------------------------------------
def test_empty_document_is_not_flagged():
    assert detect_merged_pages([]) == []


def test_page_without_lines_is_not_flagged():
    assert detect_merged_pages([_page(3, [_text_block(0)])]) == []


def test_unknown_page_height_is_skipped_not_crashed():
    """页高缺失时无法判断，必须跳过而不是抛异常或瞎报。"""
    page = _page(4, [_text_block(300, leading=1.0)], height=0.0)
    assert detect_merged_pages([page]) == []


def test_malformed_line_bbox_is_tolerated():
    """line bbox 残缺/非数值不能把整次检测带崩 —— 这是诊断代码，不是主链路。"""
    lines = [
        {"bbox": [1, 2], "spans": []},  # 长度不对
        {"bbox": None, "spans": []},  # None
        {"spans": []},  # 缺 bbox
        {"bbox": ["a", "b", "c", "d"], "spans": []},  # 非数值
    ]
    page = _page(
        5,
        [
            {
                "type": "text",
                "cls": "text",
                "bbox": None,
                "lines": lines,
                "text": "",
                "latex": None,
                "img": None,
            }
        ],
    )
    assert isinstance(detect_merged_pages([page]), list)


def test_a_broken_y1_does_not_fabricate_line_height():
    """``y1`` 坏掉时若当成 y1=0，行高会从 0 变成 y0 —— 虚增。

    40 行、每行起点 y=600 而 y1 不可解析：若把 y1 当 0，每行「高」600pt，行高
    总和 24000pt = 36 页，一个完全正常的页会被误判成折叠。
    """
    lines = [{"bbox": [118.0, 600.0, 459.0, "bad"], "spans": []} for _ in range(40)]
    page = _page(
        5,
        [
            {
                "type": "text",
                "cls": "text",
                "bbox": None,
                "lines": lines,
                "text": "",
                "latex": None,
                "img": None,
            }
        ],
    )
    assert (
        detect_merged_pages([page]) == []
    ), "an unparsable y1 was treated as 0 and inflated the line-height sum"


def test_only_the_offending_page_is_reported():
    """50 页里只有一页折叠时，报告里只能有那一页 —— 否则降级理由会误导。"""
    pages = [_page(i, [_text_block(40)]) for i in range(50)]
    pages[6] = _page(6, [_text_block(326, leading=1.45)])
    suspects = detect_merged_pages(pages)
    assert [s["page_num"] for s in suspects] == [6]


def test_reported_metrics_are_serialisable():
    """降级理由会把这些字段写进日志/trace，必须是纯标量。"""
    import json

    suspects = detect_merged_pages([_page(6, [_text_block(326, leading=1.45)])])
    json.dumps(suspects)  # 不抛即为可序列化
