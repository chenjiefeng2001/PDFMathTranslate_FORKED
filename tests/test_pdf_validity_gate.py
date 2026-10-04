"""产物 PDF 的严格阅读器闸门（``pdf2zh/pdf_validity``）。

背景
----
产物落盘后各链路原本只校验 ``isfile`` + ``st_size > 0``。但字节数大于 0 与
「Chrome/Edge 能打开」是两件事：PDFium（浏览器内置阅读器内核）对同一份字节的
判定比 MuPDF / pikepdf 严格得多。已实证的失效形态是**无效的混合引用文件** ——
``/Encrypt`` 只挂在 ``/XRef`` 流上、传统 ``trailer`` 里没有 ``/Encrypt``：

    pikepdf   -> OK（当成未加密文档）
    MuPDF     -> OK（is_encrypted=False）
    PDFium    -> FPDF_ERR_SECURITY「Unsupported security scheme」

用户侧表现就是「翻译完的 PDF 一用 Chrome 打开就报文件格式损坏」。

测试里用一个**合成**样本（``_hybrid_encrypt_pdf``）复现该形态，不依赖本机上
任何特定文件。
"""

import os
import tempfile
import unittest
import zlib
from unittest.mock import patch

import pikepdf
import pymupdf

from pdf2zh import pdf_validity
from pdf2zh.pdf_validity import (
    ensure_readable,
    repair_pdf,
    source_readability_warning,
    strict_reader_check,
)

HAVE_PDFIUM = pdf_validity._pdfium() is not None


def _plain_pdf(path, pages=1, text="hello"):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=300, height=300)
        page.insert_text((30, 60 + i * 20), f"{text} {i}")
    doc.save(path)
    doc.close()
    return path


def _hybrid_encrypt_pdf(path):
    """pikepdf 能开、PDFium 拒绝的样本。

    做法：给一份正常 PDF 追加一个 /Encrypt 字典，再在 trailer 里加
    ``/XRefStm`` 指向一个携带 ``/Encrypt`` 的 /XRef 流。严格阅读器据此认为
    文档已加密却又建不起 security handler，于是在载入阶段拒绝。
    """
    _plain_pdf(path)
    data = open(path, "rb").read()

    enc_obj = (
        b"1023 0 obj\n<</Filter/Standard/V 4/R 4/Length 128/P -3904"
        b"/O <" + b"0" * 32 + b">/U <" + b"0" * 32 + b">>>\nendobj\n"
    )
    rows = b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")
    comp = zlib.compress(rows)
    xref_stream_obj = (
        b"1024 0 obj\n<</Type/XRef/Size 1025/W [1 4 2]/Root 1 0 R"
        b"/Encrypt 1023 0 R/Filter/FlateDecode/Length "
        + str(len(comp)).encode()
        + b">>\nstream\n"
        + comp
        + b"\nendstream\nendobj\n"
    )

    ti = data.rfind(b"trailer")
    body, trailer = data[:ti], data[ti:]
    sx = trailer.find(b"startxref")
    if sx >= 0:
        trailer = trailer[:sx]
    new_trailer = trailer.replace(b">>", b"/XRefStm 999999>>", 1)
    open(path, "wb").write(
        body + enc_obj + xref_stream_obj + new_trailer + b"startxref\n999999\n%%EOF\n"
    )
    return path


def _text_stub_pdf(path):
    """V4 ``PDFRenderer`` 的真实产物形状：以 ``%PDF-`` 开头但没有 xref。"""
    with open(path, "wb") as fh:
        fh.write(b"%PDF-page num=1 w=612 h=792\nthis is not a real pdf\n")
    return path


def _page_count(path):
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def _all_text(path):
    doc = pymupdf.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


@unittest.skipUnless(
    HAVE_PDFIUM, "pypdfium2 unavailable; gate degrades to pass-through"
)
class TestPdfiumOracleIsInstalled(unittest.TestCase):
    """PDFium 是**声明依赖**，不是可选增强。

    它此前只是 ``mineru``（``magicpdf`` extra）和 ``pdftext`` 的传递依赖，
    默认安装与 CI 的 ``uv sync`` 都没有。于是闸门退化成「只看结构」、
    ``repair_pdf`` 全部拒绝改写，而 CI 里 9 个修复测试就是这么红的 ——
    本机（装了 magicpdf extra）永远绿。

    降级行为本身有测试（``test_degrades_to_pass_through_without_pdfium``、
    ``test_repair_refuses_to_rewrite_without_pypdfium2``），所以这里防的是
    「依赖被悄悄摘掉、没人发现」。
    """

    def test_pypdfium2_is_importable(self):
        self.assertIsNotNone(
            pdf_validity._pdfium(),
            "pypdfium2 is a declared pdf2zh dependency; without it the strict "
            "reader gate has no browser oracle and repair_pdf refuses to run",
        )

    def test_it_is_declared_in_pyproject(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            self.assertIn("pypdfium2", fh.read())


class TestStrictReaderCheck(unittest.TestCase):
    def test_valid_pdf_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "ok.pdf"), pages=3)
            ok, reason = strict_reader_check(path)
            self.assertTrue(ok, reason)
            self.assertEqual(reason, "")

    def test_v4_text_stub_is_rejected(self):
        """``%PDF-`` 前缀骗得过 isfile+size，但骗不过 PDFium。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _text_stub_pdf(os.path.join(tmp, "stub.pdf"))
            self.assertTrue(open(path, "rb").read().startswith(b"%PDF-"))
            ok, reason = strict_reader_check(path)
            self.assertFalse(ok)
            self.assertTrue(reason)

    def test_hybrid_encrypt_file_is_rejected(self):
        """核心形态：pikepdf 放行、严格阅读器拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _hybrid_encrypt_pdf(os.path.join(tmp, "hyb.pdf"))
            with pikepdf.open(path) as pdf:  # pikepdf 确实能开
                self.assertEqual(len(pdf.pages), 1)
            ok, reason = strict_reader_check(path)
            self.assertFalse(
                ok, "pikepdf 能开的文件被严格阅读器拒绝了 —— 正是本闸门要抓的"
            )
            self.assertTrue(reason)

    def test_missing_file_is_rejected_without_raising(self):
        ok, reason = strict_reader_check("/nonexistent/definitely-not-here.pdf")
        self.assertFalse(ok)
        self.assertIn("not found", reason)

    def test_deep_mode_also_passes_on_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "ok.pdf"), pages=4)
            self.assertTrue(strict_reader_check(path, deep=True)[0])

    def test_degrades_to_pass_through_without_pdfium(self):
        """pypdfium2 缺失时闸门必须放行，不能阻断翻译。

        契约已细化为：不再整体放行，而是**只跑**不依赖 PDFium 的结构校验。
        结构没问题就放行 —— 仍然不阻断翻译。

        反向保证（缺 PDFium 时结构非法**仍要**拦下）由
        ``tests/test_pdf_catalog_types.py::TestStrictReaderGateCatchesArrayAcroForm::
        test_structural_gate_works_without_pdfium`` 覆盖。
        """
        with tempfile.TemporaryDirectory() as tmp:
            good = _plain_pdf(os.path.join(tmp, "good.pdf"))
            with patch.object(pdf_validity, "_pdfium", return_value=None):
                self.assertEqual(strict_reader_check(good), (True, ""))

    def test_missing_file_is_reported_regardless_of_pdfium(self):
        """文件不存在是硬事实，不该因为 PDFium 缺失而被当成「放行」。

        旧实现在 PDFium 缺失时直接 ``returnTrue, ""``，连存在性都不查。
        """
        with patch.object(pdf_validity, "_pdfium", return_value=None):
            ok, reason = strict_reader_check("/nonexistent.pdf")
        self.assertFalse(ok)
        self.assertIn("not found", reason)


class TestRepairPdf(unittest.TestCase):
    def test_repairs_hybrid_encrypt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _hybrid_encrypt_pdf(os.path.join(tmp, "hyb.pdf"))
            before = os.path.getsize(path)
            self.assertFalse(strict_reader_check(path)[0])

            self.assertTrue(repair_pdf(path), "规范化后应能被严格阅读器打开")

            ok, reason = strict_reader_check(path)
            self.assertTrue(ok, reason)
            with pikepdf.open(path) as pdf:
                self.assertEqual(len(pdf.pages), 1, "规范化不得丢页面")
            # 内容保留：修复只重写结构
            self.assertGreater(os.path.getsize(path), before * 0.5)

    def test_repair_is_idempotent_in_effect(self):
        """反复修复都必须收敛到「可读 + 页数不变」。

        不比较字节：qpdf 每次保存都会写入带随机文档 ID 的 ``%`` 注释行，
        逐字节相等不是应当断言的性质。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = _hybrid_encrypt_pdf(os.path.join(tmp, "hyb.pdf"))
            self.assertTrue(repair_pdf(path))
            first = _page_count(path)
            self.assertTrue(repair_pdf(path))
            self.assertEqual(_page_count(path), first)
            self.assertTrue(strict_reader_check(path)[0])

    def test_valid_pdf_survives_repair_intact(self):
        """正常文件经修复后内容必须不变（只重写结构，不动页面）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "ok.pdf"), pages=3)
            pages_before = _page_count(path)
            text_before = _all_text(path)
            self.assertTrue(repair_pdf(path))
            self.assertEqual(_page_count(path), pages_before)
            self.assertEqual(_all_text(path), text_before)
            self.assertTrue(strict_reader_check(path)[0])

    def test_unrepairable_file_is_left_untouched(self):
        """修不了就明确返回 False，且绝不把原件换成另一个坏文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _text_stub_pdf(os.path.join(tmp, "stub.pdf"))
            before = open(path, "rb").read()
            self.assertFalse(repair_pdf(path))
            self.assertEqual(open(path, "rb").read(), before)
            self.assertEqual(os.listdir(tmp), ["stub.pdf"], "临时文件必须清理")

    def test_missing_file_returns_false(self):
        self.assertFalse(repair_pdf("/nonexistent/nope.pdf"))

    def test_repair_refuses_to_rewrite_without_pypdfium2(self):
        """无法验证结果时就不要动用户的文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "ok.pdf"))
            before = open(path, "rb").read()
            with patch.object(pdf_validity, "_pdfium", return_value=None):
                self.assertFalse(repair_pdf(path))
            self.assertEqual(open(path, "rb").read(), before)


class TestEnsureReadable(unittest.TestCase):
    def test_valid_file_passes_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "ok.pdf"))
            before = open(path, "rb").read()
            with patch.object(pdf_validity, "repair_pdf") as repair:
                self.assertTrue(ensure_readable(path))
                repair.assert_not_called()
            self.assertEqual(open(path, "rb").read(), before)

    def test_broken_file_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _hybrid_encrypt_pdf(os.path.join(tmp, "hyb.pdf"))
            with self.assertLogs("pdf2zh.pdf_validity", level="INFO") as cap:
                self.assertTrue(ensure_readable(path, label="probe"))
            self.assertTrue(any("normalised" in m for m in cap.output), cap.output)
            self.assertTrue(strict_reader_check(path)[0])

    def test_unrepairable_file_is_reported_but_delivered(self):
        """闸门不得以「丢弃产物」为代价 —— 用户宁可拿到打不开的文件也不要空手。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _text_stub_pdf(os.path.join(tmp, "stub.pdf"))
            before = open(path, "rb").read()
            with self.assertLogs("pdf2zh.pdf_validity", level="ERROR") as cap:
                self.assertFalse(ensure_readable(path, label="V4 stub"))
            self.assertTrue(any("could not be repaired" in m for m in cap.output))
            self.assertTrue(os.path.isfile(path), "产物必须仍然留在磁盘上")
            self.assertEqual(open(path, "rb").read(), before)

    def test_never_raises(self):
        ensure_readable("/nonexistent/x.pdf")
        ensure_readable("")


class TestSourceWarning(unittest.TestCase):
    def test_valid_source_warns_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                source_readability_warning(_plain_pdf(os.path.join(tmp, "ok.pdf")))
            )

    def test_broken_source_produces_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            msg = source_readability_warning(
                _hybrid_encrypt_pdf(os.path.join(tmp, "hyb.pdf"))
            )
            self.assertIsNotNone(msg)
            self.assertIn("Chrome", msg)
            self.assertIn("/Encrypt", msg)
            self.assertIn("load+save", msg, "告警要给出可操作的修复方式")

    def test_missing_source_warns_nothing(self):
        self.assertIsNone(source_readability_warning("/nonexistent.pdf"))


class TestGateIsWiredIn(unittest.TestCase):
    """闸门必须挂在**每一条**产物路径上，否则等于没有。

    逐一验证各采集/写盘点确实调用了 ``ensure_readable``。
    """

    def test_babeldoc_adapter_collector_calls_the_gate(self):
        import pdf2zh.babeldoc_adapter as ba

        class _R:
            mono_pdf_path = "a.pdf"
            dual_pdf_path = "a-dual.pdf"

        with tempfile.TemporaryDirectory() as tmp:
            for n in ("a.pdf", "a-dual.pdf"):
                _plain_pdf(os.path.join(tmp, n))
            r = _R()
            r.mono_pdf_path = os.path.join(tmp, "a.pdf")
            r.dual_pdf_path = os.path.join(tmp, "a-dual.pdf")
            with patch.object(ba, "ensure_readable") as gate:
                files = ba._collect_result_files(r)
            self.assertEqual(len(files), 2)
            self.assertEqual(gate.call_count, 2)

    def test_runtime_service_magicpdf_collector_calls_the_gate(self):
        import pdf2zh.services.runtime_service as rs

        svc = rs.RuntimeService.__new__(rs.RuntimeService)
        with tempfile.TemporaryDirectory() as tmp:
            magic = os.path.join(tmp, "magicpdf")
            os.makedirs(magic)
            _plain_pdf(os.path.join(magic, "src_mono.pdf"))
            with patch.object(rs, "ensure_readable") as gate:
                files = svc._magicpdf_result_entries(tmp, "src.pdf")
            self.assertEqual(len(files), 1)
            gate.assert_called_once()
            self.assertTrue(gate.call_args[0][0].endswith("src_mono.pdf"))

    def test_v4_output_path_is_gated(self):
        """V4 会把纯文本桩写成 .pdf —— 该路径必须有闸门 + 明确报错。"""
        src = open(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "pdf2zh",
                "services",
                "runtime_service.py",
            ),
            encoding="utf-8",
        ).read()
        v4 = src[src.index("def _execute_v4") : src.index("def _execute_v4") + 9000]
        self.assertIn("ensure_readable", v4)
        self.assertIn("is not a loadable PDF", v4)

    def test_legacy_write_path_is_gated(self):
        src = open(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "pdf2zh",
                "services",
                "runtime_service.py",
            ),
            encoding="utf-8",
        ).read()
        legacy = src[src.index("def _execute_legacy") :][:12000]
        self.assertIn("ensure_readable", legacy)


if __name__ == "__main__":
    unittest.main()
