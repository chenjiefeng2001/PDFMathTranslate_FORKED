"""IncrementalWriter 的正确性回归。

旧实现产出的是**结构非法**的字节，而自检还报 ``valid=True``：

1. ``_find_startxref`` / ``_count_objects`` 的正则在 ``rb"..."`` 里写成
   ``\\\\s``（双重转义，匹配字面反斜杠）→ ``startxref`` 偏移恒为 0、
   ``_next_objnum`` 恒为 1，新对象与文档真实对象 1 撞号；
2. xref 表从未写出（``_build_xref_section`` 是死代码）；
3. ``startxref`` 指向 0 而不是新 xref 的位置；
4. 追加的 content stream **没有被任何页面的 ``/Contents`` 引用**；
5. 自检只有 ``pikepdf.open``，而它会扫描重建 xref，于是把非法文件也判为有效。

这些断言逐条对应上面的编号。
"""

import os
import re
import tempfile
import unittest

import pikepdf
import pymupdf

from pdf2zh.assembler.incremental.writer import (
    IncrementalSourceError,
    IncrementalWriter,
)

NEW_CONTENT = b"BT /F1 14 Tf 60 200 Td (PATCHED CONTENT MARKER) Tj ET"


def _classic_xref_source(path, pages=4):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=400, height=400).insert_text((40, 60), f"ORIGINAL line {i}")
    doc.save(path, garbage=4, deflate=False)
    doc.close()
    return path


class TestIncrementalWriterValidOutput(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = _classic_xref_source(os.path.join(self._tmp.name, "src.pdf"))
        self.out = os.path.join(self._tmp.name, "out.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, patches):
        writer = IncrementalWriter()
        writer.open(self.src)
        for index, payload in patches:
            writer.replace_page_content(index, payload)
        return writer, writer.commit(self.out)

    def test_startxref_points_at_a_real_xref_table(self):
        """缺陷 3：startxref 曾恒为 0。"""
        self._commit([(0, NEW_CONTENT)])
        data = open(self.out, "rb").read()
        matches = list(re.finditer(rb"startxref\s+(\d+)\s*%%EOF", data))
        self.assertTrue(matches, "产物缺少 startxref/%%EOF")
        offset = int(matches[-1].group(1))
        self.assertGreater(offset, 0, "startxref 偏移不得为 0")
        self.assertLess(offset, len(data))
        self.assertEqual(
            data[offset : offset + 4],
            b"xref",
            "startxref 必须指向经典 xref 表",
        )

    def test_xref_entries_all_land_on_object_headers(self):
        """缺陷 2：xref 表必须真的写出，且每条偏移都落在 'N 0 obj' 上。"""
        self._commit([(0, NEW_CONTENT), (2, NEW_CONTENT)])
        data = open(self.out, "rb").read()
        self.assertIn(b"\nxref\n", data)
        offset = int(
            list(re.finditer(rb"startxref\s+(\d+)\s*%%EOF", data))[-1].group(1)
        )
        tail = data[offset:]
        checked = 0
        for _start, _count, block in re.findall(
            rb"(\d+)\s+(\d+)\s*\n((?:\d{10} \d{5} [nf] \n)+)", tail
        ):
            for entry in block.strip().split(b"\n"):
                parts = entry.split()
                if len(parts) < 3 or parts[2] == b"f":
                    continue
                checked += 1
                at = int(parts[0])
                self.assertRegex(
                    data[at : at + 24],
                    rb"\d+ 0 obj",
                    f"xref 偏移 {at} 未落在对象头",
                )
        self.assertGreaterEqual(checked, 2, "xref 里应有本次写入的对象")

    def test_trailer_chains_previous_xref(self):
        """增量更新必须用 /Prev 接上旧 xref，否则新对象不可解析。"""
        self._commit([(0, NEW_CONTENT)])
        data = open(self.out, "rb").read()
        self.assertIn(b"/Prev", data)
        self.assertEqual(data.count(b"%%EOF"), 2, "增量段应各带一个 %%EOF")

    def test_new_object_numbers_do_not_collide(self):
        """缺陷 1：新对象号曾从 1 起算，与文档真实对象 1 撞号。"""
        writer, stats = self._commit([(0, NEW_CONTENT)])
        data = open(self.out, "rb").read()
        offset = int(
            list(re.finditer(rb"startxref\s+(\d+)\s*%%EOF", data))[-1].group(1)
        )
        appended_numbers = [
            int(n)
            for n, _c, _b in re.findall(
                rb"(\d+)\s+(\d+)\s*\n((?:\d{10} \d{5} [nf] \n)+)",
                data[offset:],
            )
        ]
        self.assertTrue(appended_numbers)
        self.assertTrue(
            all(n > 1 for n in appended_numbers),
            f"追加对象号必须大于已用最大号，得到 {appended_numbers}",
        )
        self.assertGreater(writer._next_objnum, 1)

    def test_patched_page_actually_references_the_new_stream(self):
        """缺陷 4：旧实现追加的流是没人引用的死对象。"""
        self._commit([(0, NEW_CONTENT), (2, NEW_CONTENT)])
        doc = pymupdf.open(self.out)
        try:
            self.assertIn("PATCHED CONTENT MARKER", doc[0].get_text())
            self.assertIn("PATCHED CONTENT MARKER", doc[2].get_text())
            self.assertNotIn("ORIGINAL line 0", doc[0].get_text())
            self.assertIn("ORIGINAL line 1", doc[1].get_text())
            self.assertIn("ORIGINAL line 3", doc[3].get_text())
        finally:
            doc.close()

    def test_page_count_is_preserved(self):
        self._commit([(0, NEW_CONTENT), (1, NEW_CONTENT)])
        with pikepdf.open(self.out) as pdf:
            self.assertEqual(len(pdf.pages), 4)

    def test_valid_flag_requires_the_patch_to_apply(self):
        """缺陷 5：valid 必须同时意味着「能解析」且「补丁生效」。"""
        _writer, stats = self._commit([(0, NEW_CONTENT)])
        self.assertTrue(stats["valid"], stats.get("error"))
        self.assertNotIn("error", stats)
        self.assertEqual(stats["page_count"], 4)

    def test_patch_effect_check_rejects_a_doc_without_the_patch(self):
        """变异防线：``_patch_applied`` 必须真的有鉴别力，不能恒真。

        拿**未打补丁**的源去问「补丁生效了吗」，答案必须是 False —— 否则
        ``valid=True`` 就只是「能打开」，正是旧实现蒙混过关的方式。
        """
        writer, _stats = self._commit([(0, NEW_CONTENT)])
        with pikepdf.open(self.src) as unpatched:
            self.assertFalse(
                writer._patch_applied(unpatched, [0]),
                "对没有补丁的文档误判为已生效",
            )
            self.assertTrue(
                writer._patch_applied(pikepdf.open(self.out), [0]),
                "对已打补丁的文档误判为未生效",
            )

    def test_page_count_mismatch_is_reported_invalid(self):
        """页数不符必须让 valid=False（而不是无条件 True）。"""
        writer = IncrementalWriter()
        writer.open(self.src)
        writer.replace_page_content(0, NEW_CONTENT)
        writer._page_count = 99  # 谎报页数，模拟页数校验被绕过
        stats = writer.commit(self.out)
        self.assertFalse(stats["valid"])
        self.assertEqual(stats["page_count"], 4)

    def test_commit_actually_calls_the_patch_effect_check(self):
        """变异防线：commit 必须**真的调用**补丁生效检查。

        去掉这次调用后，``valid`` 就退化成「只是能打开」——正是旧实现一路
        valid=True 的方式，所以这里断言调用本身发生且拿到被改页号。
        """
        writer = IncrementalWriter()
        writer.open(self.src)
        writer.replace_page_content(0, NEW_CONTENT)
        writer.replace_page_content(2, NEW_CONTENT)

        calls = []
        real_check = writer._patch_applied

        def spy(test_pdf, patched):
            calls.append(list(patched))
            return real_check(test_pdf, patched)

        writer._patch_applied = spy
        stats = writer.commit(self.out)

        self.assertTrue(stats["valid"], stats.get("error"))
        self.assertEqual(calls, [[0, 2]], "commit 未调用补丁生效检查")

    def test_page_type_is_page(self):
        """重写的页对象必须仍然是 /Type /Page（否则真实阅读器会丢弃它）。"""
        self._commit([(0, NEW_CONTENT), (1, NEW_CONTENT)])
        with pikepdf.open(self.out) as pdf:
            for page in pdf.pages:
                self.assertEqual(str(page.obj["/Type"]), "/Page")

    def test_page_keeps_its_resources_and_media_box(self):
        """除了 /Contents，页面的其他键必须原样保留。"""
        self._commit([(0, NEW_CONTENT)])
        with pikepdf.open(self.src) as before, pikepdf.open(self.out) as after:
            for key in ("/MediaBox", "/Resources"):
                if key in before.pages[0].obj:
                    self.assertIn(key, after.pages[0].obj)
                    self.assertEqual(
                        before.pages[0].obj[key].unparse(),
                        after.pages[0].obj[key].unparse(),
                        f"{key} 未被保留",
                    )

    def test_trailer_size_covers_every_appended_object(self):
        """trailer /Size 必须大于本次写入的最高对象号。"""
        _writer, stats = self._commit([(0, NEW_CONTENT), (2, NEW_CONTENT)])
        data = open(self.out, "rb").read()
        offset = int(
            list(re.finditer(rb"startxref\s+(\d+)\s*%%EOF", data))[-1].group(1)
        )
        trailer = data[offset:]
        size = int(re.search(rb"/Size (\d+)", trailer).group(1))
        self.assertGreater(size, 0)
        objnums = [
            int(n)
            for n, _c, _b in re.findall(
                rb"(\d+)\s+(\d+)\s*\n((?:\d{10} \d{5} [nf] \n)+)",
                trailer,
            )
        ]
        self.assertTrue(objnums)
        self.assertLess(max(objnums), size, "有对象号超出了 trailer /Size")
        self.assertEqual(stats["next_object_number"], size)

    def test_output_is_increment_not_a_rewrite(self):
        _writer, stats = self._commit([(0, NEW_CONTENT)])
        self.assertEqual(stats["original_size"], os.path.getsize(self.src))
        self.assertLess(stats["append_size"], stats["original_size"])
        self.assertEqual(
            open(self.out, "rb").read()[: stats["original_size"]],
            open(self.src, "rb").read(),
            "增量产物必须以原始字节开头",
        )

    def test_strict_reader_accepts_the_output(self):
        """Chrome/Edge 用的 PDFium 必须能打开（用 pypdfium2 时才断言）。"""
        self._commit([(0, NEW_CONTENT)])
        try:
            import pypdfium2 as pdfium
        except Exception:  # noqa: BLE001 -- 依赖不可用则跳过
            self.skipTest("pypdfium2 unavailable")
        doc = pdfium.PdfDocument(self.out)
        try:
            self.assertEqual(len(doc), 4)
        finally:
            doc.close()

    def test_out_of_range_page_is_ignored_not_fatal(self):
        """基准脚本会对不存在的页逐页调用，不能抛异常。"""
        writer = IncrementalWriter()
        writer.open(self.src)
        with self.assertLogs("pdf2zh.assembler.incremental.writer", level="WARNING"):
            writer.replace_page_content(999, NEW_CONTENT)
        stats = writer.commit(self.out)
        self.assertEqual(stats["pages_patched"], 0)
        self.assertTrue(stats["valid"])


class TestIncrementalWriterRejectsBadSources(unittest.TestCase):
    """无法安全增量更新的源必须**明确报错**，不能静默产出坏文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_xref_stream_source(self):
        plain = os.path.join(self._tmp.name, "plain.pdf")
        _classic_xref_source(plain, pages=1)
        xs = os.path.join(self._tmp.name, "xs.pdf")
        with pikepdf.open(plain) as pdf:
            pdf.save(xs, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        self.assertIn(b"/XRef", open(xs, "rb").read())
        with self.assertRaises(IncrementalSourceError) as ctx:
            IncrementalWriter().open(xs)
        self.assertIn("XRef stream", str(ctx.exception))

    def test_rejects_encrypted_source(self):
        enc = os.path.join(self._tmp.name, "enc.pdf")
        doc = pymupdf.open()
        doc.new_page(width=200, height=200).insert_text((20, 40), "secret")
        doc.save(
            enc,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="own",
            user_pw="usr",
        )
        doc.close()
        with self.assertRaises(IncrementalSourceError) as ctx:
            IncrementalWriter().open(enc)
        self.assertIn("encrypted", str(ctx.exception))

    def test_rejects_encrypted_source_with_empty_user_password(self):
        """空用户密码的加密文档**能被** pikepdf 打开（不抛 PasswordError），
        只有 ``is_encrypted`` 这条检查能拦住它 —— 所以这条分支必须存在。
        """
        enc = os.path.join(self._tmp.name, "enc_empty.pdf")
        doc = pymupdf.open()
        doc.new_page(width=200, height=200).insert_text((20, 40), "secret")
        doc.save(
            enc,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="own",
            user_pw="",
        )
        doc.close()
        # 前置确认：pikepdf 确实能打开（否则这条测试就退化成上一个用例）
        with pikepdf.open(enc) as probe:
            self.assertTrue(probe.is_encrypted)
        with self.assertRaises(IncrementalSourceError) as ctx:
            IncrementalWriter().open(enc)
        self.assertIn("encrypted", str(ctx.exception))

    def test_rejects_non_pdf(self):
        bad = os.path.join(self._tmp.name, "bad.pdf")
        with open(bad, "wb") as fh:
            fh.write(b"not a pdf at all")
        with self.assertRaises(IncrementalSourceError):
            IncrementalWriter().open(bad)

    def test_rejects_truncated_pdf(self):
        path = _classic_xref_source(os.path.join(self._tmp.name, "t.pdf"), pages=2)
        raw = open(path, "rb").read()
        with open(path, "wb") as fh:
            fh.write(raw[: len(raw) // 2])
        with self.assertRaises(IncrementalSourceError):
            IncrementalWriter().open(path)

    def test_commit_before_open_raises(self):
        with self.assertRaises(RuntimeError):
            IncrementalWriter().commit(os.path.join(self._tmp.name, "x.pdf"))


class TestIncrementalRegexesAreNotDoubleEscaped(unittest.TestCase):
    """缺陷 1 的根因：``rb"\\\\s"`` 匹配的是字面反斜杠。锁死这两条正则。"""

    def test_startxref_regex_matches_a_real_trailer(self):
        src = _classic_xref_source(os.path.join(tempfile.gettempdir(), "rx.pdf"))
        data = open(src, "rb").read()
        writer = IncrementalWriter()
        found = writer._find_startxref(data)
        self.assertGreater(found, 0, "startxref 正则匹配不到真实尾部")
        self.assertEqual(data[found : found + 4], b"xref")

    def test_object_header_regex_finds_objects(self):
        src = _classic_xref_source(os.path.join(tempfile.gettempdir(), "rx2.pdf"))
        data = open(src, "rb").read()
        self.assertGreater(IncrementalWriter._max_object_number(data), 1)


if __name__ == "__main__":
    unittest.main()
