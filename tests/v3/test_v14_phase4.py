# -*- coding: utf-8 -*-
"""V1.15 — Phase 4：语义级文档重建。

覆盖：
- 4.1 SemanticGraph：sections/belongs_to/mentions（"see Figure 3" 解析）；
- 4.2 ContextTranslation + DomainGlossary：文档上下文 + 领域术语钉死；
- 4.3 References：引用重编号（正文+译文）；
- 4.4 FigureUnderstanding：策略映射 + 图片记录 → figure 块 + caption_of；
- 4.5 Incremental：内容哈希缓存，只重建脏节点；
- 集成：全链在合成模型上跑通 + to_graph 语义边投影。
"""
import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.v3.document_model import build_document_model, translate_document


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


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica", size=10.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname,
                           size=size))


def build_model():
    page = LTPage(1, (0, 0, 600, 800))
    add_text(page, 50, 760, "5 Methodology", size=16)
    add_text(page, 50, 740, "5.1 Data Collection")
    add_text(page, 50, 720, "The kernel scheduler runs threads here.")
    add_text(page, 50, 700, "Fig. 1. System overview.")
    add_text(page, 50, 680, "See Figure 1 for details.")
    add_text(page, 50, 660, "Equation 3 describes the field.")
    return build_document_model([page])


def build_converter(**kwargs):
    from pdf2zh.converter import TranslateConverter
    from pdf2zh.collision_resolver import CollisionResolver
    from pdfminer.pdfinterp import PDFResourceManager
    from unittest.mock import patch
    translator = Mock()
    translator.translate = Mock(side_effect=lambda t: "YI" + t)
    translator.lang_in = "en"
    translator.lang_out = "zh-CN"
    with patch("pdf2zh.converter.build_translator") as bt:
        bt.return_value = translator
        conv = TranslateConverter(PDFResourceManager(), layout={},
                                  lang_in="en", lang_out="zh-CN",
                                  service="stub")
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
    for k, v in kwargs.items():
        setattr(conv, k, v)
    return conv


class TestSemanticGraph(unittest.TestCase):
    def test_sections_and_belongs_to(self):
        from pdf2zh.v3.semantic_graph import build_semantic_relations
        model = build_model()
        stats = build_semantic_relations(model)
        self.assertGreaterEqual(stats["sections"], 1)
        self.assertGreaterEqual(stats["belongs_to"], 1)
        sections = model.metadata["sections"]
        self.assertEqual(sections[0]["number"], "5")
        # 段落属于 5 Methodology 或 5.1
        members = [m for s in sections for m in s["members"]]
        self.assertTrue(members)
        rels = [r for r in model.relations if r.type == "belongs_to"]
        self.assertTrue(rels)

    def test_mentions_resolved(self):
        from pdf2zh.v3.semantic_graph import build_semantic_relations
        model = build_model()
        stats = build_semantic_relations(model)
        self.assertGreaterEqual(stats["mentions"], 1)
        mention = [r for r in model.relations if r.type == "mentions"]
        self.assertTrue(mention)
        # "See Figure 1" → caption 块 "Fig. 1. System overview."
        m = mention[0]
        target = [b for p in model.pages for b in p.blocks
                  if m.target == "p1_" + str(model.pages[0].blocks.index(b))]
        self.assertTrue(target)
        self.assertIn("Fig. 1", target[0].text)

    def test_detect_mentions_patterns(self):
        from pdf2zh.v3.semantic_graph import detect_mentions
        refs = detect_mentions("see Figure 3 and Table 2 and Eq.(4)")
        types = {r["target_type"] for r in refs}
        self.assertEqual(types, {"figure", "table", "equation"})

    def test_to_graph_semantic_edges(self):
        from pdf2zh.v3.semantic_graph import build_semantic_relations
        model = build_model()
        build_semantic_relations(model)
        g = model.to_graph()
        types = {e.edge_type.value for e in g.edges}
        self.assertIn("contains", types)   # belongs_to 投影
        self.assertIn("reference", types)  # mentions 投影


class TestDomainGlossary(unittest.TestCase):
    def test_detect_domain(self):
        from pdf2zh.v3.domain_glossary import detect_domain
        self.assertEqual(detect_domain("kernel scheduler thread"), "cs")
        self.assertEqual(detect_domain("lesion biopsy patient"), "medicine")

    def test_apply_glossary(self):
        from pdf2zh.v3.domain_glossary import DomainGlossary
        g = DomainGlossary(domains=["cs"])
        out = g.apply("The kernel schedules threads on the CPU")
        self.assertIn("内核", out)
        self.assertIn("线程", out)
        # 整词匹配：kernel32 不误替换
        self.assertEqual(g.apply("kernel32.dll"), "kernel32.dll")


class TestContextTranslation(unittest.TestCase):
    def test_context_fields(self):
        from pdf2zh.v3.context_translation import document_context_for
        from pdf2zh.v3.semantic_graph import build_semantic_relations
        model = build_model()
        build_semantic_relations(model)
        from pdf2zh.v3.doc_passes import TranslationPolicyPass
        TranslationPolicyPass().run(model)
        block = model.pages[0].blocks[1]  # 正文段
        ctx = document_context_for(model, block)
        self.assertEqual(ctx["type"], "paragraph")
        self.assertIn("domain", ctx)
        self.assertIn("policy", ctx)
        self.assertTrue(ctx["parent"])  # 属于某 section

    def test_translate_context_aware(self):
        from pdf2zh.v3.context_translation import (
            translate_document_context_aware,
        )
        from pdf2zh.v3.domain_glossary import DomainGlossary
        from pdf2zh.v3.doc_passes import TranslationPolicyPass
        from pdf2zh.v3.semantic_graph import build_semantic_relations
        model = build_model()
        build_semantic_relations(model)
        TranslationPolicyPass().run(model)
        seen = []
        def ctx_fn(text, context):
            seen.append(context)
            return "译:" + text
        stats = translate_document_context_aware(
            model, ctx_fn, glossary=DomainGlossary(domains=["cs"]))
        self.assertGreaterEqual(stats["translated"], 1)
        self.assertTrue(seen)
        # 上下文带 type/domain
        self.assertEqual(seen[0]["type"], "heading")

    def test_glossary_pinning_in_context_translation(self):
        from pdf2zh.v3.context_translation import (
            translate_document_context_aware,
        )
        from pdf2zh.v3.domain_glossary import DomainGlossary
        from pdf2zh.v3.doc_passes import TranslationPolicyPass
        model = build_model()
        TranslationPolicyPass().run(model)
        captured = []
        def ctx_fn(text, context):
            captured.append(text)
            return text
        translate_document_context_aware(
            model, ctx_fn, glossary=DomainGlossary(domains=["cs"]))
        # 含 kernel 的段落被钉死为「内核」后再进翻译器
        joined = " ".join(captured)
        self.assertIn("内核", joined)


class TestReferences(unittest.TestCase):
    def test_resolve_and_renumber(self):
        from pdf2zh.v3.references import renumber_references, resolve_references
        model = build_model()
        refs = resolve_references(model)
        self.assertTrue(refs)
        # Figure 1 → Figure 7（编号变化后重写正文）
        count = renumber_references(model, {("figure", "1"): "7"})
        self.assertGreaterEqual(count, 1)
        texts = [b.text for p in model.pages for b in p.blocks]
        self.assertTrue(any("Figure 7" in t for t in texts))
        self.assertFalse(any("Figure 1 for" in t for t in texts))

    def test_renumber_rewrites_translated(self):
        from pdf2zh.v3.references import renumber_references
        model = build_model()
        for b in model.pages[0].blocks:
            if "Figure 1 for" in (b.text or ""):
                b.metadata["translated"] = "见图1"
        count = renumber_references(model, {("figure", "1"): "7"})
        self.assertGreaterEqual(count, 1)
        translated = [b.metadata.get("translated") for p in model.pages
                      for b in p.blocks if b.metadata.get("translated")]
        self.assertTrue(any("图7" in t for t in translated))


class TestFigureUnderstanding(unittest.TestCase):
    def test_strategy_map(self):
        from pdf2zh.v3.figure_understanding import figure_strategy
        self.assertEqual(figure_strategy("photo"), "preserve")
        self.assertEqual(figure_strategy("screenshot"), "ocr_overlay")
        self.assertEqual(figure_strategy("chart"), "keep_labels")
        self.assertEqual(figure_strategy("diagram"), "ocr_redraw")
        self.assertEqual(figure_strategy("scanned"), "ocr_pipeline")
        self.assertEqual(figure_strategy("unknown"), "preserve")

    def test_annotate_figures(self):
        from pdf2zh.v3.figure_understanding import annotate_figures
        from pdf2zh.v3.doc_passes import SemanticPass
        model = build_model()
        SemanticPass().run(model)
        records = [
            {"page": 1, "object_id": "p1_x5", "image_class": "screenshot",
             "bbox": (50, 780, 300, 820),
             "decision": {"render_mode": "overlay"}},
        ]
        added = annotate_figures(model, records)
        self.assertEqual(added, 1)
        p1 = model.pages[0]
        figs = [b for b in p1.blocks if b.kind == "figure"]
        self.assertEqual(len(figs), 1)
        self.assertEqual(figs[0].metadata["strategy"], "ocr_overlay")
        self.assertFalse(figs[0].metadata["translate"])
        # 与题注的 caption_of 关系
        rels = [r for r in model.relations if r.type == "caption_of"]
        self.assertTrue(rels)


class TestIncremental(unittest.TestCase):
    def test_update_only_dirty(self):
        from pdf2zh.v3.incremental import IncrementalEngine
        model = build_model()
        engine = IncrementalEngine()
        engine.register(model)
        # 首次 update：全部 cached
        first = engine.update(model)
        self.assertEqual(first["dirty"], [])
        self.assertGreaterEqual(len(first["cached"]), 5)
        # 修改一个块 → 只有它 dirty
        target = model.pages[0].blocks[2]
        target.text = "The kernel scheduler runs threads here. Updated."
        second = engine.update(model)
        self.assertEqual(len(second["dirty"]), 1)
        self.assertEqual(second["dirty"][0], "p1_2")

    def test_rebuild_plan_only_dirty(self):
        from pdf2zh.v3.incremental import IncrementalEngine
        model = build_model()
        engine = IncrementalEngine()
        engine.register(model)
        model.pages[0].blocks[2].text = "changed"
        plan = engine.update(model)
        rebuild = engine.rebuild_plan(model, dirty_ids=plan["dirty"])
        self.assertEqual(len(rebuild), 1)
        self.assertEqual(rebuild[0]["block_id"], "p1_2")

    def test_node_hash_stability(self):
        from pdf2zh.v3.incremental import node_hash
        from pdf2zh.v3.canonical_page import BlockModel
        b1 = BlockModel(text="hello", kind="paragraph")
        b2 = BlockModel(text="hello", kind="paragraph")
        self.assertEqual(node_hash(b1), node_hash(b2))
        b2.kind = "heading"
        self.assertNotEqual(node_hash(b1), node_hash(b2))


class TestMainlineSemanticGraph(unittest.TestCase):
    def test_channel_runs_semantic_relations(self):
        from pdf2zh.v3.mainline_wiring import run_document_model
        conv = build_converter()
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 760, "5 Methodology", size=16)
        add_text(page, 50, 740, "The kernel scheduler runs here.")
        add_text(page, 50, 720, "See Figure 1 for details.")
        conv._gate_records = []
        run_document_model(conv, page)
        dm = conv.document_model
        self.assertIn("semantic_graph", dm.metadata)
        self.assertGreaterEqual(dm.metadata["semantic_graph"]["sections"], 1)
        self.assertIn("pass_report", dm.metadata)
        self.assertEqual(dm.metadata["pass_report"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
