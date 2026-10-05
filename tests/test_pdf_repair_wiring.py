"""``runtime_service`` 里「损坏源文件 → 自动修复副本」的接线。

这一层的价值全在**接线正确**上，逻辑本身在 :mod:`pdf2zh.pdf_repair`。所以这里
只钉那些「接错了就静默出事」的性质：

- 原件必须保持不变（引擎读副本，用户的东西不动）；
- ``enable_repair=False`` 必须真的关掉 —— 它此前是个从未被读取的死字段；
- 副本的**文件名**必须与原文件一致，否则结果下载名会从 ``<stem>-mono.pdf``
  变成 ``normalize-mono.pdf``（同一份输入每次名字还不一样）；
- 修复副本必须在任务结束时被回收；
- 诊断链路绝不能反过来把翻译搞挂。

测试直接绑定真实方法到一个 stub ``self`` 上：这些方法只用到 ``_emit_event`` 与
模块 logger，为测它们去构造整个服务（store / config / 线程）会引入无关失败。
"""

from __future__ import annotations

import os

import pymupdf
import pytest

from pdf2zh.pdf_repair import RepairOutcome
from pdf2zh.pdf_validity import strict_reader_check
from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
    TranslationRequest,
)


class _StubService:
    """只提供 ``_repair_sources_if_needed`` 需要的 ``self`` 表面。"""

    _repair_sources_if_needed = RuntimeService._repair_sources_if_needed
    _release_repaired_sources = RuntimeService._release_repaired_sources

    def __init__(self) -> None:
        self.events: list[str] = []

    def _emit_event(self, task_id, stage, progress, message) -> None:
        self.events.append(message)


def _plain(path, pages: int = 3):
    d = pymupdf.open()
    for i in range(pages):
        pg = d.new_page(width=300, height=300)
        pg.insert_text((30, 60), f"page {i}")
    d.save(str(path))
    d.close()
    return path


@pytest.fixture()
def truncated(tmp_path):
    p = _plain(tmp_path / "cut.pdf")
    p.write_bytes(p.read_bytes()[: int(len(p.read_bytes()) * 0.6)])
    return p


@pytest.fixture()
def healthy(tmp_path):
    return _plain(tmp_path / "ok.pdf")


def test_truncated_source_is_swapped_for_a_verified_copy(truncated):
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated))
    before = truncated.read_bytes()

    stub._repair_sources_if_needed("t1", req)
    try:
        assert req.source_path != str(truncated)
        assert os.path.isfile(req.source_path)
        assert strict_reader_check(req.source_path)[
            0
        ], "the copy handed to the engines must pass the strict reader gate"
        assert truncated.read_bytes() == before, "the user's original was rewritten"
        assert req.extra_config.get("_repair_copies") == [req.source_path]
    finally:
        stub._release_repaired_sources(req)


def test_enable_repair_false_really_disables_it(truncated):
    """``enable_repair`` 此前从未被任何代码读取过。"""
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated), enable_repair=False)

    stub._repair_sources_if_needed("t2", req)

    assert req.source_path == str(truncated)
    assert not (req.extra_config or {}).get("_repair_copies")


def test_healthy_source_is_not_rewritten(healthy):
    stub = _StubService()
    req = TranslationRequest(source_path=str(healthy))
    before = healthy.read_bytes()

    stub._repair_sources_if_needed("t3", req)

    assert req.source_path == str(healthy)
    assert healthy.read_bytes() == before
    assert not (req.extra_config or {}).get("_repair_copies")


def test_batch_files_are_replaced_too(truncated):
    """只换 ``source_path`` 会漏掉批量模式 —— 批量走 ``files``。"""
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated), files=[str(truncated)])

    stub._repair_sources_if_needed("t4", req)
    try:
        assert req.files and req.files[0] == req.source_path
        assert req.files[0] != str(truncated)
    finally:
        stub._release_repaired_sources(req)


def test_copy_preserves_the_source_filename(truncated):
    """结果文件名由源 stem 派生；副本改名会毁掉稳定的下载名。"""
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated))

    stub._repair_sources_if_needed("t5", req)
    try:
        assert os.path.basename(req.source_path) == truncated.name
    finally:
        stub._release_repaired_sources(req)


def test_release_reclaims_the_copy_and_its_directory(truncated):
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated))
    stub._repair_sources_if_needed("t6", req)

    copy = req.source_path
    owner = os.path.dirname(copy)
    stub._release_repaired_sources(req)

    assert not os.path.exists(copy)
    assert not os.path.exists(owner), "repair temp directory leaked"
    assert not (req.extra_config or {}).get(
        "_repair_copies"
    ), "the bookkeeping entry must be cleared so a second release is a no-op"


def test_release_is_safe_when_nothing_was_repaired():
    stub = _StubService()
    req = TranslationRequest(source_path="whatever.pdf")
    stub._release_repaired_sources(req)  # must not raise


def test_page_loss_is_surfaced_to_the_user(truncated):
    """丢页必须让用户看见 —— 译文会少掉那一页。

    断言的是**后果**而不是「page」这个词：``outcome.summary()`` 本身就含
    "1 page(s) dropped"，只查关键词的话，把追加的那句提示去掉测试照样通过
    （mutant 实测）。
    """
    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated))
    try:
        stub._repair_sources_if_needed("t7", req)
        joined = " ".join(stub.events)
        assert (
            "missing from the translation" in joined
        ), f"the consequence of the lost page was not surfaced; events={stub.events}"
    finally:
        stub._release_repaired_sources(req)


def test_a_lossless_repair_does_not_claim_pages_are_missing(tmp_path):
    """无丢页时不要报丢页 —— 假警报会让人以为译文缺内容。

    用结构损坏样本（3 页全在，只是 Catalog 违规）：它确实会被修、也确实不丢页，
    所以这条断言不是空跑。
    """
    import pikepdf

    src = _plain(tmp_path / "acroform.pdf")
    with pikepdf.open(str(src)) as pdf:
        pdf.Root["/AcroForm"] = pikepdf.Array([1])
        pdf.save(str(src) + ".tmp")
    os.replace(str(src) + ".tmp", str(src))

    stub = _StubService()
    req = TranslationRequest(source_path=str(src))
    try:
        stub._repair_sources_if_needed("t7b", req)
        assert stub.events, "nothing was emitted; the test would be vacuous"
        joined = " ".join(stub.events)
        assert "missing from the translation" not in joined, joined
    finally:
        stub._release_repaired_sources(req)


def test_repair_failure_never_blocks_translation(truncated, monkeypatch):
    """诊断链路抛异常不能让任务失败 —— 引擎自己会给出它的错误。"""
    import pdf2zh.pdf_repair as pr

    def boom(*a, **k):
        raise RuntimeError("pikepdf exploded")

    monkeypatch.setattr(pr, "repair_copy", boom)

    stub = _StubService()
    req = TranslationRequest(source_path=str(truncated))

    stub._repair_sources_if_needed("t8", req)  # must not raise

    assert req.source_path == str(truncated)


def test_unrepairable_source_keeps_the_original_path(tmp_path, monkeypatch):
    """修不好时保留原路径并继续：闸门已经在上游判过「真读不了」了。"""
    import pdf2zh.pdf_repair as pr

    src = str(_plain(tmp_path / "stub.pdf"))
    monkeypatch.setattr(
        pr,
        "repair_copy",
        lambda *a, **k: RepairOutcome(source=src, path=None, healthy=False),
    )

    stub = _StubService()
    req = TranslationRequest(source_path=src)
    stub._repair_sources_if_needed("t9", req)

    assert req.source_path == src
    assert not (req.extra_config or {}).get("_repair_copies")


# --------------------------------------------------------------------------
# 回收必须发生在真实执行路径的 finally 里
# --------------------------------------------------------------------------
# 只测 ``_release_repaired_sources`` 本身是不够的：把 ``_execute_task`` finally
# 里那行调用删掉，全部单测仍然通过，而每个损坏文件任务都会留下一个临时目录。
# 所以下面两条跑真实的 ``_execute_task``（把内核适配器换成会抛异常的桩），
# 走完它自己的 try/except/finally。
def test_failed_task_releases_the_repair_copy(truncated):
    svc = RuntimeService()
    tid = "t_rel_fail"
    svc._store.create_task(tid)
    req = TranslationRequest(source_path=str(truncated))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    svc._execute_legacy = _boom
    svc._execute_task(tid, req)

    copy = req.source_path
    assert copy != str(truncated), "the damaged source was not swapped for a copy"
    assert not os.path.exists(copy), "a FAILED task leaked its repair copy"
    assert svc._store.get_task(tid).status == TaskStage.FAILED.value


def test_cancelled_task_releases_the_repair_copy(truncated):
    """取消路径同样必须回收 —— 用户 Ctrl+C 是最常见的终止方式。"""
    svc = RuntimeService()
    tid = "t_rel_cancel"
    svc._store.create_task(tid)
    req = TranslationRequest(source_path=str(truncated))

    def _boom(*a, **k):
        raise KeyboardInterrupt("Parallel engine aborted: Ctrl+C received")

    svc._execute_legacy = _boom
    svc._execute_task(tid, req)

    copy = req.source_path
    assert copy != str(truncated)
    assert not os.path.exists(copy), "a CANCELLED task leaked its repair copy"
    assert svc._store.get_task(tid).status == TaskStage.CANCELLED.value
