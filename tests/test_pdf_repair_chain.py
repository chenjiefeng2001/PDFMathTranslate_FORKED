"""损坏 PDF 的检测与自动修复链（``pdf2zh/pdf_repair``）。

设计前提
--------
一条从没跑在真实损坏上的「修复链」只是一串愿望。所以这里的样本都是**真的**
被破坏过的文件，断言也来自实测而不是设计意图：

- ``truncated``  —— 尾部被截断。pikepdf 能开（只剩 2 页），PDFium 拒绝。
                     这是唯一一个能稳定复现「严格阅读器打不开」的合成损坏。
- ``unrecoverable`` —— 只剩文件头。libqpdf 直接报错，四级策略全部失败 ——
                     必须干净地报「修不好」，而不是悄悄交一份坏文件。
- ``structural`` —— Catalog ``/AcroForm`` 指向数组（违反 ISO 32000-1 §12.7.3）。

另外两条实测结论被写进了实现，这里钉住它们，因为它们都是**曾经写错**的地方：

1. **加密不等于损坏。** 空用户密码的加密 PDF 严格阅读器能正常打开。把它当损坏
   会导致每次都白跑一遍修复链，还会顺手把加密剥掉 —— 未经用户要求改变文档的
   安全语义。
2. **``/Type /Page`` 计数不能写成 ``bytes.count``**，它会连 ``/Type /Pages``
   （页树根）一起算进去，于是任何正常文档都被误报成「丢了一页」，健康文件
   甚至被当成损坏送去修复。
"""

from __future__ import annotations

import os
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from pdf2zh.pdf_repair import (
    detect_damage,
    release_repair,
    repair_copy,
)


# --------------------------------------------------------------------------
# 样本
# --------------------------------------------------------------------------
def _plain(path: Path, pages: int = 3) -> Path:
    d = pymupdf.open()
    for i in range(pages):
        pg = d.new_page(width=300, height=300)
        pg.insert_text((30, 60), f"sample page {i}")
    d.save(str(path))
    d.close()
    return path


@pytest.fixture()
def healthy(tmp_path):
    return _plain(tmp_path / "healthy.pdf")


@pytest.fixture()
def truncated(tmp_path):
    p = _plain(tmp_path / "cut.pdf")
    data = p.read_bytes()
    p.write_bytes(data[: int(len(data) * 0.6)])
    return p


@pytest.fixture()
def unrecoverable(tmp_path):
    p = _plain(tmp_path / "stub.pdf")
    p.write_bytes(p.read_bytes()[:110])
    return p


@pytest.fixture()
def structural(tmp_path):
    p = _plain(tmp_path / "acroform.pdf")
    with pikepdf.open(str(p)) as pdf:
        pdf.Root["/AcroForm"] = pikepdf.Array([1])
        pdf.save(str(p) + ".tmp")
    os.replace(str(p) + ".tmp", str(p))
    return p


@pytest.fixture()
def encrypted_empty_pw(tmp_path):
    p = _plain(tmp_path / "enc.pdf")
    with pikepdf.open(str(p)) as pdf:
        pdf.save(
            str(p) + ".tmp",
            encryption=pikepdf.Encryption(owner="own", user="", R=4),
        )
    os.replace(str(p) + ".tmp", str(p))
    return p


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------
def test_healthy_file_reports_nothing(healthy):
    assert detect_damage(str(healthy)) == []


def test_truncated_is_detected_with_its_lost_page(truncated):
    codes = {s.code for s in detect_damage(str(truncated))}
    assert "truncated" in codes, codes
    assert "strict" in codes, f"PDFium must be refusing it, signals={codes}"
    assert "page_loss" in codes, (
        "the truncation cost a page (3 -> 2); that loss must be visible, it is "
        "the dangerous kind of silent content loss"
    )


def test_unrecoverable_is_reported_as_such(unrecoverable):
    codes = {s.code for s in detect_damage(str(unrecoverable))}
    assert codes == {"unreadable"}, codes


def test_structural_signal_is_not_labelled_strict(structural):
    """pikepdf 的 ``inspect_pdf`` 把结构问题也折进 ``ok``。

    照抄它会把一个 Catalog 键类型错误误报成「浏览器打不开」—— 而 PDFium 151
    其实能打开（``pdf_validity`` 的模块文档记录过：真正拒绝的是 Edge 155）。
    """
    signals = detect_damage(str(structural))
    codes = {s.code for s in signals}
    assert "structural" in codes, codes
    assert (
        "strict" not in codes
    ), f"a Catalog key-type error was reported as a strict-reader failure: {signals}"


def test_encryption_alone_is_not_damage(encrypted_empty_pw):
    """空用户密码的加密 PDF 能被严格阅读器打开，不是损坏。

    当成损坏的后果：每次都白跑修复链，而且修复会把加密剥掉 —— 那是未经用户
    要求改变文档安全语义。
    """
    assert (
        detect_damage(str(encrypted_empty_pw)) == []
    ), "encryption must not be reported as damage"


def test_missing_file_is_reported_not_raised(tmp_path):
    codes = {s.code for s in detect_damage(str(tmp_path / "nope.pdf"))}
    assert codes == {"missing"}


def test_page_object_count_ignores_the_page_tree_node(tmp_path):
    """``/Type /Pages`` 是页树根，不是页。算进去会让健康文件误报丢页。"""
    from pdf2zh.pdf_repair import _raw_page_object_count

    p = _plain(tmp_path / "three.pdf", pages=3)
    assert _raw_page_object_count(str(p)) == 3, (
        "a 3-page file has 3 /Type /Page objects plus one /Type /Pages node; "
        "counting both reports a phantom lost page"
    )


# --------------------------------------------------------------------------
# 修复链
# --------------------------------------------------------------------------
def test_healthy_file_is_left_completely_alone(healthy):
    before = healthy.read_bytes()
    outcome = repair_copy(str(healthy))
    assert outcome.healthy is True
    assert outcome.repaired is False
    assert outcome.path is None
    assert healthy.read_bytes() == before, "a healthy source must not be rewritten"


def test_truncated_file_is_repaired_into_a_verified_copy(truncated):
    from pdf2zh.pdf_validity import strict_reader_check

    outcome = repair_copy(str(truncated))
    try:
        assert outcome.repaired, outcome.summary()
        assert outcome.path and os.path.isfile(outcome.path)
        ok, reason = strict_reader_check(outcome.path)
        assert (
            ok
        ), f"the copy must pass the very gate that flagged the original: {reason}"
        assert outcome.strategy
        # the original is untouched -- it belongs to the user
        assert strict_reader_check(str(truncated))[0] is False
    finally:
        release_repair(outcome.path)


def test_repair_reports_the_page_it_could_not_recover(truncated):
    outcome = repair_copy(str(truncated))
    try:
        assert outcome.repaired
        assert outcome.pages_lost >= 1, (
            "the truncated sample lost a page; the outcome must say so rather "
            "than presenting a short file as a faithful repair"
        )
        assert "page" in outcome.summary()
    finally:
        release_repair(outcome.path)


def test_unrecoverable_file_fails_loudly_and_silently_writes_nothing(unrecoverable):
    outcome = repair_copy(str(unrecoverable))
    assert outcome.healthy is False
    assert outcome.repaired is False
    assert outcome.path is None
    assert outcome.attempts, "every strategy must leave a trace of why it failed"
    assert all(not a.ok for a in outcome.attempts)
    assert all(a.detail for a in outcome.attempts), (
        "a failed attempt without a reason is exactly the 'just says repair "
        "failed' case this chain exists to avoid"
    )
    assert "UNREPAIRABLE" in outcome.summary()


def test_structural_damage_is_actually_gone_from_the_copy(structural):
    """采用的副本必须不再带结构缺陷。

    pikepdf 的 load+save **不会**自动纠正 ``/AcroForm -> []``（实测原样保留），
    所以这依赖 ``_normalize`` 里的 ``_sanitize_catalog``。而复检能挡住「修了却
    没修干净」，是因为闸门底下的 ``inspect_pdf`` 把 ``catalog_structure_problems``
    折进了 ``ok``。
    """
    from pdf2zh.pdf_validity import catalog_structure_problems

    assert list(catalog_structure_problems(str(structural))), "sample is not damaged"

    outcome = repair_copy(str(structural))
    try:
        assert outcome.repaired, outcome.summary()
        remaining = list(catalog_structure_problems(outcome.path))
        assert not remaining, f"adopted copy still carries {remaining}"
    finally:
        release_repair(outcome.path)


def test_the_cheapest_sufficient_strategy_wins(structural):
    """够用就停在最轻的一级。

    mutant 实测：去掉 ``_sanitize_catalog`` 后，``normalize`` / ``rebuild`` /
    ``ignore_xref_streams`` 的产物都被复检拒绝，最后 ``salvage_pages`` 胜出 ——
    结果**不算错**（新建文档天然没有坏 Catalog），但那一级会把整份文档重建，
    大纲/书签/AcroForm 这些文档级结构全丢。静默降级到这个代价，必须钉住。
    """
    outcome = repair_copy(str(structural))
    try:
        assert outcome.repaired
        assert outcome.strategy == "normalize", (
            f"one rewrite was enough, but the chain fell through to "
            f"{outcome.strategy} (rebuilds the document, loses outlines)"
        )
        assert (
            len(outcome.attempts) == 1
        ), f"more strategies than necessary ran: {outcome.attempts}"
    finally:
        release_repair(outcome.path)


def test_a_failing_cheapest_strategy_does_not_end_the_chain(truncated, monkeypatch):
    """第一级抛异常时链必须继续升级，而不是就地放弃。

    mutant 实测：把循环改成 ``catalog[:1]``，只要第一级总是成功就完全看不出来 ——
    而那正是「便宜的修法不灵」的情形，此时放弃等于白修。
    """
    import pdf2zh.pdf_repair as pr

    def _boom(*a, **k):
        raise RuntimeError("normalize is unavailable")

    monkeypatch.setattr(pr, "_normalize", _boom)

    outcome = pr.repair_copy(str(truncated))
    try:
        assert (
            outcome.repaired
        ), "the chain gave up even though a later strategy could still work"
        assert outcome.strategy != "normalize"
        first = outcome.attempts[0]
        assert first.strategy == "normalize"
        assert not first.ok
        assert (
            "normalize is unavailable" in first.detail
        ), "the failure reason must survive so the log can explain itself"
    finally:
        release_repair(outcome.path)


def test_every_attempt_is_rechecked_before_being_accepted(truncated, monkeypatch):
    """每一级的产物都必须复检。不复检 = 一个没验证过的文件直接进引擎。

    验证走的是**判坏那同一个闸门**：按别的标准验收就会出现「按 A 判坏、按 B 判好」
    —— 产物在闸门这里仍然打不开，却已经交给只吃字节的引擎了。
    """
    import pdf2zh.pdf_repair as pr

    calls: list[str] = []
    real = pr._verify_repaired

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(pr, "_verify_repaired", spy)

    outcome = pr.repair_copy(str(truncated))
    try:
        assert outcome.repaired
        assert calls, "nothing was verified; an unverified file would be shipped"
        assert outcome.path in calls, "the adopted copy was never verified"
    finally:
        release_repair(outcome.path)


def test_nothing_is_adopted_when_the_gate_refuses_everything(truncated, monkeypatch):
    """闸门全部拒绝时必须交白卷，而不是采用最后一份产物。"""
    import pdf2zh.pdf_repair as pr

    monkeypatch.setattr(pr, "_verify_repaired", lambda p: (False, "mutant"))

    outcome = pr.repair_copy(str(truncated))

    assert outcome.repaired is False
    assert outcome.path is None
    assert outcome.attempts
    assert all(not a.ok for a in outcome.attempts)
    assert all(
        "mutant" in a.detail for a in outcome.attempts
    ), "the reason the gate gave must survive into the attempt record"


def test_chain_escalates_until_one_verifies(truncated, monkeypatch):
    """前几级必须先跑，且失败会记录 —— 不能一上来就用最重的那级。"""
    calls: list[str] = []

    import pdf2zh.pdf_repair as pr

    catalog = pr._strategy_catalog()

    def wrap(name, fn):
        def inner(src, dst):
            calls.append(name)
            return fn(src, dst)

        return inner

    monkeypatch.setattr(
        pr, "_strategy_catalog", lambda: [(n, wrap(n, f)) for n, f in catalog]
    )

    outcome = pr.repair_copy(str(truncated))
    try:
        assert outcome.repaired
        assert calls, "no strategy ran"
        first = calls[0]
        assert (
            first == "normalize"
        ), f"the cheapest strategy must be tried first, got {calls}"
    finally:
        release_repair(outcome.path)


def test_strategy_subset_is_honoured(truncated):
    outcome = repair_copy(str(truncated), strategies=["salvage_pages"])
    try:
        if outcome.repaired:
            assert outcome.strategy == "salvage_pages"
        else:
            assert [a.strategy for a in outcome.attempts] == ["salvage_pages"]
    finally:
        release_repair(outcome.path)


def test_repair_can_be_disabled(truncated):
    outcome = repair_copy(str(truncated), enabled=False)
    assert outcome.healthy is False
    assert outcome.repaired is False
    assert outcome.attempts == ()
    assert outcome.signals, "detection still runs when repair is off"


def test_copy_keeps_the_original_filename(truncated):
    """结果文件名由源 stem 派生；副本改名会把产物叫成 ``normalize-mono.pdf``，
    也会让同一份输入每次得到不同的下载名。"""
    outcome = repair_copy(str(truncated))
    try:
        assert outcome.repaired
        assert os.path.basename(outcome.path) == os.path.basename(str(truncated))
    finally:
        release_repair(outcome.path)


def test_release_reclaims_copy_and_directory(truncated):
    outcome = repair_copy(str(truncated))
    assert outcome.repaired
    owner = os.path.dirname(outcome.path)
    release_repair(outcome.path)
    assert not os.path.exists(outcome.path)
    assert not os.path.exists(owner), "temp directory leaked"


def test_release_refuses_paths_it_does_not_own(tmp_path):
    """清理函数绝不能有能力删掉调用方自己的文件。"""
    keep_dir = tmp_path / "not-ours"
    keep_dir.mkdir()
    keep = keep_dir / "precious.pdf"
    keep.write_bytes(b"%PDF-1.4 important")

    release_repair(str(keep))

    assert keep.exists(), "release deleted a file it did not create"
    assert keep_dir.is_dir()


def test_release_is_idempotent_and_none_safe(truncated):
    outcome = repair_copy(str(truncated))
    release_repair(None)
    release_repair("")
    release_repair(outcome.path)
    release_repair(outcome.path)
    assert not os.path.exists(outcome.path)


def test_no_temp_directories_leak_on_failure(unrecoverable, tmp_path):
    before = {p for p in Path(os.environ.get("TEMP", "/tmp")).glob("pdf2zh_repair_*")}
    repair_copy(str(unrecoverable))
    after = {p for p in Path(os.environ.get("TEMP", "/tmp")).glob("pdf2zh_repair_*")}
    assert after == before, f"a failed chain leaked {after - before}"


def test_the_original_is_never_modified(truncated, structural):
    """无论修不修得动，用户的原文件都必须一个字节都不变。"""
    for src in (truncated, structural):
        before = src.read_bytes()
        outcome = repair_copy(str(src))
        try:
            assert src.read_bytes() == before, f"{src.name} was modified"
        finally:
            release_repair(outcome.path)
