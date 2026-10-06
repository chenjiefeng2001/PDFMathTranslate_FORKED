"""并页的**逐页隔离**：一页折叠不该让整篇放弃 magicpdf（7P2 修复）。

背景
----
``doc/7p2_control_experiment.md`` 的对照实验暴露了一个我自己引入的缺陷：P0 的
``detect_merged_pages`` 命中后走 ``_degrade_engine``，于是**整篇 50 页**退到 legacy
内核，代价是叠印 11→16 页、越界 0→3 个 span。而真正出问题的只有第 7 页。

现在改成：占比不高时**只隔离那几页**（保留原文、不翻译不重绘），其余页照常走
magicpdf；只有占比超过阈值（整篇解析都不可信）才退回整篇降级。

这些测试钉住的是「隔离不等于丢页」和「隔离不等于擦白」两件事 —— 后者尤其关键：
白擦了原文又不画新内容，就是凭空抹掉一页。
"""

from __future__ import annotations

from pdf2zh.magicpdf_cli import _MERGED_PAGE_DEGRADE_RATIO, _quarantine_merged_pages
from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel
from pdf2zh.v3.document_model import (
    DocumentModel,
    render_plan_from_model,
    translate_document,
)
from pdf2zh.v3.magicpdf_renderer import _erases_source_region, _is_translated_block


def _doc(pages: int = 5, blocks: int = 3) -> DocumentModel:
    m = DocumentModel()
    for p in range(pages):
        pg = PageModel(page_num=p, width=595.0, height=842.0)
        pg.blocks = [
            BlockModel(
                text=f"p{p} b{i}",
                kind="paragraph",
                x0=72.0,
                y0=700.0,
                x1=540.0,
                y1=722.0,
                lines=[
                    LineModel(
                        text=f"p{p} b{i}",
                        baseline=0.0,
                        x0=72.0,
                        y0=700.0,
                        x1=540.0,
                        y1=722.0,
                    )
                ],
                metadata={"font_size": 12.0},
            )
            for i in range(blocks)
        ]
        m.pages.append(pg)
    return m


# --------------------------------------------------------------------------
# 隔离的语义
# --------------------------------------------------------------------------
def test_quarantined_page_keeps_its_source_text_and_is_not_marked_translated():
    m = _doc()
    _quarantine_merged_pages(m, [2])
    translate_document(m, lambda t: t.upper(), thread=4)

    for b in m.pages[2].blocks:
        assert (
            b.metadata["translated"] == b.text
        ), "an isolated block must fall back to its own source text"
        assert b.metadata["translate"] is False


def test_other_pages_are_translated_normally():
    m = _doc()
    _quarantine_merged_pages(m, [2])
    translate_document(m, lambda t: t.upper(), thread=4)

    for p in (0, 1, 3, 4):
        for b in m.pages[p].blocks:
            assert (
                b.metadata["translated"] == b.text.upper()
            ), f"page {p} should still be translated"


def test_isolated_blocks_are_neither_drawn_nor_erased():
    """隔离 ≠ 擦白。

    白擦了原文又不画任何东西，就是凭空抹掉一页 —— 那是 commit 63a5555 修过的
    同一类缺陷，换个入口又回来了。
    """
    m = _doc()
    _quarantine_merged_pages(m, [2])
    translate_document(m, lambda t: t.upper(), thread=4)
    plan = render_plan_from_model(m)

    class _Src:
        page_count = 5

    src = _Src()
    isolated = [e for e in plan if e["page"] == 2]
    others = [e for e in plan if e["page"] == 0]
    assert isolated and others, "plan should contain both kinds"

    for e in isolated:
        assert not _is_translated_block(e), "an isolated block must not be redrawn"
        assert not _erases_source_region(e, src), (
            "an isolated block must NOT be erased -- it keeps the original text, "
            "so whitening it would delete the page's content"
        )
    assert any(
        _is_translated_block(e) for e in others
    ), "translated pages must still be drawn"


def test_the_page_still_exists_in_the_plan():
    """隔离的页不能从产物里消失 —— 页数必须不变。"""
    m = _doc()
    _quarantine_merged_pages(m, [2])
    translate_document(m, lambda t: t.upper(), thread=4)
    plan = render_plan_from_model(m)
    assert 2 in {e["page"] for e in plan}


def test_stale_translations_are_cleared_immediately():
    """隔离那一刻就要清掉已回填的译文，否则渲染器会拿旧值再画一遍。

    这条断言查的是**隔离后、翻译前**的状态 —— 之前只查翻译后的最终值，而
    ``translate_document`` 会用 translate_fn 的返回值覆盖 ``translated``，于是
    「忘了 pop」这个变异体照样通过。
    """
    m = _doc(pages=1, blocks=1)
    m.pages[0].blocks[0].metadata["translated"] = "STALE VALUE"
    _quarantine_merged_pages(m, [0])
    assert (
        "translated" not in m.pages[0].blocks[0].metadata
    ), "a stale translation survived quarantine and would be drawn"


def test_a_stale_translation_is_never_drawn_even_without_translation():
    """绕过 translate_document 直接出 plan 时同样不能画出旧值。"""
    m = _doc(pages=1, blocks=1)
    m.pages[0].blocks[0].metadata["translated"] = "STALE VALUE"
    _quarantine_merged_pages(m, [0])
    plan = render_plan_from_model(m)
    for e in plan:
        assert not _is_translated_block(
            e
        ), f"stale translation {e.get('translated')!r} would still be drawn"


def test_stale_translations_are_cleared():
    m = _doc()
    m.pages[2].blocks[0].metadata["translated"] = "STALE VALUE"
    _quarantine_merged_pages(m, [2])
    translate_document(m, lambda t: t.upper(), thread=4)
    assert m.pages[2].blocks[0].metadata["translated"] == "p2 b0"


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------
def test_returns_the_number_of_marked_blocks():
    m = _doc(blocks=4)
    assert _quarantine_merged_pages(m, [0, 1]) == 8


def test_empty_page_number_list_marks_nothing():
    m = _doc()
    assert _quarantine_merged_pages(m, []) == 0
    translate_document(m, lambda t: t.upper(), thread=4)
    assert m.pages[0].blocks[0].metadata["translated"] == "P0 B0"


def test_unknown_page_number_is_ignored():
    m = _doc()
    assert _quarantine_merged_pages(m, [99]) == 0


def test_none_document_is_tolerated():
    assert _quarantine_merged_pages(None, [1]) == 0


def test_blocks_without_metadata_dict_do_not_crash():
    m = _doc(pages=1, blocks=1)
    m.pages[0].blocks[0].metadata = None
    assert _quarantine_merged_pages(m, [0]) == 0


def test_existing_translation_policy_is_preserved_not_clobbered():
    """已有 policy 时只翻 translate 标志，不能把别的键抹掉。"""
    m = _doc(pages=1, blocks=1)
    m.pages[0].blocks[0].metadata["translation_policy"] = {"source_text": "custom"}
    _quarantine_merged_pages(m, [0])
    pol = m.pages[0].blocks[0].metadata["translation_policy"]
    assert pol["translate"] is False
    assert pol["source_text"] == "custom"


# --------------------------------------------------------------------------
# 阈值：为什么是「占比」而不是「页数」
# --------------------------------------------------------------------------
def test_a_single_merged_page_never_triggers_whole_file_degrade():
    """7P0 的实测形态：50 页里 1 页折叠 = 2%，远低于阈值 → 必须走隔离。"""
    assert 1 / 50 < _MERGED_PAGE_DEGRADE_RATIO


def test_a_collapse_affecting_most_of_the_document_still_degrades():
    """整篇解析都不可信时，逐页隔离没有意义。"""
    assert 40 / 50 > _MERGED_PAGE_DEGRADE_RATIO
