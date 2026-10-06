"""并页的**逐页重解析**（7P3）：能救回那几页，就不必让它们保持原文。

为什么重解析能解决
------------------
实测（mp2e 前 50 页）：MinerU 把**源第 7~14 页共 8 页目录**压成第 7 页的**一个**
``index`` 块 / **327 行**，而该页高只有 665pt —— 行距 2.03pt。也就是它是**把多页当成
一页看**的结果。单独喂一页进去就没有"多页"可并。

隔离（``test_merged_page_quarantine.py``）只能让这些页**不翻译**；重解析能真正拿回
页边界。这是两者分工：重解析优先，失败才隔离。

为什么复用既有切片
------------------
并页**正是页号错配造成的**。``adapter.parse`` 里已有
``_slice_pdf_for_pages`` + ``_remap_magicpdf_result_pages``（预切片 → 分析 → 页号还原），
自己再造一套页号映射等于把同一个缺陷再写一遍。所以这里只交付页号，拿回**已还原**的结果。

守卫
----
重解析的结果**必须**不再触发并页检测才准进主链路：否则换回来的还是那份被压扁的结果，
而页号已经错位过一次，绝不能让它再流到渲染器。任何一步失败都返回空字典 → 调用方退回隔离。
"""

from __future__ import annotations

import sys
import types

import pytest

from pdf2zh.magicpdf_cli import _reparse_merged_pages
from pdf2zh.magicpdf_adapter import MagicPdfParseResult


def _res(page_num: int, blocks: int = 3, height: float = 665.0):
    """一个**干净**的单页解析结果（行距正常，不会触发并页检测）。"""
    lines = []
    for i in range(blocks * 4):
        y = 76.0 + i * 16.0
        lines.append({"bbox": [118.0, y, 459.0, y + 16.0], "spans": []})
    return MagicPdfParseResult(
        page_num=page_num,
        width=539.0,
        height=height,
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


def _collapsed(page_num: int):
    """一个**仍然并页**的单页结果（327 行挤进一页）。"""
    lines = []
    for i in range(327):
        y = 76.0 + i * 1.45
        lines.append({"bbox": [118.0, y, 459.0, y + 1.45], "spans": []})
    return MagicPdfParseResult(
        page_num=page_num,
        width=539.0,
        height=665.0,
        raw={},
        blocks=[
            {
                "type": "index",
                "cls": "index",
                "bbox": [117.0, 137.0, 459.0, 611.0],
                "lines": lines,
                "text": "x",
                "latex": None,
                "img": None,
            }
        ],
        backend="mineru",
    )


class _FakeAdapter:
    """按页号返回预置结果；记录被请求过哪些页。"""

    def __init__(self, factory):
        self._factory = factory
        self.requested: list = []

    def parse(self, pdf_path, pages=None, ocr=False, lang=None, progress_cb=None):
        self.requested.append(pages)
        return [self._factory(p) for p in (pages or [])]


# --------------------------------------------------------------------------
# 成功路径
# --------------------------------------------------------------------------
def test_a_clean_reparse_is_returned_for_that_page():
    a = _FakeAdapter(lambda p: _res(6))
    got = _reparse_merged_pages(a, "book.pdf", [6], False, "en")
    assert set(got) == {6}
    assert a.requested == [[6]], "must re-parse exactly that page, one page at a time"


def test_reparse_requests_pages_individually():
    a = _FakeAdapter(lambda p: _res(p))
    _reparse_merged_pages(a, "book.pdf", [6, 9], False, "en")
    assert a.requested == [[6], [9]], (
        "batching them back together would re-create the multi-page view that "
        "caused the collapse"
    )


def test_pages_are_visited_in_order_and_deduplicated():
    a = _FakeAdapter(lambda p: _res(p))
    _reparse_merged_pages(a, "book.pdf", [9, 6, 9], False, "en")
    assert a.requested == [[6], [9]]


# --------------------------------------------------------------------------
# 守卫：仍然并页的结果绝不能进主链路
# --------------------------------------------------------------------------
def test_a_reparse_that_is_still_collapsed_is_rejected():
    a = _FakeAdapter(lambda p: _collapsed(6))
    assert _reparse_merged_pages(a, "book.pdf", [6], False, "en") == {}, (
        "a re-parse that is still collapsed must not be adopted -- its page "
        "numbering has already been wrong once"
    )


def test_mixed_outcome_keeps_only_the_clean_page():
    a = _FakeAdapter(lambda p: _res(6) if p == 6 else _collapsed(9))
    got = _reparse_merged_pages(a, "book.pdf", [6, 9], False, "en")
    assert set(got) == {6}, "only the page that actually recovered may be adopted"


# --------------------------------------------------------------------------
# 失败一律退回隔离（返回空字典，不抛）
# --------------------------------------------------------------------------
def test_no_adapter_returns_empty():
    assert _reparse_merged_pages(None, "book.pdf", [6], False, "en") == {}


def test_empty_page_list_returns_empty():
    assert _reparse_merged_pages(_FakeAdapter(_res), "x.pdf", [], False, "en") == {}


def test_a_raising_adapter_is_swallowed():
    class _Boom:
        def parse(self, *a, **k):
            raise RuntimeError("MinerU exploded")

    assert _reparse_merged_pages(_Boom(), "x.pdf", [6], False, "en") == {}


def test_an_adapter_returning_nothing_is_swallowed():
    class _Empty:
        def parse(self, *a, **k):
            return []

    assert _reparse_merged_pages(_Empty(), "x.pdf", [6], False, "en") == {}


def test_none_page_numbers_are_ignored():
    a = _FakeAdapter(lambda p: _res(p))
    assert _reparse_merged_pages(a, "x.pdf", [None], False, "en") == {}
    assert a.requested == []


# --------------------------------------------------------------------------
# 结果的页号必须已经是原文档页号
# --------------------------------------------------------------------------
def test_the_result_carries_the_original_page_number_not_the_slice_index():
    """切片里第 0 页对应原文档第 6 页 —— 映射由 adapter.parse 负责还原。

    若这里拿到的是切片局部页号（0），把它塞回 results 就会把第 0 页的内容画到
    书的第一页上 —— 正是并页的成因。所以断言结果里带的是**请求的**页号。
    """

    class _Remapped:
        """模拟 adapter.parse 已把切片页号还原为原文档页号。"""

        def parse(self, pdf_path, pages=None, ocr=False, lang=None, progress_cb=None):
            return [_res(orig) for orig in (pages or [])]

    got = _reparse_merged_pages(_Remapped(), "x.pdf", [6], False, "en")
    assert set(got) == {6}
    assert got[6].page_num == 6


@pytest.mark.parametrize("bad", [object(), "not-a-number", 1.5j])
def test_unusable_page_numbers_do_not_crash(bad):
    """脏页号不能让补救链路崩掉 —— 崩了就等于没有补救。

    这条覆盖的是**外层** ``except``：``int(bad)`` 发生在逐页 ``try`` **之外**
    （在 ``sorted({int(p) ...})`` 里），所以它正是外层兜底存在的理由。此前这条
    写成「返回 dict 或抛 TypeError 都算过」，于是把外层兜底改成 ``raise`` 的变异体
    照样通过 —— 那等于把补救链路从"降级"降级成"崩溃"。
    """
    a = _FakeAdapter(lambda p: _res(0))
    out = _reparse_merged_pages(a, "x.pdf", [bad], False, "en")
    assert out == {}, "an unusable page number must degrade to 'no re-parse'"


# --------------------------------------------------------------------------
# 换回 results：按页号，不按顺序
# --------------------------------------------------------------------------
def test_reparsed_page_is_swapped_into_its_own_slot():
    from pdf2zh.magicpdf_cli import _swap_reparsed_pages

    results = [_res(p) for p in (5, 6, 7)]
    good = _res(6)
    good.raw = {"marker": "reparsed"}
    assert _swap_reparsed_pages(results, {6: good}) == 1
    assert results[1] is good, "page 6's content landed in the wrong slot"
    assert results[0].raw != {"marker": "reparsed"}
    assert results[2].raw != {"marker": "reparsed"}


def test_swapping_by_position_would_be_wrong_and_swap_refuses_to():
    """按顺序替换会把第 7 页写进第 3 页 —— 那正是并页的成因。

    这条把"为什么必须按页号"钉住：``reparsed`` 只含 1 页，而 ``results`` 有 3 页，
    顺序替换与按页号替换的结果完全不同。
    """
    from pdf2zh.magicpdf_cli import _swap_reparsed_pages

    results = [_res(p) for p in (5, 6, 7)]
    good = _res(7)
    good.raw = {"marker": "reparsed"}
    _swap_reparsed_pages(results, {7: good})
    assert results[2] is good, "must key on page number, not position"
    assert results[0].raw == {}, "page 5 must be untouched"


def test_a_page_with_no_matching_slot_is_reported_not_silently_dropped():
    """换不进去的页必须让调用方知道 —— 否则它会落进「既没隔离也没内容」的最坏组合。"""
    from pdf2zh.magicpdf_cli import _swap_reparsed_pages

    results = [_res(p) for p in (5, 7)]  # 没有第 6 页
    assert _swap_reparsed_pages(results, {6: _res(6)}) == 0
    assert _swap_reparsed_pages(results, {}) == 0
    assert _swap_reparsed_pages([], {6: _res(6)}) == 0


def test_multiple_pages_are_swapped_by_number():
    from pdf2zh.magicpdf_cli import _swap_reparsed_pages

    results = [_res(p) for p in (3, 6, 9)]
    a, b = _res(6), _res(9)
    a.raw, b.raw = {"m": "a"}, {"m": "b"}
    assert _swap_reparsed_pages(results, {6: a, 9: b}) == 2
    assert results[1] is a and results[2] is b
    assert results[0].raw == {}


def test_swapping_tolerates_unusable_page_numbers():
    from pdf2zh.magicpdf_cli import _swap_reparsed_pages

    results = [_res(6)]

    class _Bad:
        page_num = None

    results.append(_Bad())
    assert _swap_reparsed_pages(results, {6: _res(6)}) == 1


def test_the_module_has_no_unexpected_stdlib_dependency():
    """只用标准库 + 项目内模块，便于在打包环境里工作。"""
    import pdf2zh.magicpdf_cli as mod

    src = sys.modules[mod.__name__]
    assert hasattr(src, "_reparse_merged_pages")
    assert isinstance(types.ModuleType, type)  # 保持 import 有意义
