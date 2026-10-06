"""``translate_document`` 的并发语义（doc/7p0 P1 剩余项）。

7P0 实测（``doc/7p0_real_load_report.md`` §4）：``--thread 4`` 在 magicpdf 路径
**完全无效** ——

    串行服务延迟（常驻 opencode serve，5 段不同文本）  9.7s 均值
    本次运行 585 块 / ~5280s                          9.0s 每块
    有效并发                                          1.07x

4 线程本该 ~24 分钟而不是 89 分钟。根因：``translate_document`` 是纯串行 for 循环，
线程池在 ``pdf2zh/v3/paragraph_batch.py``、只被 legacy ``converter.py`` 引用。

这些测试钉住的是**语义不变**，不是「更快」—— 快慢要真跑 89 分钟才知道，而串行版
和并发版必须产出**逐字相同**的结果，否则这个改动就是在拿正确性换速度。
"""

from __future__ import annotations

import threading
import time

import pytest

from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel
from pdf2zh.v3.document_model import DocumentModel, translate_document


def _block(text: str, kind: str = "paragraph", meta: dict | None = None) -> BlockModel:
    md: dict = {"font_size": 12.0}
    if meta:
        md.update(meta)
    return BlockModel(
        text=text,
        kind=kind,
        x0=72.0,
        y0=700.0,
        x1=540.0,
        y1=722.0,
        lines=[
            LineModel(text=text, baseline=0.0, x0=72.0, y0=700.0, x1=540.0, y1=722.0)
        ],
        metadata=md,
    )


def _doc(
    blocks_per_page: int = 12, pages: int = 3, kind: str = "paragraph"
) -> DocumentModel:
    model = DocumentModel()
    for p in range(pages):
        page = PageModel(page_num=p, width=595.0, height=842.0)
        page.blocks = [
            _block(f"page {p} block {i} text", kind) for i in range(blocks_per_page)
        ]
        model.pages.append(page)
    return model


def _upper(text: str) -> str:
    return text.upper()


def _snapshot(model: DocumentModel) -> list:
    """块级 (text, translated, kind, translate) 快照，用于逐字比对。"""
    return [
        (b.text, b.metadata.get("translated"), b.kind, b.metadata.get("translate"))
        for page in model.pages
        for b in page.blocks
    ]


# --------------------------------------------------------------------------
# 语义不变：并发 == 串行
# --------------------------------------------------------------------------
def test_concurrent_result_is_identical_to_serial():
    """并发版的块级产物必须与串行版逐字一致。

    这是本改动的核心约束。快慢另说，先保证没拿正确性换速度。
    """
    serial = _doc()
    conc = _doc()
    s_stats = translate_document(serial, _upper, thread=1)
    c_stats = translate_document(conc, _upper, thread=8)

    assert _snapshot(conc) == _snapshot(serial)
    assert c_stats == s_stats


def test_statistics_are_identical_and_ordered_deterministically():
    """统计必须按块序累加，而不是按线程完成序。

    按完成序累加在并发下是非确定的：同样的输入两次运行可能得到不同的
    ``translated``/``toc_translated`` 分布，于是基于统计的判定会随机翻转。
    """
    runs = [translate_document(_doc(), _upper, thread=8) for _ in range(5)]
    assert all(r == runs[0] for r in runs), f"non-deterministic stats: {runs}"
    assert runs[0] == translate_document(_doc(), _upper, thread=1)


def _toc_doc() -> DocumentModel:
    """TOC 与普通段混排，且 ``toc_translated`` 非零。

    只有 ``toc_translated`` 会因块序而变 —— 纯 paragraph 文档里每个块的
    ``kind`` 都一样，按完成序累加与按块序累加结果**完全相同**，于是上面那条
    测试在实现真的写错时也照样通过。这份样本让两个计数器落在不同的块上，
    顺序错乱才会显形。
    """
    model = DocumentModel()
    page = PageModel(page_num=0, width=595.0, height=842.0)
    blocks = []
    for i in range(9):
        if i % 3 == 1:
            blocks.append(
                _block(
                    f"Chapter {i} Something",
                    kind="toc",
                    meta={
                        "toc_entries": [
                            {"title": f"Section {i}.1 Intro", "page_number": str(i)},
                            {"title": f"Section {i}.2 Body", "page_number": str(i + 1)},
                        ]
                    },
                )
            )
        else:
            blocks.append(_block(f"plain paragraph {i}"))
    page.blocks = blocks
    model.pages.append(page)
    return model


def test_toc_heavy_statistics_survive_concurrency():
    """TOC/段落混排下统计仍与串行逐字一致（多次运行）。"""

    def _slow(text: str) -> str:
        time.sleep(0.005)
        return text.upper()

    serial = _toc_doc()
    s = translate_document(serial, _slow, thread=1)
    assert s["toc_translated"] > 0, "sample must exercise toc_translated"

    for _ in range(6):
        got = _toc_doc()
        c = translate_document(got, _slow, thread=8)
        assert c == s, f"concurrent stats diverged: {c} != {s}"
        assert [
            (b.metadata.get("translated"), len(b.metadata.get("toc_entries") or []))
            for b in got.pages[0].blocks
        ] == [
            (b.metadata.get("translated"), len(b.metadata.get("toc_entries") or []))
            for b in serial.pages[0].blocks
        ], "per-block payload assignment drifted between runs"


def test_thread_one_takes_the_serial_path():
    """thread<=1 时必须完全走串行（不建线程池）。"""

    def _boom(text):  # 若被并发执行，异常会被 unit 吞掉，这里察觉不到
        raise AssertionError("translate_fn called")

    model = _doc(blocks_per_page=2, pages=1)
    # 空文本块会被跳过，给一个非空块即可
    model.pages[0].blocks[0] = _block("only")
    translate_document(model, _upper, thread=1)
    assert model.pages[0].blocks[0].metadata["translated"] == "ONLY"


# --------------------------------------------------------------------------
# 真的并发了
# --------------------------------------------------------------------------
def test_translation_calls_actually_overlap():
    """四个各睡 0.2s 的调用在 4 线程下总耗时应接近 0.2s 而非 0.8s。

    这是「``--thread`` 现在真的生效」的可执行断言：把并发去掉，这个测试会
    从 ~0.2s 变成 ~0.8s。它也是唯一一个能在几秒内验证加速的测试 —— 真实
    服务要 89 分钟。
    """
    model = _doc(blocks_per_page=4, pages=1)
    barrier_hit = threading.Event()

    def _slow(text: str) -> str:
        if not barrier_hit.is_set():
            barrier_hit.set()
        time.sleep(0.2)
        return text.upper()

    t0 = time.perf_counter()
    translate_document(model, _slow, thread=4)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.6, (
        f"4 x 0.2s calls took {elapsed:.2f}s -- they did not overlap, so "
        f"--thread is still being ignored"
    )


def test_every_block_is_translated_exactly_once_under_concurrency():
    """并发下不能漏译或重复译。"""
    calls: list[str] = []
    lock = threading.Lock()

    def _counting(text: str) -> str:
        with lock:
            calls.append(text)
        return f"[{text}]"

    model = _doc(blocks_per_page=6, pages=4)
    translate_document(model, _counting, thread=8)

    expected = [b.text for page in model.pages for b in page.blocks]
    assert sorted(calls) == sorted(
        expected
    ), f"call count mismatch: {len(calls)} calls for {len(expected)} blocks"
    for page in model.pages:
        for b in page.blocks:
            assert b.metadata["translated"] == f"[{b.text}]"


# --------------------------------------------------------------------------
# 失败语义不变
# --------------------------------------------------------------------------
def test_a_failing_block_falls_back_to_source_and_does_not_abort_the_document():
    """单块翻译失败仍回落原文，且不影响其它块 —— 与串行版一致。"""
    model = _doc(blocks_per_page=5, pages=1)

    def _fail_on_second(text: str) -> str:
        if "block 2 " in text:
            raise RuntimeError("service exploded")
        return text.upper()

    stats = translate_document(model, _fail_on_second, thread=4)

    for b in model.pages[0].blocks:
        if "block 2 " in b.text:
            assert b.metadata["translated"] == b.text, "failed block lost its source"
        else:
            assert b.metadata["translated"] == b.text.upper()
    assert stats["translated"] >= 1


def test_a_raising_translation_unit_cannot_abort_the_whole_document(monkeypatch):
    """``block_translation_unit`` 自己会吞异常，但它**承诺**不抛 —— 承诺不是保证。

    这里是并发路径唯一的兜底：某个块的 unit 若真的抛了（未来的改动、或某个
    side-channel 忘了包 try），整篇 585 块的翻译会一起失败。这条测试用打桩强行
    让它抛，验证兜底真的在。没它的话，那段 except 是不可达的死代码，变异测试
    也无法证明它有效。
    """
    import pdf2zh.v3.render_payload as rp

    real = rp.block_translation_unit

    def _sometimes_raising(block, translate_fn, model=None):
        if "block 1 " in (block.text or ""):
            raise RuntimeError("unit exploded")
        return real(block, translate_fn, model=model)

    monkeypatch.setattr(rp, "block_translation_unit", _sometimes_raising)

    model = _doc(blocks_per_page=6, pages=1)
    stats = translate_document(model, _upper, thread=6)

    for b in model.pages[0].blocks:
        if "block 1 " in b.text:
            assert (
                b.metadata["translated"] == b.text
            ), "the raising block must fall back to its own source text"
            assert b.metadata["translate"] is False
        else:
            assert (
                b.metadata["translated"] == b.text.upper()
            ), "an unrelated block lost its translation"
    assert stats["translated"] >= 4


def test_preserve_kinds_are_not_sent_to_the_translator_under_concurrency():
    """公式/代码等保留块在并发下也绝不能进翻译器。"""
    sent: list[str] = []
    lock = threading.Lock()

    def _rec(text: str) -> str:
        with lock:
            sent.append(text)
        return "X"

    model = DocumentModel()
    page = PageModel(page_num=0, width=595.0, height=842.0)
    page.blocks = [
        _block("translate me"),
        _block("x = 1 + 2", kind="formula"),
        _block("print(1)", kind="code"),
        _block("Figure 1", kind="figure"),
        _block("CHAPTER 1", kind="header"),
        _block("translate me too"),
    ]
    model.pages.append(page)

    stats = translate_document(model, _rec, thread=6)

    assert sent == ["translate me", "translate me too"] or sorted(sent) == sorted(
        ["translate me", "translate me too"]
    ), f"a preserve-kind reached the translator: {sent}"
    assert stats["preserved"] == 4, stats


def test_empty_text_blocks_are_skipped_without_shifting_the_others():
    """空文本块不进翻译器，**且不能让它后面的块错位拿到别人的译文**。

    早先这条只断言「发出去的是哪些文本」和「首块没有 translated」，于是
    ``work`` 少一个过滤条件的变异体照样通过：空块被算进 ``units``，而主循环
    在跳过空块时**不推进 cursor**，后面的块就整体前移一格 —— ``real`` 会拿到
    空块的 skip unit，译文变成空串。这类错位在成品里表现为「某段突然空白」。
    """
    model = DocumentModel()
    page = PageModel(page_num=0, width=595.0, height=842.0)
    page.blocks = [_block(""), _block("   "), _block("real"), _block("second")]
    model.pages.append(page)

    sent: list[str] = []
    translate_document(model, lambda t: sent.append(t) or f"T:{t}", thread=4)

    assert sorted(sent) == ["real", "second"], f"unexpected translator input: {sent}"
    blocks = model.pages[0].blocks
    assert blocks[0].metadata.get("translated") is None, "empty block was translated"
    assert blocks[1].metadata.get("translated") is None, "blank block was translated"
    assert (
        blocks[2].metadata["translated"] == "T:real"
    ), "block 2 received another block's unit -- the unit cursor drifted"
    assert (
        blocks[3].metadata["translated"] == "T:second"
    ), "block 3 received another block's unit -- the unit cursor drifted"


# --------------------------------------------------------------------------
# 退化输入
# --------------------------------------------------------------------------
def test_single_block_document_is_handled():
    model = _doc(blocks_per_page=1, pages=1)
    stats = translate_document(model, _upper, thread=8)
    assert stats["translated"] == 1


def test_document_with_no_blocks_does_not_crash():
    model = DocumentModel()
    model.pages.append(PageModel(page_num=0, width=595.0, height=842.0))
    stats = translate_document(model, _upper, thread=8)
    assert stats["translated"] == 0


def test_worker_count_falls_back_to_env_when_thread_is_zero(monkeypatch):
    """thread=0 时读环境变量，两条引擎路径共用同一个旋钮。"""
    from pdf2zh.v3 import document_model as dm

    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH_THREADS", "7")
    assert dm._translate_workers(0) == 7
    # 显式值优先
    assert dm._translate_workers(3) == 3
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH_THREADS", "not-a-number")
    assert dm._translate_workers(0) >= 1


@pytest.mark.parametrize("thread", [1, 2, 4, 16])
def test_any_thread_count_gives_the_same_answer(thread):
    """任意并发度都与串行版一致 —— 基准也必须真的跑过串行。

    早先这里忘了给基准调 ``translate_document``，拿未翻译的模型当期望值，于是
    四个参数化用例全红而真正的并发 bug 一个都没测出来。
    """
    serial = _doc()
    translate_document(serial, _upper, thread=1)

    got = _doc()
    translate_document(got, _upper, thread=thread)

    assert _snapshot(got) == _snapshot(serial)
