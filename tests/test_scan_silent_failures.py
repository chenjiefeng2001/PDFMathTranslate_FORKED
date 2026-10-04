"""扫描件翻译链路的四类静默失效 + 排版几何可观测性。

背景（325 页扫描件 ``task_c76b0cf6457a`` 实测）
--------------------------------------------------
一次任务状态是 ``completed`` / 100% / 无 error，但产物：

- ``990001e7-mono.pdf`` 650 页（源325 页），全档 **CJK=0** —— 一字未译；
- ``c54e7189-dual.pdf`` 325 页，同样 CJK=0；
- 60 页抽样里 30 个 span 越出页面下边界、9 页 span 相互压盖（肉眼叠影）。

本文件锁死四件事：

1. **D1零译文必须硬失败**。``translation_ok`` 只说明翻译器没抛异常；
   翻译器返回原文（缓存命中同源文本、目标语=源语、服务端 passthrough）时
   错误列表为空、旧逻辑判 rc=0，于是把一份逐字等于原文的「译文」当成功交付。
2. **D2 产物收集不得交付无法归属的文件**。``matches_source`` 原本写成
   ``not stem or ...``，stem 为空时恒真 —— 批量路径不传 source_path，
   于是 out_dir 里任意 ``-mono.``/``-dual.`` 都会被当成本次产物。
3. **D4 排版几何必须有观测点**。越界与叠印此前既不进 stats 也不告警。
4. **D5 单文件完成计数**。completed_files 停在 0，前端显示「完成 0/1」。
"""

import os
import tempfile
import unittest

import pymupdf

from pdf2zh import pdf_validity
from pdf2zh.services.runtime_service import RuntimeService, TaskStage
from pdf2zh.v3.magicpdf_renderer import audit_page_geometry, render_plan_to_pdf


def _service() -> RuntimeService:
    svc = RuntimeService()
    svc.shutdown()  # 纯 store 模式，无需后台扫描线程
    return svc


def _task(svc: RuntimeService) -> str:
    tid = "task_geo"
    svc._store.create_task(tid)
    svc._store.update_task(tid, status=TaskStage.PARSING.value)
    return tid


def _pdf(path, pages=1, size=(300, 300), text="hello"):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((30, 60 + i * 20), f"{text} {i}")
    doc.save(path)
    doc.close()
    return path


def _plan_entry(page=0, text="hello", translated=None, box=None):
    return {
        "block_id": f"b{page}",
        "page": page,
        "kind": "paragraph",
        "text": text,
        "translated": text if translated is None else translated,
        "render_path": "translate_refit",
        "src_box": box or [30, 40, 200, 80],
        "dst_box": box or [30, 40, 200, 80],
        "font_size": 12,
    }


class TestZeroTranslationIsHardFailure(unittest.TestCase):
    """D1: 身份翻译（译文 == 原文）不能冒充成功。"""

    def test_identity_translation_counts_zero_changed(self):
        """分类逻辑本身：只有内容真变了才算数。"""
        calls = []

        def translate_with_tracking(value):
            out = "".join(value)  # 恒等：与旧实现同样用闭包计数
            calls.append((value, out))
            return out

        from pdf2zh.v3.document_model import translate_document

        doc = _doc_with_one_paragraph()
        translate_document(doc, translate_with_tracking, lang_out="zh-CN")
        ok = len(calls)
        changed = sum(1 for v, o in calls if (o or "").strip() != (v or "").strip())
        self.assertGreater(ok, 0, "翻译器应被调用")
        self.assertEqual(changed, 0, "恒等翻译下 changed 必须为 0")

    def test_real_change_is_counted(self):
        calls = []

        def translate_with_tracking(value):
            out = f"译:{value}"
            calls.append((value, out))
            return out

        from pdf2zh.v3.document_model import translate_document

        doc = _doc_with_one_paragraph()
        translate_document(doc, translate_with_tracking, lang_out="zh-CN")
        changed = sum(1 for v, o in calls if (o or "").strip() != (v or "").strip())
        self.assertEqual(changed, len(calls))

    def test_cli_source_carries_the_identity_guard(self):
        """守住真正的落点：CLI 里必须有 identity_only 判定。"""
        import inspect

        from pdf2zh import magicpdf_cli

        src = inspect.getsource(magicpdf_cli.run_magicpdf_main)
        self.assertIn("translation_changed", src)
        self.assertIn("identity_only", src)
        self.assertIn("身份翻译", src)


def _doc_with_one_paragraph():
    from pdf2zh.v3.canonical_page import BlockModel, PageModel
    from pdf2zh.v3.document_model import DocumentModel

    block = BlockModel(
        kind="paragraph",
        text="Structural holes are absent relationships.",
        x0=30,
        y0=40,
        x1=200,
        y1=80,
    )
    page = PageModel(page_num=0, width=300, height=300, blocks=[block])
    return DocumentModel(pages=[page])


class TestArtifactCollectionCannotDeliverForeignFiles(unittest.TestCase):
    """D2: 只交付能确认属于本次任务的产物。"""

    def setUp(self):
        self.svc = _service()

    def tearDown(self):
        self.svc.shutdown()

    def test_empty_stem_matches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "990001e7-mono.pdf")
            with open(stale, "wb") as fh:
                fh.write(b"%PDF-1.7\n%%EOF\n")
            entries = self.svc._magicpdf_result_entries(tmp, source_path=None)
            self.assertEqual(entries, [], "stem 为空时不得把任意 -mono. PDF 当本次产物")

    def test_mismatched_stem_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = os.path.join(tmp, "c54e7189-dual.pdf")
            with open(wrong, "wb") as fh:
                fh.write(b"%PDF-1.7\n%%EOF\n")
            entries = self.svc._magicpdf_result_entries(
                tmp, source_path=os.path.join(tmp, "990001e7.pdf")
            )
            self.assertEqual(entries, [], "stem 不匹配的产物不得收集")

    def test_matching_stem_in_magic_dir_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            magic = os.path.join(tmp, "magicpdf")
            os.makedirs(magic)
            good = os.path.join(magic, "990001e7_mono.pdf")
            _pdf(good)
            entries = self.svc._magicpdf_result_entries(
                tmp, source_path=os.path.join(tmp, "990001e7.pdf")
            )
            self.assertEqual([e["name"] for e in entries], ["990001e7_mono.pdf"])

    def test_baseline_filters_untouched_files(self):
        """上一轮遗留、本轮未改动的文件不得当成本轮产物。"""
        with tempfile.TemporaryDirectory() as tmp:
            magic = os.path.join(tmp, "magicpdf")
            os.makedirs(magic)
            stale = os.path.join(magic, "990001e7_mono.pdf")
            _pdf(stale)
            src = os.path.join(tmp, "990001e7.pdf")
            baseline = self.svc._magicpdf_artifact_snapshot(tmp, src)
            entries = self.svc._magicpdf_result_entries(
                tmp, source_path=src, baseline=baseline
            )
            self.assertEqual(entries, [], "未改动的旧产物不得计入本轮")

    def test_batch_path_passes_source_path(self):
        """批量收集必须传 source_path（代码级守卫，防回归）。"""
        import inspect

        src = inspect.getsource(RuntimeService._execute_magicpdf_batch)
        self.assertIn(
            "_magicpdf_result_entries(\n                    file_output, source_path=path",
            src,
        )


class TestLayoutGeometryIsObservable(unittest.TestCase):
    """D4: 越界与叠印必须成为可统计、可告警的事实。"""

    def test_clean_render_reports_no_defects(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "m.pdf")
            _pdf(out, pages=1)
            _, stats = render_plan_to_pdf(
                [_plan_entry(translated="你好世界")],
                page_sizes={0: [300, 300]},
                output_path=out + ".render.pdf",
            )
            self.assertNotIn("spans_outside_page", stats)
            self.assertNotIn("pages_with_overlap", stats)

    def test_span_outside_page_is_counted(self):
        """dst_box 落在页面下边界之外时必须被统计。

        几何取自实测：300pt 页高、基线 y=305 时 span bbox 底边 308.3 →
        越界 1 个。y=320 这类更极端的值 MuPDF 直接不落墨（读回无 span），
        所以测试必须用「刚越界」而不是「远远越界」，否则测的是 MuPDF 的
        丢弃行为而不是我们的检测。
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "m.pdf")
            _, stats = render_plan_to_pdf(
                [_plan_entry(translated="edge", box=[30, 291, 200, 306])],
                page_sizes={0: [300, 300]},
                output_path=out,
            )
            self.assertGreater(
                stats.get("spans_outside_page", 0), 0, "越界 span 必须被统计"
            )
            self.assertGreater(stats.get("pages_with_spans_outside", 0), 0)

    def test_background_spans_are_excluded_from_overlap(self):
        """背景层 span 不得算作「本次绘制压盖」。

        ``show_pdf_page`` 会把原页文本**复制进输出文本层**，所以一个覆盖
        原文的译文块必然与那段原文 bbox 重叠。若不排除，每个翻译块都会
        报一次重叠 —— 实测 4 页里报出 43 对，审计直接退化成噪声。
        """
        import tempfile as _tf

        from pdf2zh.v3.magicpdf_renderer import page_span_snapshot

        with _tf.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            doc = pymupdf.open()
            pg = doc.new_page(width=300, height=300)
            pg.insert_text((30, 60), "original source line")
            doc.save(src)
            doc.close()

            out = pymupdf.open()
            page = out.new_page(width=300, height=300)
            page.show_pdf_page(page.rect, pymupdf.open(src), 0)
            background = page_span_snapshot(page)
            page.insert_text((30, 60), "translated line")  # 同一位置

            naive = audit_page_geometry(out)
            self.assertGreater(
                naive["pages_with_overlap"],
                0,
                "不排除背景时确实会报重叠（这正是要修的噪声源）",
            )
            filtered = audit_page_geometry(out, ignore={0: background})
            self.assertEqual(
                filtered["pages_with_overlap"],
                0,
                "排除背景后，同一位置的译文不应算作压盖",
            )
            out.close()

    def test_geometry_audit_detects_overlap_on_real_pdf(self):
        """直接对真实产物形状的 PDF 跑审计：同一行画两份 → 压盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ghost.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 60), "Overlapping line of text here")
            # 同一行、纵向几乎重叠的第二份（模拟叠影）
            page.insert_text((31, 62), "Overlapping line of text here")
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertGreater(audit["pages_with_overlap"], 0, "重叠页必须被审计发现")

    def test_audit_ignores_blank_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "blank.pdf")
            doc = pymupdf.open()
            doc.new_page(width=300, height=300)
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertEqual(audit["spans_outside_page"], 0)
            self.assertEqual(audit["pages_with_overlap"], 0)

    def test_audit_handles_none_document(self):
        audit = audit_page_geometry(None)
        self.assertEqual(audit["spans_outside_page"], 0)

    def test_overlap_threshold_does_not_flag_tight_spans(self):
        """相邻但不重叠的 span（同行紧邻）不得误报为叠印。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tight.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 60), "AAAA")
            page.insert_text((60, 60), "BBBB")
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertEqual(audit["pages_with_overlap"], 0, "紧邻不重叠不算叠印")

    def test_sub_threshold_overlap_is_not_flagged(self):
        """重叠量小于阈值的**真重叠**必须放过。

        锁死 3pt 阈值本身：把阈值改成 0 会让每一对紧邻 span 都成「叠印」，
        325 页文档会报出成千上万个假阳性，告警随即变成噪声而被忽略 ——
        那等于把这个可观测点废掉。

        几何取自实测：11pt 字号插出来�� span bbox 高 15.11pt，所以纵向错开
        13.2pt 时两行仍重叠约 1.9pt（低于阈值）；错开 1.5pt 时重叠 13.6pt
        ——那是真叠印，必须被抓（见下一个测试）。两个方向的边界都写死。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "slight.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 60), "first line here")
            page.insert_text((30, 73.2), "first line here")
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertEqual(
                audit["pages_with_overlap"],
                0,
                "1.9pt 的重叠低于 3pt 阈值，不得算叠印",
            )

    def test_vertical_offset_of_1_5pt_is_real_overlap(self):
        """1.5pt 错位在 11pt 字号下重叠 13.6pt —— 必须判为叠印。

        与上一个测试成对：证明阈值是在区分「轻微压边」与「整行叠印」，
        而不是把纵向错位一律放过。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "small_offset.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 60), "first line here")
            page.insert_text((30, 61.5), "first line here")
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertGreater(audit["pages_with_overlap"], 0)

    def test_real_overlap_above_threshold_is_flagged(self):
        """同一行整行级重叠（实测叠影形态）必须被抓。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "real.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 60), "a whole duplicated line of text")
            page.insert_text((30, 60.5), "a whole duplicated line of text")
            doc.save(path)
            doc.close()
            audit = audit_page_geometry(pymupdf.open(path))
            self.assertGreater(audit["pages_with_overlap"], 0)


class TestSingleFileCompletionCount(unittest.TestCase):
    """D5: completed=1 时completed_files 不能是 0。"""

    def test_single_file_completion_counts_one(self):
        svc = _service()
        try:
            tid = _task(svc)
            with tempfile.TemporaryDirectory() as tmp:
                real = _pdf(os.path.join(tmp, "x-mono.pdf"))
                svc._complete_file(
                    tid, [{"name": "x-mono.pdf", "path": real}], message="Completed"
                )
                state = svc.get_task_state(tid)
                self.assertEqual(state.status, TaskStage.COMPLETED.value)
                self.assertEqual(
                    state.completed_files, 1, "单文件完成必须记 1，不能停在 0"
                )
        finally:
            svc.shutdown()


class TestValidityGateStillWired(unittest.TestCase):
    """确认本轮改动没有把既有严格阅读器闸门弄坏。"""

    def test_gate_module_available(self):
        self.assertTrue(hasattr(pdf_validity, "ensure_readable"))


if __name__ == "__main__":
    unittest.main()
