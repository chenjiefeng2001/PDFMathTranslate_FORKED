"""MinerU 摄入路径的 PDFium 可读性（``pdf2zh/magicpdf_adapter`` +
``pdf2zh/pdf_validity``）。

缺陷
----
MinerU 3.x 的 ``pipeline`` 后端靠 **PDFium** 渲染页面图像，而 PDFium 比本工具
主链路（MuPDF/pikepdf）严格得多。对「``/Encrypt`` 只挂在 XRef 流上、传统
trailer 里没有」的混合引用文件：

    pikepdf   -> OK（当成未加密文档）
    MuPDF     -> OK（is_encrypted=False）
    PDFium    -> FPDF_ERR_SECURITY「Unsupported security scheme」

入口闸门 :func:`ensure_source_usable` 因此只发一条 ``warn``「可以继续翻译」，
但 MinerU 拿**同一份原始字节**直接崩在 ``open_pdfium_document`` ——
告警说能继续、引擎却根本打不开，两边自相矛盾。实测那份 325 页源文件就是这样
既没翻译出内容、又没有明确报错。

修法
----
原件不动（源文件属于用户，见 ``runtime_service._warn_unreadable_source`` 的
说明），另给一份规范化副本给这类「只能用 PDFium」的消费者
（:func:`pdf_validity.normalized_copy`）。只在本就健康时才零成本通过。

样本用 ``test_pdf_validity_gate._hybrid_encrypt_pdf`` 合成，不依赖本机上任何
特定文件；闸门侧依赖 PDFium 时整体 skip（降级行为另有测试覆盖）。
"""

import hashlib
import json
import os
import sys
import types

import pytest

from pdf2zh import pdf_validity
from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.pdf_validity import (
    normalized_copy,
    release_normalized_copy,
    repair_pdf,
    strict_reader_check,
)

HAVE_PDFIUM = pdf_validity._pdfium() is not None

pytestmark = pytest.mark.skipif(
    not HAVE_PDFIUM,
    reason="pypdfium2 unavailable; normalized_copy is a no-op by design",
)


# --------------------------------------------------------------------------
# 样本与工具（样本构造在 tests/conftest.py：``hybrid_xref_pdf`` /
# ``healthy_pdf`` 两个 fixture 是唯一实现）
# --------------------------------------------------------------------------
@pytest.fixture()
def no_mineru_override(monkeypatch):
    """强制走主进程分支：本机若探测到隔离 venv 会自动改走子进程。"""
    import pdf2zh.engine_env as _ee

    monkeypatch.setattr(_ee, "mineru_python_override", lambda: None)
    monkeypatch.delenv("PDF2ZH_MINERU_PYTHON", raising=False)
    monkeypatch.delenv("PDF2ZH_MINERU_VENV_DIR", raising=False)


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _all_text(path):
    import pymupdf

    doc = pymupdf.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


# --------------------------------------------------------------------------
# normalized_copy：只读 PDF 拒绝时给出可读副本，原件一个字不动
# --------------------------------------------------------------------------
def test_hybrid_xref_sample_really_is_the_reported_defect(hybrid_xref_pdf):
    """先确认样本还复现缺陷，否则下面所有用例都在测一个假的问题。"""
    import pikepdf

    with pikepdf.open(hybrid_xref_pdf) as pdf:
        assert len(pdf.pages) >= 1, "pikepdf 必须能开，否则就不是本 bug 的样本"
    ok, _reason = strict_reader_check(hybrid_xref_pdf)
    assert not ok, "样本必须 PDFium 打不开，否则这个测试文件失去意义"


def test_unreadable_pdf_yields_a_pdfium_readable_copy(hybrid_xref_pdf):
    copy_path = normalized_copy(hybrid_xref_pdf)
    try:
        assert copy_path, "PDFium 读不了的源必须给出规范化副本，而不是 None"
        ok, reason = strict_reader_check(copy_path)
        assert ok, f"副本必须 PDFium 可读，实测仍然打不开：{reason}"
    finally:
        release_normalized_copy(copy_path)


def test_original_is_left_byte_for_byte_untouched(hybrid_xref_pdf):
    """源文件属于用户。规范化只能发生在副本上。"""
    before = _digest(hybrid_xref_pdf)
    size_before = os.path.getsize(hybrid_xref_pdf)

    copy_path = normalized_copy(hybrid_xref_pdf)
    try:
        assert copy_path, "样本不可读，必须产出副本"
        assert _digest(hybrid_xref_pdf) == before, "原件内容被改动了"
        assert os.path.getsize(hybrid_xref_pdf) == size_before, "原件体积被改动了"
        # 关键：原件**仍然**不可读 —— 说明修的是副本，不是「顺手把用户的
        # 文件就地治好」而用户毫不知情。
        assert not strict_reader_check(hybrid_xref_pdf)[
            0
        ], "原件竟被就地修好了 —— 这违反「不改写用户输入」的既定设计"
    finally:
        release_normalized_copy(copy_path)


def test_copy_keeps_the_original_filename(hybrid_xref_pdf):
    """副本会作为 ``pdf_file_names`` 流进 MinerU，决定中间产物的目录层级；
    用随机临时名的话，排查时看到的是 ``.pdf2zh_norm_xxxx`` 这种没法认的东西。"""
    copy_path = normalized_copy(hybrid_xref_pdf)
    try:
        assert os.path.basename(copy_path) == os.path.basename(hybrid_xref_pdf), (
            f"副本文件名应为 {os.path.basename(hybrid_xref_pdf)!r}，"
            f"实得 {os.path.basename(copy_path)!r}"
        )
    finally:
        release_normalized_copy(copy_path)


def test_copy_preserves_the_text_layer(tmp_path, hybrid_xref_pdf):
    """规范化是为了让 PDFium 能开，不是重新生成内容 —— 少一个字就是数据丢失。

    基准取「施加混合引用手术**之前**」的那份正常 PDF：那正是规范化必须原样
    保住的文本层。样本自身带 ``/Encrypt``，连 MuPDF 都读不出它的文本
    （``encryption dictionary missing owner password``），拿它当基准不可行。
    """
    from tests.conftest import plain_pdf

    baseline = plain_pdf(str(tmp_path / "baseline.pdf"))
    expected = _all_text(baseline)
    assert expected.strip(), "基准 PDF 本身没有文本，用例会失去意义"

    copy_path = normalized_copy(hybrid_xref_pdf)
    try:
        assert copy_path, "样本不可读，必须产出副本"
        assert _all_text(copy_path) == expected, (
            f"规范化后的文本层与基准不符：\n--- 基准 ---\n{expected!r}\n"
            f"--- 副本 ---\n{_all_text(copy_path)!r}"
        )
    finally:
        release_normalized_copy(copy_path)


def test_healthy_pdf_is_not_rewritten(healthy_pdf):
    """健康文件必须原样通过：探测是载入期检查（约 20ms），load+save 要全量
    解析。对健康文件重写是纯浪费，还会平白改动用户文件的内容。"""
    before = _digest(healthy_pdf)
    assert (
        normalized_copy(healthy_pdf) is None
    ), "PDFium 已经能读的文件不应产出规范化副本"
    assert _digest(healthy_pdf) == before


def test_copy_is_confined_to_a_private_temp_dir(hybrid_xref_pdf):
    copy_path = normalized_copy(hybrid_xref_pdf)
    try:
        parent = os.path.basename(os.path.dirname(copy_path))
        assert parent.startswith(
            "pdf2zh_norm_"
        ), f"副本应放在 pdf2zh_norm_* 私有目录里，实得 {parent!r}"
    finally:
        release_normalized_copy(copy_path)


@pytest.mark.parametrize(
    "content", [b"", b"not a pdf at all", b"%PDF-1.4 truncated", b"\x00" * 64]
)
def test_garbage_input_returns_none_without_raising(tmp_path, content):
    """规范化是尽力而为的补救：失败就退回原件，绝不让翻译任务崩在这里。"""
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(content)
    assert normalized_copy(str(junk)) is None


def test_missing_input_returns_none(tmp_path):
    assert normalized_copy(str(tmp_path / "nope.pdf")) is None
    assert normalized_copy("") is None
    assert normalized_copy(None) is None


# --------------------------------------------------------------------------
# release_normalized_copy：必须回收干净，且无权删除调用方的文件
# --------------------------------------------------------------------------
def test_release_removes_both_the_copy_and_its_directory(hybrid_xref_pdf):
    copy_path = normalized_copy(hybrid_xref_pdf)
    owner_dir = os.path.dirname(copy_path)
    release_normalized_copy(copy_path)
    assert not os.path.exists(copy_path), "副本文件没删掉"
    assert not os.path.exists(owner_dir), f"临时目录泄漏了：{owner_dir}"


def test_release_is_idempotent_and_none_safe(hybrid_xref_pdf):
    copy_path = normalized_copy(hybrid_xref_pdf)
    release_normalized_copy(None)
    release_normalized_copy("")
    release_normalized_copy(copy_path)
    release_normalized_copy(copy_path)  # 第二次不得抛
    assert not os.path.exists(copy_path)


def test_release_refuses_paths_it_does_not_own(tmp_path):
    """清理函数绝不能有能力删掉调用方自己的文件。"""
    keep_dir = tmp_path / "not-ours"
    keep_dir.mkdir()
    keep = keep_dir / "precious.pdf"
    keep.write_bytes(b"%PDF-1.4 important")

    release_normalized_copy(str(keep))

    assert keep.exists(), "release 删掉了不属于它的目录下的文件"
    assert keep_dir.is_dir(), "release 删掉了不属于它的目录"


# --------------------------------------------------------------------------
# repair_pdf 未被重构破坏（原地重写那条老路）
# --------------------------------------------------------------------------
def test_repair_pdf_still_fixes_in_place(hybrid_xref_pdf):
    before = _digest(hybrid_xref_pdf)
    assert repair_pdf(hybrid_xref_pdf) is True, "原地修复失效"
    assert strict_reader_check(hybrid_xref_pdf)[0], "修复后 PDFium 仍打不开"
    assert _digest(hybrid_xref_pdf) != before, "文件内容没变，说明根本没重写"


def test_repair_pdf_leaves_no_temp_files_behind(hybrid_xref_pdf):
    """回归护栏：规范化核心被提取成共享函数后，``repair_pdf`` 依赖它返回的
    临时路径做 ``os.replace``。若那个返回值在 ``finally`` 里被提前删掉，
    修复会静默失败。"""
    siblings = set(os.listdir(os.path.dirname(hybrid_xref_pdf)))
    assert repair_pdf(hybrid_xref_pdf) is True
    assert (
        set(os.listdir(os.path.dirname(hybrid_xref_pdf))) == siblings
    ), "原地修复留下了临时文件残留"


def test_repair_pdf_refuses_unreadable_garbage(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    assert repair_pdf(str(junk)) is False


# --------------------------------------------------------------------------
# 接线：引擎拿到的必须是可读副本（这条才是真正的回归护栏）
# --------------------------------------------------------------------------
def _install_fake_mineru(monkeypatch):
    """注入假 ``mineru.cli.common``，并把 ``do_parse`` 实参记下来。"""
    captured = {}

    def do_parse(**kwargs):
        captured.update(kwargs)
        out = kwargs["output_dir"]
        stem = kwargs["pdf_file_names"][0]
        nested = os.path.join(out, stem, "auto")
        os.makedirs(nested, exist_ok=True)
        with open(
            os.path.join(nested, f"{stem}_middle.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump({"pdf_info": [{"page_idx": 0}]}, fh)

    def read_fn(path):
        with open(path, "rb") as fh:
            return fh.read()

    common = types.ModuleType("mineru.cli.common")
    common.do_parse = do_parse
    common.read_fn = read_fn
    monkeypatch.setitem(sys.modules, "mineru.cli", types.ModuleType("mineru.cli"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", common)
    return captured


def test_mineru_receives_bytes_it_can_actually_open(
    hybrid_xref_pdf, tmp_path, monkeypatch, no_mineru_override
):
    """本 bug 的核心断言：喂给 MinerU 的**字节**必须 PDFium 能打开。

    只断言「某个规范化函数被调用了」是不够的 —— 真正的故障发生在字节流入
    ``do_parse`` 的那一刻，所以这里把字节落盘后再走一次真实闸门。
    """
    captured = _install_fake_mineru(monkeypatch)

    MagicPdfAdapter()._parse_mineru(hybrid_xref_pdf)

    assert captured, "do_parse 没被调用"
    pdf_bytes = captured["pdf_bytes_list"][0]
    probe = tmp_path / "given_to_mineru.pdf"
    probe.write_bytes(pdf_bytes)

    ok, reason = strict_reader_check(str(probe))
    assert ok, f"喂给 MinerU 的字节 PDFium 打不开（这正是本 bug）：{reason}"
    assert not strict_reader_check(hybrid_xref_pdf)[
        0
    ], "样本若已可读，本用例就没有验证到任何东西"


def test_engine_gets_the_copy_not_the_original(
    hybrid_xref_pdf, monkeypatch, no_mineru_override
):
    """副本用完即删，所以路径必须在解析期间真实存在。"""
    seen = {}

    def fake_source(self, pdf_path, **kwargs):
        seen["path"] = pdf_path
        seen["exists_during_parse"] = os.path.isfile(pdf_path)
        # 必须在**解析期间**判定：解析返回后 finally 已经把副本回收了，
        # 事后再去检查只会得到「文件不存在」。
        seen["readable_during_parse"] = strict_reader_check(pdf_path)[0]
        return []

    monkeypatch.setattr(MagicPdfAdapter, "_parse_mineru_source", fake_source)
    MagicPdfAdapter()._parse_mineru(hybrid_xref_pdf)

    assert seen[
        "exists_during_parse"
    ], "解析期间引擎拿到的路径不存在 —— 副本被过早删除或根本没建"
    assert (
        seen["path"] != hybrid_xref_pdf
    ), "引擎仍拿到原始不可读文件，规范化没有接线到调用点"
    assert seen["readable_during_parse"], "引擎拿到的文件 PDFium 打不开"


def test_healthy_source_is_passed_through_verbatim(
    healthy_pdf, monkeypatch, no_mineru_override
):
    """健康文件不能被换成一个副本：多一次全量 load+save，且用户拿到的中间
    产物名字会莫名多一层临时目录。"""
    seen = {}

    def fake_source(self, pdf_path, **kwargs):
        seen["path"] = pdf_path
        return []

    monkeypatch.setattr(MagicPdfAdapter, "_parse_mineru_source", fake_source)
    MagicPdfAdapter()._parse_mineru(healthy_pdf)

    assert seen["path"] == healthy_pdf, f"健康文件应原样传入，实得 {seen['path']!r}"


def test_copy_is_released_after_success(
    hybrid_xref_pdf, monkeypatch, no_mineru_override
):
    seen = {}

    def fake_source(self, pdf_path, **kwargs):
        seen["path"] = pdf_path
        return []

    monkeypatch.setattr(MagicPdfAdapter, "_parse_mineru_source", fake_source)
    MagicPdfAdapter()._parse_mineru(hybrid_xref_pdf)

    assert not os.path.exists(seen["path"]), "成功后副本没被回收"
    assert not os.path.exists(os.path.dirname(seen["path"])), "临时目录泄漏"


def test_copy_is_released_even_when_the_engine_explodes(
    hybrid_xref_pdf, monkeypatch, no_mineru_override
):
    """引擎抛异常时也必须回收 —— 失败的任务同样会留下临时目录。"""
    seen = {}

    def boom(self, pdf_path, **kwargs):
        seen["path"] = pdf_path
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(MagicPdfAdapter, "_parse_mineru_source", boom)
    with pytest.raises(RuntimeError, match="engine exploded"):
        MagicPdfAdapter()._parse_mineru(hybrid_xref_pdf)

    assert not os.path.exists(seen["path"]), "引擎失败后副本没被回收（finally 丢失）"
    assert not os.path.exists(os.path.dirname(seen["path"])), "临时目录泄漏"


def test_subprocess_branch_also_gets_the_copy(hybrid_xref_pdf, monkeypatch):
    """子进程路径（``PDF2ZH_MINERU_PYTHON`` 指向隔离 venv）才是出事的那个：
    worker 只吃路径，原件一路直达 MinerU。"""
    import pdf2zh.engine_env as _ee

    monkeypatch.setattr(_ee, "mineru_python_override", lambda: sys.executable)

    seen = {}

    def fake_sub(self, pdf_path, **kwargs):
        seen["path"] = pdf_path
        seen["exists_during_parse"] = os.path.isfile(pdf_path)
        seen["readable_during_parse"] = strict_reader_check(pdf_path)[0]
        return []

    monkeypatch.setattr(MagicPdfAdapter, "_parse_mineru_subprocess", fake_sub)
    MagicPdfAdapter()._parse_mineru(hybrid_xref_pdf)

    assert seen["exists_during_parse"], "子进程拿到的路径不存在"
    assert seen["path"] != hybrid_xref_pdf, "子进程仍拿到原始不可读文件"
    assert seen["readable_during_parse"], "子进程拿到的文件 PDFium 打不开"
    assert not os.path.exists(seen["path"]), "子进程路径未回收副本"
