# -*- coding: utf-8 -*-
"""V9.0 — 收尾模块单元测试（P1/P2 全量落锁）。

覆盖 render_advisor / geometry_merge / structure_fusion /
translation_advisor / ocr_engine / image_renderer / image_calibrate /
ir_convergence / toc_semantics 新语法 / Table-Reference Processors /
corpus_baseline synthetic 扩展。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, Edge, EdgeType, NodeType
from pdf2zh.v3.document_ir import SemanticRole, TranslationRole, RenderingRole
from pdf2zh.v3.content_preservation import ROLE_DEFAULT
from pdf2zh.v3.toc_semantics import (
    parse_toc_entry,
    TOCKind,
    TOCTranslationPolicy,
    toc_to_ir_records,
)


def node(nid, text="", ntype=NodeType.PARAGRAPH, page=0, bbox=(0, 0, 100, 20)):
    return DocumentNode(id=nid, node_type=ntype, bbox=bbox, text=text, page_num=page)


class TestRenderAdvisor(unittest.TestCase):
    def test_float_roles_preserve(self):
        from pdf2zh.v3.render_advisor import RenderAdvisor

        plan = RenderAdvisor().plan(
            gate_verdict={
                "writeback_allowed": True,
                "issues": [],
                "overlap_rate": 0.0,
                "page_height": 792.0,
            },
            snapshot_nodes=[{"id": "a", "role": "figure"}],
        )
        self.assertEqual(plan["routing"]["a"]["render_path"], "preserve_float")

    def test_overflow_blocked_when_gate_rejects(self):
        from pdf2zh.v3.render_advisor import RenderAdvisor

        plan = RenderAdvisor().plan(
            gate_verdict={
                "writeback_allowed": False,
                "issues": ["blocks overflow the page: [p3_0 p3_1]"],
                "overlap_rate": 0.1,
                "page_height": 792.0,
            },
            snapshot_nodes=[{"id": "p3_0", "role": "paragraph"}],
        )
        self.assertEqual(plan["routing"]["p3_0"]["render_path"], "block")
        self.assertFalse(plan["admissible"])

    def test_default_translate_refit(self):
        from pdf2zh.v3.render_advisor import RenderAdvisor

        plan = RenderAdvisor().plan(
            gate_verdict=None,
            snapshot_nodes=[{"id": "p", "role": "paragraph"}],
        )
        self.assertEqual(plan["routing"]["p"]["render_path"], "translate_refit")


class TestGeometryMerge(unittest.TestCase):
    def test_dice_similarity(self):
        from pdf2zh.v3.geometry_merge import dice_similarity

        self.assertAlmostEqual(dice_similarity("hello world", "hello world"), 1.0)
        self.assertAlmostEqual(dice_similarity("abc", "xyz"), 0.0)

    def test_rows_and_merge_consistent(self):
        from pdf2zh.v3.geometry_merge import (
            rows_from_geometry,
            merge_geometry_and_legacy,
        )
        from pdf2zh.v3.geometry import Char

        chars = [
            Char(text="A", x0=100, y0=700, x1=110, y1=712, page_num=1),
            Char(text="B", x0=112, y0=700, x1=122, y1=712, page_num=1),
        ]
        rows = rows_from_geometry(chars, page_num=1)
        self.assertTrue(rows)
        report = merge_geometry_and_legacy(chars, rows, page_num=1)
        self.assertTrue(report.consistent)
        self.assertGreaterEqual(report.text_similarity, 0.5)


class TestStructureFusion(unittest.TestCase):
    def test_lightweight_fuse_and_refine(self):
        from pdf2zh.v3.structure_fusion import StructureFusion

        g = DocumentGraph(
            [
                node("n1", text="Chapter 3: Results", bbox=(0, 0, 200, 24)),
                node("n2", text="Body paragraph here.", bbox=(0, 30, 200, 50)),
            ]
        )
        report = StructureFusion().fuse(g)
        self.assertTrue(report.notes or report.classified or report.refined >= 0)
        # 融合不改图节点数（单一 IR 纪律）
        self.assertEqual(len(g.nodes), 2)


class TestTranslationAdvisor(unittest.TestCase):
    def test_router_verdicts(self):
        from pdf2zh.v3.translation_advisor import (
            MainlineTranslationRouter,
            KEEP_ROUTE,
            TRANSLATE_ROUTE,
        )

        router = MainlineTranslationRouter()
        self.assertEqual(router.decide("").route, KEEP_ROUTE)
        self.assertEqual(router.decide("12345").route, KEEP_ROUTE)
        self.assertEqual(
            router.decide("A long sentence worth translating.").route, TRANSLATE_ROUTE
        )

    def test_processor_writes_policy(self):
        from pdf2zh.v3.processors import get_semantic, set_policy, POLICY_KEY
        from pdf2zh.v3.translation_advisor import TranslationAdvisorProcessor

        n = node("n1", text="Some body text to translate.")
        TranslationAdvisorProcessor().process(n, DocumentGraph())
        self.assertIn(POLICY_KEY, n.metadata)
        self.assertEqual(n.metadata[POLICY_KEY], "translate")


class TestOCREngine(unittest.TestCase):
    def test_deterministic_ocr(self):
        from pdf2zh.v3.ocr_engine import (
            DeterministicOCRBackend,
            ocr_into_pixels,
        )
        from pdf2zh.v3.image_engine import TextRegion

        regions = [TextRegion((0.1, 0.1, 0.4, 0.3))]
        out = ocr_into_pixels(None, regions, DeterministicOCRBackend())
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].text)

    def test_no_backend_is_passthrough(self):
        from pdf2zh.v3.ocr_engine import ocr_into_pixels
        from pdf2zh.v3.image_engine import TextRegion

        regions = [TextRegion((0.1, 0.1, 0.4, 0.3))]
        self.assertEqual(ocr_into_pixels(None, regions, None), regions)


class TestImageRenderer(unittest.TestCase):
    def test_render_modes_produce_bytes(self):
        from pdf2zh.v3.image_renderer import (
            render_preserve,
            render_region_replace,
            render_overlay,
            render_full_repaint,
            render_image_decision,
        )
        from pdf2zh.v3.image_engine import (
            RenderMode,
            TranslationDecision,
            TextRegion,
        )

        canvas = np.full((40, 60, 3), 255, dtype=np.uint8)
        self.assertTrue(render_preserve(canvas))
        regions = [TextRegion((0.1, 0.1, 0.4, 0.4)), TextRegion((0.5, 0.1, 0.9, 0.4))]
        plate = np.full((16, 24, 3), 180, dtype=np.uint8)
        self.assertTrue(render_region_replace(canvas, regions, plates={0: plate}))
        self.assertTrue(render_overlay(canvas, regions, plates={}))
        self.assertTrue(render_full_repaint(canvas, regions, plates={}))
        out = render_image_decision(
            canvas,
            decision=TranslationDecision(
                translate=True, render_mode=RenderMode.PRESERVE
            ),
            plates=None,
        )
        self.assertTrue(out)


class TestImageCalibrate(unittest.TestCase):
    def test_calibrate_accuracy(self):
        from pdf2zh.v3.image_calibrate import CalibrationSample, calibrate
        from pdf2zh.v3.image_engine import ImageClass

        samples = [
            CalibrationSample(
                {
                    "color_count": 250,
                    "unique_color_ratio": 0.4,
                    "aspect_ratio": 1.4,
                    "white_ratio": 0.05,
                    "edge_density": 0.05,
                },
                ImageClass.PHOTO,
            ),
            CalibrationSample(
                {
                    "color_count": 20,
                    "aspect_ratio": 1.2,
                    "white_ratio": 0.5,
                    "edge_density": 0.4,
                },
                ImageClass.CHART,
            ),
        ]
        report = calibrate(samples, grid={"photo_min_unique": (0.10, 0.50, 3)})
        self.assertAlmostEqual(report.baseline_accuracy, 1.0)
        self.assertGreaterEqual(report.best_accuracy, report.baseline_accuracy)
        self.assertGreater(report.grid_size, 0)


class TestIRConvergence(unittest.TestCase):
    def test_converged_snapshot_and_consistency(self):
        from pdf2zh.v3.ir_convergence import (
            converged_snapshot,
            snapshot_consistency,
            deprecated_note,
            DEPRECATED_VIEWS,
        )

        g = DocumentGraph([node("n1", text="Hello world", bbox=(0, 0, 200, 20))])
        snap = converged_snapshot(g, title="t")
        self.assertIn("node_count", snap)
        self.assertTrue(snapshot_consistency(snap, snap)["consistent"])
        self.assertIn("structure.to_document_ir", DEPRECATED_VIEWS)
        self.assertIn("DEPRECATED", deprecated_note())


class TestTOCSemanticsNewGrammar(unittest.TestCase):
    def test_bare_numbered_section(self):
        e = parse_toc_entry("1. Introduction")
        self.assertEqual(e.kind, TOCKind.SECTION)
        self.assertEqual(e.number, "1")
        self.assertEqual(e.title, "Introduction")
        self.assertTrue(e.matched)

    def test_section_sign(self):
        e = parse_toc_entry("\u00a72 Methods")
        self.assertEqual(e.kind, TOCKind.SECTION)
        self.assertEqual(e.number, "2")

    def test_zh_prefix(self):
        e = parse_toc_entry("\u7b2c3\u7ae0 \u7ed3\u679c")
        self.assertEqual(e.kind, TOCKind.CHAPTER)
        self.assertEqual(e.number, "3")

    def test_toc_to_ir_records(self):
        e = parse_toc_entry("Section 2 Results")
        recs = toc_to_ir_records([(e, "Results", "\u7b2c2\u8282 Results")], page_num=1)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "section")
        self.assertEqual(recs[0]["page_num"], 1)
        self.assertEqual(recs[0]["title_remainder"], "Results")


class TestTableReferenceProcessors(unittest.TestCase):
    def test_table_processor(self):
        from pdf2zh.v3.processors import TableNodeProcessor

        n = node("t", text="col1  col2  col3\nv1    v2    v3\nv4    v5    v6")
        TableNodeProcessor().process(n, DocumentGraph())
        self.assertEqual(n.node_type, NodeType.TABLE)

    def test_reference_processor_and_edge(self):
        from pdf2zh.v3.processors import ReferenceNodeProcessor

        g = DocumentGraph(
            [
                node("c1", text="[1, 2]", page=1, bbox=(0, 60, 100, 70)),
                node("b1", text="References", page=1, bbox=(0, 50, 100, 58)),
            ]
        )
        for n in list(g.nodes):
            ReferenceNodeProcessor().process(n, g)
        ReferenceNodeProcessor().finalize(g)
        self.assertTrue(any(e.edge_type == EdgeType.CITATION_OF for e in g.edges))


class TestCorpusBaselineSynthetic(unittest.TestCase):
    def test_synthetic_build_and_diff(self):
        from pdf2zh.corpus_baseline import build_synthetic_corpus, diff_corpora

        d1 = tempfile.mkdtemp(prefix="syn_a_")
        d2 = tempfile.mkdtemp(prefix="syn_b_")
        build_synthetic_corpus(d1, count=3, seed=42)
        build_synthetic_corpus(d2, count=3, seed=42)
        diffs = diff_corpora(d1, d2)
        self.assertEqual(len(diffs), 3)
        self.assertTrue(all(d["consistent"] for d in diffs))

    def test_scale_100_docs_deterministic(self):
        """P2 语料规模扩充：100 份合成文档双份构建 → 逐桶全一致。"""
        from pdf2zh.corpus_baseline import build_synthetic_corpus, diff_corpora

        d1 = tempfile.mkdtemp(prefix="syn_100_a_")
        d2 = tempfile.mkdtemp(prefix="syn_100_b_")
        m1 = build_synthetic_corpus(d1, count=100, seed=7)
        m2 = build_synthetic_corpus(d2, count=100, seed=7)
        self.assertEqual(len(m1), 100)
        self.assertEqual(len(m2), 100)
        diffs = diff_corpora(d1, d2)
        self.assertEqual(len(diffs), 100)
        self.assertTrue(
            all(d["consistent"] for d in diffs),
            [d for d in diffs if not d["consistent"]][:3],
        )


if __name__ == "__main__":
    unittest.main()
