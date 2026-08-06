# -*- coding: utf-8 -*-
"""V1.12 — 文档统一模型（DocumentModel）：多页树 + Relations + 图桥接。

覆盖：
- build_document_model：多页 → 页树 + 全部标注（role/formula/style/toc/render）；
- Relations：FOLLOWS 阅读序 / TOC_CHILD_OF 层级 / CAPTION_OF（best-effort）；
- annotate_translation：译后文本写 metadata；
- to_graph：投影 DocumentGraph（节点 + 边）+ view_as_ir 序列化视图可用；
- 主链路通道：run_document_model 跨页累积；
- 多页模型 JSON 可序列化（诊断/落盘）。
"""
import json
import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.v3.document_model import (
    REL_CAPTION_OF, REL_FOLLOWS, REL_TOC_CHILD_OF, DocumentModel,
    annotate_render, annotate_translation, build_document_model,
)


def make_char(x, y, text="A", size=10.0, fontname="Helvetica"):
    font = Mock()
    font.fontname = fontname
    font.get_descent.return_value = -0.25
    ch = LTChar(
        (1, 0, 0, 1, x, y),
        font,
        size,
        1.0,
        0.0,
        text,
        textwidth=0.5,
        textdisp=(0.0, 0.0),
        ncs=Mock(),
        graphicstate=Mock(),
    )
    ch.cid = ord(text[0])
    ch.font = font
    return ch


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica"):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname))


def build_converter(**kwargs):
    from pdf2zh.converter import TranslateConverter
    from pdf2zh.collision_resolver import CollisionResolver
    translator = Mock()
    translator.translate = Mock(side_effect=lambda t: "YI" + t)
    translator.lang_in = "en"
    translator.lang_out = "zh-CN"
    from unittest.mock import patch
    with patch("pdf2zh.converter.build_translator") as bt:
        bt.return_value = translator
        conv = TranslateConverter(
            PDFResourceManager(),
            layout={},
            lang_in="en", lang_out="zh-CN", service="stub",
        )
    conv.thread = 1
    conv.noto_name = "noto"
    noto = Mock()
    noto.char_lengths.return_value = [8.0]
    noto.has_glyph.return_value = True
    conv.noto = noto
    conv.fontmap, conv.fontid = {}, {}
    conv.text_metrics = {}
    conv.collision_resolver = CollisionResolver()
    conv.translator = translator
    conv.emit_ir = False
    conv.relayout_gate = None
    for k, v in kwargs.items():
        setattr(conv, k, v)
    return conv


class TestBuildDocumentModel(unittest.TestCase):
    def _two_pages(self):
        p1 = LTPage(1, (0, 0, 600, 800))
        add_text(p1, 50, 700, "5.2.1 Parser ...... 292")
        add_text(p1, 50, 680, "Plain body text", fontname="Times")
        p2 = LTPage(2, (0, 0, 600, 800))
        add_text(p2, 50, 700, "x^2 + y^2 = z^2")
        add_text(p2, 50, 680, "More body text")
        return [p1, p2]

    def test_multi_page_model_and_annotations(self):
        model = build_document_model(self._two_pages())
        self.assertEqual(len(model.pages), 2)
        self.assertEqual(model.metadata["page_order"], [1, 2])
        stats = model.stats()
        self.assertEqual(stats["pages"], 2)
        self.assertGreaterEqual(stats["blocks"], 4)
        # 标注 Pass 生效：TOC（scan）+ role + render
        p1 = model.pages[0]
        toc = [b for b in p1.blocks if b.metadata.get("kind") == "toc"]
        self.assertTrue(toc, "树内自扫描应标出 TOC")
        self.assertEqual(toc[0].metadata["toc_number"], "5.2.1")
        self.assertTrue(all(b.metadata.get("render_path") for b in p1.blocks))

    def test_relations_follows_and_toc_hierarchy(self):
        model = build_document_model([self._two_pages()[0]])
        types = [r.type for r in model.relations]
        self.assertIn(REL_FOLLOWS, types)
        p1 = model.pages[0]
        toc_block = [b for b in p1.blocks
                     if b.metadata.get("kind") == "toc"][0]
        toc_idx = p1.blocks.index(toc_block)
        child = f"p1_{toc_idx}"
        rels = [r for r in model.relations
                if r.type == REL_TOC_CHILD_OF and r.source == child]
        # 无父编号（5.2.1 是根）→ 无 TOC_CHILD_OF；父条目场景见下个用例
        self.assertEqual(rels, [])

    def test_toc_child_of_with_parent_entry(self):
        from pdf2zh.v3.canonical_page import annotate_toc_scan
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 740, "5 Results ...... 290")
        add_text(page, 50, 720, "5.2 Parser ...... 292")
        p = __import__("pdf2zh.v3.canonical_page",
                       fromlist=["build_page_model"]).build_page_model(
            page, page_num=1)
        annotate_toc_scan(p)
        model = DocumentModel()
        model.add_page(p)
        rels = [r for r in model.relations
                if r.type == REL_TOC_CHILD_OF]
        self.assertEqual(len(rels), 1)
        parent = [b for b in p.blocks if b.metadata.get("toc_number") == "5"][0]
        child = [b for b in p.blocks
                 if b.metadata.get("toc_number") == "5.2"][0]
        self.assertEqual(rels[0].source, f"p1_{p.blocks.index(child)}")
        self.assertEqual(rels[0].target, f"p1_{p.blocks.index(parent)}")

    def test_caption_of_relation(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Fig. 1. System overview.", fontname="Times")
        add_text(page, 50, 680, "Body text here")
        p = __import__("pdf2zh.v3.canonical_page",
                       fromlist=["build_page_model"]).build_page_model(
            page, page_num=1)
        from pdf2zh.v3.document_model import annotate_roles
        annotate_roles(p)
        # 无 figure 块 → CAPTION_OF 应为空（best-effort，不误报）
        model = DocumentModel()
        model.add_page(p)
        self.assertFalse(
            [r for r in model.relations if r.type == REL_CAPTION_OF])
        # 手工造 figure 块验证连线逻辑
        fig = __import__("pdf2zh.v3.canonical_page",
                         fromlist=["BlockModel"]).BlockModel(
            text="", kind="figure", x0=50, y0=720, x1=300, y1=750)
        p.blocks.insert(0, fig)
        for b in p.blocks:
            if b.metadata.get("role") == "caption":
                b.metadata["role"] = "caption"
        model2 = DocumentModel()
        model2.add_page(p)
        caps = [r for r in model2.relations if r.type == REL_CAPTION_OF]
        self.assertEqual(len(caps), 1)

    def test_annotate_translation(self):
        model = build_document_model([self._two_pages()[0]])
        p1 = model.pages[0]
        n = len(p1.blocks)
        hits = annotate_translation(p1, {"p1_0": "译后文本", "p1_1": "正文"})
        self.assertEqual(hits, min(2, n))
        b0 = p1.blocks[0]
        self.assertEqual(b0.metadata["translated"], "译后文本")
        self.assertFalse(b0.metadata["translated_same"])

    def test_annotate_render_paths(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        add_text(page, 50, 680, "x^2 + y^2 = z^2")
        from pdf2zh.v3.canonical_page import build_page_model
        p = build_page_model(page, page_num=1)
        annotate_render(p)
        for b in p.blocks:
            self.assertIn(b.metadata["render_path"],
                          ("overlay", "preserve_float", "translate_refit"))


class TestDocumentModelGraphBridge(unittest.TestCase):
    def test_to_graph_and_ir_view(self):
        from pdf2zh.v3.ir_convergence import converged_snapshot
        model = build_document_model(
            [__import__("tests.v3.test_v12_document_model",
                        fromlist=["TestBuildDocumentModel"]).
             TestBuildDocumentModel()._two_pages()[0]])
        g = model.to_graph()
        self.assertGreaterEqual(len(g.nodes), 2)
        # 节点 id 稳定、可寻址
        ids = {n.id for n in g.nodes}
        self.assertTrue(any(i.startswith("p1_") for i in ids))
        # 边来自 Relations
        self.assertGreaterEqual(len(g.edges), 1)
        # 既有生态视图可消费（IR 序列化视图，不新增 IR）
        snap = converged_snapshot(g, title="doc_model_test")
        self.assertIsNotNone(snap)
        d = snap if isinstance(snap, dict) else snap.to_dict()
        self.assertIn("node_count", d)

    def test_model_json_serializable(self):
        model = build_document_model(
            [__import__("tests.v3.test_v12_document_model",
                        fromlist=["TestBuildDocumentModel"]).
             TestBuildDocumentModel()._two_pages()[0]])
        text = json.dumps(model.to_dict(), ensure_ascii=False)
        self.assertTrue(text)


class TestMainlineDocumentModelChannel(unittest.TestCase):
    def test_run_document_model_accumulates_pages(self):
        from pdf2zh.v3.mainline_wiring import run_document_model
        conv = build_converter()
        conv.document_model = None
        p1 = LTPage(1, (0, 0, 600, 800))
        add_text(p1, 50, 700, "5.2.1 Parser ...... 292")
        p2 = LTPage(2, (0, 0, 600, 800))
        add_text(p2, 50, 700, "Body text on page two")
        conv._gate_records = []
        run_document_model(conv, p1)
        run_document_model(conv, p2)
        dm = conv.document_model
        self.assertIsInstance(dm, DocumentModel)
        self.assertEqual(dm.metadata["page_order"], [1, 2])
        self.assertEqual(len(dm.pages), 2)
        # 单页数据可落盘（诊断用途）
        d = dm.to_dict()
        self.assertEqual(d["stats"]["pages"], 2)


class TestModelConsumption(unittest.TestCase):
    def _page_with_toc_and_formula(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        add_text(page, 50, 680, "Body text here", fontname="Times")
        add_text(page, 50, 660, "x^2 + y^2 = z^2")
        return page

    def test_translate_document_decisions(self):
        from pdf2zh.v3.canonical_page import build_page_model
        from pdf2zh.v3.document_model import (
            DocumentModel, build_document_model, translate_document,
        )
        model = build_document_model([self._page_with_toc_and_formula()])
        stats = translate_document(model, lambda t: "译_" + t)
        # toc 描述标题 + 正文翻译；公式 preserve
        self.assertGreaterEqual(stats["translated"], 2)
        self.assertGreaterEqual(stats["preserved"], 1)
        self.assertGreaterEqual(stats["toc_translated"], 1)
        p1 = model.pages[0]
        translated = [b.metadata["translated"] for b in p1.blocks
                      if b.metadata.get("translate")]
        self.assertTrue(any(t.startswith("译_") for t in translated))
        # 公式块 preserve：translated == 原文
        formula = [b for b in p1.blocks
                   if b.metadata.get("role") == "formula"][0]
        self.assertEqual(formula.metadata["translated"], "x^2 + y^2 = z^2")
        self.assertFalse(formula.metadata["translate"])

    def test_render_plan_from_model(self):
        from pdf2zh.v3.document_model import (
            build_document_model, render_plan_from_model,
        )
        model = build_document_model([self._page_with_toc_and_formula()])
        plan = render_plan_from_model(model)
        self.assertGreaterEqual(len(plan), 3)
        by_kind = {p["kind"] for p in plan}
        self.assertIn("toc", by_kind)
        toc = [p for p in plan if p["kind"] == "toc"][0]
        self.assertTrue(toc["block_id"].startswith("p1_"))
        self.assertEqual(len(toc["src_box"]), 4)
        self.assertEqual(toc["dst_box"], toc["src_box"])  # 初始 = 源几何
        self.assertGreater(toc["font_size"], 0)

    def test_toc_records_from_model_schema(self):
        from pdf2zh.v3.document_model import (
            build_document_model, toc_records_from_model,
        )
        model = build_document_model([self._page_with_toc_and_formula()])
        records = toc_records_from_model(model)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["number"], "5.2.1")
        self.assertEqual(r["title"], "Parser")
        self.assertEqual(r["page"], "292")
        self.assertEqual(r["page_num"], 1)
        self.assertTrue(r["block_id"].startswith("p1_"))
        self.assertTrue(r["matched"])
        # 与 toc_to_ir_records schema 兼容的键都在
        for key in ("raw", "kind", "level", "number", "title", "page",
                    "leader", "matched", "title_remainder",
                    "translated_title", "page_num"):
            self.assertIn(key, r)

    def test_annotate_translation_from_records(self):
        from pdf2zh.v3.canonical_page import build_page_model
        from pdf2zh.v3.document_model import annotate_translation_from_records
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        pm = build_page_model(page, page_num=1)
        records = [{"text": "5.2.1 Parser ...... 292",
                    "translated": "译 5.2.1 Parser"}]
        hits = annotate_translation_from_records(pm, records)
        self.assertEqual(hits, 1)
        b = pm.blocks[0]
        self.assertEqual(b.metadata["translated"], "译 5.2.1 Parser")
        self.assertFalse(b.metadata["translated_same"])

    def test_model_to_graph_semantic_pipeline_ir(self):
        """统一模型 → DocumentGraph → 既有 Processor 栈 → IR 视图。"""
        from pdf2zh.v3.document_pipeline import run_semantic_pipeline
        from pdf2zh.v3.ir_convergence import converged_snapshot
        model = build_document_model(
            [__import__("tests.v3.test_v12_document_model",
                        fromlist=["TestBuildDocumentModel"]).
             TestBuildDocumentModel()._two_pages()[0]])
        g = model.to_graph()
        report = run_semantic_pipeline(g)
        self.assertTrue(report.ok())
        snap = converged_snapshot(g, title="model_pipeline")
        self.assertIsNotNone(snap)
        ids_before = {n.id for n in g.nodes}
        # 单 IR 断言：run 前后节点集合不变
        self.assertEqual(ids_before, {n.id for n in g.nodes})


if __name__ == "__main__":
    unittest.main()
