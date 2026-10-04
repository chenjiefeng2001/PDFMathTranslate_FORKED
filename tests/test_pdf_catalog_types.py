"""Catalog 键类型校验 + 源文件入口闸门。

对应实测缺陷（Edge 155 / PDFium 13x）
------------------------------------
``/Root/AcroForm`` 指向**数组**而非字典（对象 1 是 ``1 0 obj [] endobj``）::

    pypdfium2 (PDFium 151)  ->  放行，325 页正常
    Edge 155   (PDFium 13x) ->  "We can't open this file / Something went
                                 wrong"，页码 0 of 0

原因：纯 PDFium 闸门存在**版本差导致的系统性假阴性**。因此结构校验必须
**不依赖 pypdfium2**（只依赖 pikepdf），并在 PDFium 缺失时降级为**告警**
而非静默放行。

这里的样本是合成的（``_acroform_array_pdf``），不依赖本机任何特定文件。
真机上已用 Downloads 的 373 个 PDF 普查过：零假阳性，只精确命中那两个坏文件。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pikepdf
import pymupdf

from pdf2zh import pdf_validity
from pdf2zh.pdf_validity import (
    UnusableSourceError,
    catalog_structure_problems,
    check_source,
    ensure_readable,
    ensure_source_usable,
    inspect_pdf,
    repair_pdf,
    strict_reader_check,
)

HAVE_PDFIUM = pdf_validity._pdfium() is not None


def _plain_pdf(path, pages=1):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=300, height=300).insert_text((30, 60 + i * 20), f"page {i}")
    doc.save(path)
    doc.close()
    return path


def _with_catalog(path, key, value):
    """给 ``path`` 的 Catalog 塞一个键（``value`` 为 pikepdf 对象）。

    pikepdf 拒绝原地覆盖输入，所以先写临时文件再替换。
    """
    tmp = path + ".tmp"
    with pikepdf.open(path) as pdf:
        pdf.Root[key] = value
        pdf.save(tmp)
    os.replace(tmp, path)
    return path


def _with_catalog_stream(path, key, data):
    """给 Catalog 塞一个 stream 类型的键。"""
    tmp = path + ".tmp"
    with pikepdf.open(path) as pdf:
        pdf.Root[key] = pikepdf.Stream(pdf, data)
        pdf.save(tmp)
    os.replace(tmp, path)
    return path


def _acroform_array_pdf(path):
    """复现真实缺陷：``/AcroForm`` 指向一个**数组**。"""
    _plain_pdf(path)
    return _with_catalog(path, "/AcroForm", pikepdf.Array([]))


def _acroform_empty_dict_pdf(path):
    """对照组：``/AcroForm`` 是空字典 —— 合法，不该被拦。"""
    _plain_pdf(path)
    return _with_catalog(path, "/AcroForm", pikepdf.Dictionary())


def _acroform_real_dict_pdf(path):
    """对照组：正常表单（``/Fields`` + ``/DR``），不该被拦。"""
    _plain_pdf(path)
    dr = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary(
                {
                    "/Helv": pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/Font"),
                            "/Subtype": pikepdf.Name("/Type1"),
                            "/BaseFont": pikepdf.Name("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    return _with_catalog(
        path,
        "/AcroForm",
        pikepdf.Dictionary(
            {
                "/Fields": pikepdf.Array([]),
                "/DR": dr,
                "/DA": pikepdf.String("/Helv 0 Tf"),
            }
        ),
    )


def _name_key_array_pdf(path, key):
    _plain_pdf(path)
    return _with_catalog(path, key, pikepdf.Array([]))


def _plain_acroform_array_pdf(path):
    return _acroform_array_pdf(path)


class TestCatalogStructureCheck(unittest.TestCase):
    """结构校验本体。"""

    def test_array_acroform_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = catalog_structure_problems(
                _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("/AcroForm", problems[0])
        self.assertIn("数组", problems[0])
        self.assertIn("字典", problems[0])

    def test_empty_dict_acroform_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                catalog_structure_problems(
                    _acroform_empty_dict_pdf(os.path.join(tmp, "ok.pdf"))
                ),
                [],
            )

    def test_real_acroform_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                catalog_structure_problems(
                    _acroform_real_dict_pdf(os.path.join(tmp, "ok.pdf"))
                ),
                [],
            )

    def test_every_catalog_key_is_actually_checked(self):
        """``/AcroForm`` 不能是特例 —— 表里每个键都要真的生效。

        按各键**声明**的类型塞一个错误类型：期望 dict 的塞数组，期望 array 的
        塞字典。``stream`` 型（``/Metadata``）单独用内存态测 —— 把 ``/Metadata``
        换成非法内容后pikepdf 自己的 ``save()`` 会先抛
        ``update_xmp_pdfversion``，无法落盘做端到端验证。
        """
        file_keys = {
            k: v for k, v in pdf_validity.CATALOG_KEY_TYPES.items() if v != "stream"
        }
        with tempfile.TemporaryDirectory() as tmp:
            for key, want in file_keys.items():
                path = os.path.join(tmp, f"{key.strip('/')}.pdf")
                _plain_pdf(path)
                wrong = pikepdf.Array([]) if want == "dict" else pikepdf.Dictionary()
                _with_catalog(path, key, wrong)
                problems = catalog_structure_problems(path)
                self.assertTrue(
                    any(key in p for p in problems),
                    f"{key} 的类型错误未被检出（表里声明应为 {want}）",
                )

    def test_stream_valued_key_is_checked_in_memory(self):
        """/Metadata 必须是流；塞字典必须被检出（内存态，不落盘）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "m.pdf"))
            with pikepdf.open(path) as pdf:
                pdf.Root["/Metadata"] = pikepdf.Dictionary()
                self.assertEqual(
                    pdf_validity._sanitize_catalog(pdf),
                    ["/Metadata（应为流，实为字典）"],
                )
                self.assertNotIn("/Metadata", pdf.Root)

    def test_declared_types_are_honoured(self):
        """反向：塞**正确**类型时不得报任何问题（防假阳性）。"""
        correct = {"dict": pikepdf.Dictionary(), "array": pikepdf.Array([])}
        with tempfile.TemporaryDirectory() as tmp:
            for key, want in pdf_validity.CATALOG_KEY_TYPES.items():
                if want == "stream":
                    continue  # stream 用专用用例覆盖
                path = os.path.join(tmp, f"ok_{key.strip('/')}.pdf")
                _plain_pdf(path)
                _with_catalog(path, key, correct[want])
                self.assertEqual(
                    catalog_structure_problems(path),
                    [],
                    f"{key} 塞了正确的 {want} 却被误报",
                )

    def test_stream_valued_metadata_key_is_accepted(self):
        """/Metadata 声明为 stream；塞个流进去不该被误报（防假阳性）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "meta.pdf"))
            _with_catalog_stream(path, "/Metadata", b"<x:xmpmeta/>")
            self.assertEqual(catalog_structure_problems(path), [])

    def test_plain_pdf_has_no_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                catalog_structure_problems(_plain_pdf(os.path.join(tmp, "p.pdf"))), []
            )

    def test_missing_file_yields_no_problems(self):
        self.assertEqual(catalog_structure_problems("/nonexistent/nope.pdf"), [])


class TestStrictReaderGateCatchesArrayAcroForm(unittest.TestCase):
    """闸门必须拦住这一类 —— 旧版 PDFium 放行正是漏网的原因。"""

    def test_strict_reader_check_rejects_array_acroform(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = strict_reader_check(
                _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            )
        self.assertFalse(ok, "结构非法的 /AcroForm 未被闸门拦下")
        self.assertIn("/AcroForm", reason)

    def test_strict_reader_check_accepts_dict_acroform(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, _ = strict_reader_check(
                _acroform_real_dict_pdf(os.path.join(tmp, "ok.pdf"))
            )
        self.assertTrue(ok)

    def test_structural_gate_works_without_pdfium(self):
        """pypdfium2 缺失时结构校验仍必须生效（不能一起放行）。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            good = _acroform_real_dict_pdf(os.path.join(tmp, "good.pdf"))
            with patch.object(pdf_validity, "_pdfium", return_value=None):
                ok_bad, reason_bad = strict_reader_check(bad)
                ok_good, _ = strict_reader_check(good)
        self.assertFalse(ok_bad, "PDFium 缺失时结构闸门被一并跳过了")
        self.assertIn("/AcroForm", reason_bad)
        self.assertTrue(ok_good)


class TestDegradedModeIsLoud(unittest.TestCase):
    """PDFium 缺失必须**告警**，不能静默放行。"""

    def test_pdfium_probe_warns_when_import_fails(self):
        """直接验 ``_pdfium()`` 自己会 warn —— 之前只测了下游，漏了这一层。

        用 ``sys.modules['pypdfium2'] = None`` 制造真实的 ImportError；
        直接 patch ``_pdfium`` 会把待测的那行一起替换掉，什么都验不到。
        """
        with patch.dict(sys.modules, {"pypdfium2": None}):
            with patch.object(pdf_validity, "_warned_missing", False):
                with self.assertLogs("pdf2zh.pdf_validity", level="WARNING") as cap:
                    self.assertIsNone(pdf_validity._pdfium())
        self.assertTrue(
            any("pypdfium2 unavailable" in m for m in cap.output),
            f"缺失依赖没有告警，实际日志：{cap.output}",
        )
        self.assertTrue(
            any(m.startswith("WARNING") for m in cap.output),
            f"缺失依赖必须是 warning 级而非 info：{cap.output}",
        )

    def test_verdict_reports_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "p.pdf"))
            with patch.object(pdf_validity, "_pdfium", return_value=None):
                verdict = inspect_pdf(path)
        self.assertFalse(verdict.pdfium_available)
        self.assertTrue(verdict.degraded)
        self.assertTrue(verdict.ok, "结构没问题时仍应放行")

    def test_ensure_readable_warns_when_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "p.pdf"))
            with patch.object(pdf_validity, "_pdfium", return_value=None):
                with self.assertLogs("pdf2zh.pdf_validity", level="WARNING") as cap:
                    ensure_readable(path, label="degraded-probe")
        self.assertTrue(
            any(
                "DEGRADED" in m.upper() or "could not be verified" in m
                for m in cap.output
            ),
            f"未告警降级，实际日志：{cap.output}",
        )


class TestRepairFixesCatalogTypes(unittest.TestCase):
    """``repair_pdf`` 必须真能修 —— pikepdf 的 load+save 本身修不了。"""

    def test_repair_removes_the_malformed_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            before = catalog_structure_problems(path)
            self.assertTrue(before)
            self.assertTrue(repair_pdf(path), "repair_pdf 未能修复")
            self.assertEqual(catalog_structure_problems(path), [])
            ok, _ = strict_reader_check(path)
            self.assertTrue(ok, "修复后仍过不了闸门")

    def test_repair_keeps_page_content_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            doc = pymupdf.open(path)
            self.assertEqual(doc.page_count, 1)
            self.assertIn("page 0", doc[0].get_text())
            doc.close()
            repair_pdf(path)
            with pikepdf.open(path) as pdf:
                self.assertEqual(len(pdf.pages), 1)
            doc = pymupdf.open(path)
            self.assertIn("page 0", doc[0].get_text())
            doc.close()

    def test_repair_leaves_valid_acroform_intact(self):
        """清洗只能删**类型非法**的键，正常表单必须保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_real_dict_pdf(os.path.join(tmp, "ok.pdf"))
            self.assertTrue(repair_pdf(path))
            with pikepdf.open(path) as pdf:
                self.assertIn("/AcroForm", pdf.Root)
                self.assertIn("/Fields", pdf.Root["/AcroForm"])

    def test_ensure_readable_repairs_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "bad.pdf"))
            self.assertTrue(ensure_readable(path, label="artifact"))
            self.assertEqual(catalog_structure_problems(path), [])


class TestSourceGate(unittest.TestCase):
    """源文件入口闸门的三档结论。"""

    def test_missing_file_is_ok(self):
        self.assertEqual(check_source("/nonexistent/x.pdf").severity, "ok")

    def test_plain_source_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                check_source(_plain_pdf(os.path.join(tmp, "p.pdf"))).severity, "ok"
            )

    def test_array_acroform_source_warns_but_is_usable(self):
        """这一类我们读得了、也能产出干净译文，不能阻断（实测可翻译）。"""
        with tempfile.TemporaryDirectory() as tmp:
            verdict = check_source(_acroform_array_pdf(os.path.join(tmp, "s.pdf")))
        self.assertEqual(verdict.severity, "warn")
        self.assertIn("/AcroForm", verdict.message)
        self.assertIn("翻译可继续", verdict.message)

    def test_encrypted_source_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enc.pdf")
            doc = pymupdf.open()
            doc.new_page(width=200, height=200).insert_text((20, 40), "s")
            doc.save(
                path,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="o",
                user_pw="u",
            )
            doc.close()
            verdict = check_source(path)
        self.assertEqual(verdict.severity, "fatal")
        self.assertIn("密码", verdict.message)

    def test_empty_user_password_encryption_is_NOT_fatal(self):
        """回归防线：``is_encrypted=True`` **不等于**我们打不开。

        实测存在大量「空用户密码 + 仅 owner 密码限制权限」的文件
        （``livro_-_the_non-designers_desi.pdf`` 194 页、
        ``topbookchinese.pdf`` 109 页），pikepdf / MuPDF / PDFium 全都能正常
        打开。曾经把 ``pdf.is_encrypted`` 直接判成 fatal，会阻断这类本来能翻
        的任务 —— 闸门制造问题，而不是发现问题。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "perm.pdf")
            doc = pymupdf.open()
            doc.new_page(width=200, height=200).insert_text((20, 40), "s")
            doc.save(
                path,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner-secret",
                user_pw="",
            )
            doc.close()
            # 前置确认：确实是「加密但空用户密码」，且我们读得了
            with pikepdf.open(path) as pdf:
                self.assertTrue(pdf.is_encrypted)
                self.assertEqual(len(pdf.pages), 1)
            self.assertEqual(check_source(path).severity, "ok")
            # 且不得阻断任务
            self.assertEqual(ensure_source_usable(path, label="perm").severity, "ok")

    def test_unparseable_source_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "junk.pdf")
            with open(path, "wb") as fh:
                fh.write(b"this is not a pdf")
            verdict = check_source(path)
        self.assertEqual(verdict.severity, "fatal")

    def test_ensure_source_usable_raises_on_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "junk.pdf")
            with open(path, "wb") as fh:
                fh.write(b"this is not a pdf")
            with self.assertRaises(UnusableSourceError) as ctx:
                ensure_source_usable(path, label="[task=t1] source")
        self.assertIn("无法处理", str(ctx.exception))

    def test_ensure_source_usable_passes_warn_through(self):
        """warn 不得抛异常 —— 否则会打断当前可用的翻译链路。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "s.pdf"))
            verdict = ensure_source_usable(path, label="src")
        self.assertEqual(verdict.severity, "warn")

    def test_ensure_source_usable_passes_ok_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "p.pdf"))
            verdict = ensure_source_usable(path, label="src")
        self.assertEqual(verdict.severity, "ok")


class TestCatalogKeyTableIsConservative(unittest.TestCase):
    """闸门宁可漏报也不能误报 —— 锁住表的边界。"""

    def test_table_only_lists_known_keys(self):
        self.assertEqual(
            sorted(pdf_validity.CATALOG_KEY_TYPES),
            [
                "/AcroForm",
                "/DSS",
                "/Metadata",
                "/Names",
                "/OCProperties",
                "/StructTreeRoot",
                "/Threads",
            ],
        )

    def test_table_maps_to_known_type_names(self):
        self.assertTrue(
            set(pdf_validity.CATALOG_KEY_TYPES.values()) <= {"dict", "array", "stream"}
        )

    def test_no_pages_or_type_in_table(self):
        """``/Pages``/``/Type`` 缺失时 pikepdf 直接打不开，不该在这里重复查。"""
        self.assertNotIn("/Pages", pdf_validity.CATALOG_KEY_TYPES)
        self.assertNotIn("/Type", pdf_validity.CATALOG_KEY_TYPES)

    def test_type_name_classifier(self):
        pikepdf_mod = pdf_validity._pikepdf()
        self.assertEqual(pdf_validity._pdf_type_name(pikepdf_mod.Dictionary()), "dict")
        self.assertEqual(pdf_validity._pdf_type_name(pikepdf_mod.Array([])), "array")
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "p.pdf"))
            with pikepdf.open(path) as pdf:
                stream = pikepdf_mod.Stream(pdf, b"data")
                self.assertEqual(pdf_validity._pdf_type_name(stream), "stream")
                self.assertEqual(
                    pdf_validity._pdf_type_name(pikepdf_mod.Name("/X")), "name"
                )
                self.assertEqual(
                    pdf_validity._pdf_type_name(pikepdf_mod.String("s")), "string"
                )


class TestSourceGateWiring(unittest.TestCase):
    """源闸门必须真的接在任务入口上，否则等于没做。"""

    @staticmethod
    def _svc():
        from pdf2zh.services.runtime_service import RuntimeService

        return RuntimeService()

    @staticmethod
    def _req(path):
        from types import SimpleNamespace

        return SimpleNamespace(source_path=path)

    def test_fatal_source_raises_so_the_task_fails_fast(self):
        """重新抛出即可让 _execute_task 的 except 把任务置 FAILED。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "junk.pdf")
            with open(path, "wb") as fh:
                fh.write(b"not a pdf at all")
            svc = self._svc()
            with self.assertRaises(UnusableSourceError) as ctx:
                svc._warn_unreadable_source("t-wiring-fatal", self._req(path))
        self.assertIn("无法处理", str(ctx.exception))

    def test_warn_source_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "s.pdf"))
            svc = self._svc()
            svc._warn_unreadable_source("t-wiring-warn", self._req(path))

    def test_ok_source_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _plain_pdf(os.path.join(tmp, "p.pdf"))
            svc = self._svc()
            svc._warn_unreadable_source("t-wiring-ok", self._req(path))

    def test_missing_source_does_not_raise(self):
        svc = self._svc()
        svc._warn_unreadable_source("t-wiring-missing", self._req(""))
        svc._warn_unreadable_source(
            "t-wiring-missing2", self._req("/nonexistent/x.pdf")
        )

    def test_warn_source_emits_a_progress_event(self):
        """用户必须能在界面上看到这条归因告警，而不是只落在日志里。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _acroform_array_pdf(os.path.join(tmp, "s.pdf"))
            svc = self._svc()
            events = []
            svc._event_listeners.append(events.append)
            svc._warn_unreadable_source("t-wiring-evt", self._req(path))
        self.assertTrue(events, "warn 级源缺陷没有产生任何进度事件")
        self.assertTrue(
            any("/AcroForm" in (e.message or "") for e in events),
            f"事件里没有缺陷归因：{[e.message for e in events]}",
        )


if __name__ == "__main__":
    unittest.main()
