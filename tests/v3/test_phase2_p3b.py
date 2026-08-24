"""Headless tests for Phase 2 - P3b: Session + InlineLayout + ColumnLayout."""

from __future__ import annotations
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.memory import DocumentMemory
from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig

B = (0, 0, 0, 0)


class TestSession(unittest.TestCase):
    def test_provider_attr(self):
        from pdf2zh.v3.translator import TranslationSession

        s = TranslationSession(graph=DocumentGraph())
        self.assertIsNotNone(s.provider)

    def test_post_processor_attr(self):
        from pdf2zh.v3.translator import TranslationSession

        s = TranslationSession(graph=DocumentGraph())
        self.assertIsNotNone(s.post_processor)

    def test_stats_default(self):
        from pdf2zh.v3.translator import TranslationSession, TranslationStats

        s = TranslationSession(graph=DocumentGraph())
        self.assertIsInstance(s.stats, TranslationStats)

    def test_record_translated(self):
        from pdf2zh.v3.translator import TranslationSession

        s = TranslationSession(graph=DocumentGraph())
        s.record_translated("n1", latency_ms=50, tokens=25)
        self.assertEqual(s.stats.translated, 1)
        self.assertEqual(s.stats.total_latency_ms, 50)

    def test_summary_has_stats(self):
        from pdf2zh.v3.translator import TranslationSession

        s = TranslationSession(graph=DocumentGraph())
        self.assertIn("stats", s.summary())

    def test_on_translate(self):
        from pdf2zh.v3.translator import TranslationSession

        s = TranslationSession(graph=DocumentGraph())
        calls = []
        s.on_translate(lambda n, t: calls.append((n, t)))
        self.assertTrue(hasattr(s, "_on_translate"))

    def test_custom_provider(self):
        from pdf2zh.v3.translator import TranslationSession, MockLLMProvider

        p = MockLLMProvider(delay_ms=0.0)
        s = TranslationSession(graph=DocumentGraph(), provider=p)
        self.assertIs(s.provider, p)

    def test_custom_post_processor(self):
        from pdf2zh.v3.translator import TranslationSession, PostProcessor

        pp = PostProcessor(DocumentMemory())
        s = TranslationSession(graph=DocumentGraph(), post_processor=pp)
        self.assertIs(s.post_processor, pp)

    def test_translator_uses_session(self):
        from pdf2zh.v3.translator import TranslationSession, Translator

        graph = DocumentGraph()
        graph.add_node(
            DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="test")
        )
        s = TranslationSession(graph=graph)
        t = Translator(s)
        self.assertIsNotNone(t.translate_node("n1"))

    def test_translate_all(self):
        from pdf2zh.v3.translator import TranslationSession, Translator

        graph = DocumentGraph()
        graph.add_node(
            DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="Hello")
        )
        graph.add_node(
            DocumentNode(id="n2", node_type=NodeType.PARAGRAPH, bbox=B, text="World")
        )
        s = TranslationSession(graph=graph)
        t = Translator(s)
        results = t.translate_all()
        self.assertEqual(len(results), 2)
        self.assertIn("n1", results)


class TestInlineLayout(unittest.TestCase):
    def test_char_cjk(self):
        from pdf2zh.v3.layout import InlineLayout, GlyphMetric

        m = InlineLayout.measure_char("中", 12.0)
        self.assertIsInstance(m, GlyphMetric)
        self.assertTrue(m.is_cjk)

    def test_char_ascii(self):
        from pdf2zh.v3.layout import InlineLayout

        m = InlineLayout.measure_char("A", 12.0)
        self.assertFalse(m.is_cjk)
        self.assertAlmostEqual(m.width, 7.0)

    def test_measure_word(self):
        from pdf2zh.v3.layout import InlineLayout

        self.assertGreater(InlineLayout.measure_word("Hello", 12.0), 0)

    def test_break_line(self):
        from pdf2zh.v3.layout import InlineLayout

        self.assertEqual(InlineLayout.break_line("Hi World", 200, 12.0), ["Hi World"])

    def test_break_line_overflow(self):
        from pdf2zh.v3.layout import InlineLayout

        self.assertGreater(
            len(InlineLayout.break_line("Hello World Test", 50, 12.0)), 1
        )

    def test_break_line_empty(self):
        from pdf2zh.v3.layout import InlineLayout

        self.assertEqual(InlineLayout.break_line("", 100, 12.0), [""])


class TestColumnLayout(unittest.TestCase):
    def test_single(self):
        from pdf2zh.v3.layout import ColumnLayout

        n = DocumentNode(
            id="n1", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 100, 50), text="x"
        )
        self.assertEqual(ColumnLayout.detect_columns([n], 612)[0], 1)

    def test_two(self):
        n1 = DocumentNode(
            id="n1", node_type=NodeType.PARAGRAPH, bbox=(50, 100, 250, 150), text="left"
        )
        n2 = DocumentNode(
            id="n2",
            node_type=NodeType.PARAGRAPH,
            bbox=(350, 100, 550, 150),
            text="right",
        )
        from pdf2zh.v3.layout import ColumnLayout

        self.assertEqual(ColumnLayout.detect_columns([n1, n2], 612)[0], 2)

    def test_no_nodes(self):
        from pdf2zh.v3.layout import ColumnLayout

        self.assertEqual(ColumnLayout.detect_columns([], 612)[0], 1)

    def test_assign_left(self):
        from pdf2zh.v3.layout import ColumnLayout, ColumnRegion

        n = DocumentNode(
            id="n1", node_type=NodeType.PARAGRAPH, bbox=(60, 100, 160, 150), text="x"
        )
        cols = [ColumnRegion(x=50, w=200), ColumnRegion(x=350, w=200)]
        self.assertEqual(ColumnLayout.assign_to_column(n, cols), 0)

    def test_assign_right(self):
        from pdf2zh.v3.layout import ColumnLayout, ColumnRegion

        n = DocumentNode(
            id="n1", node_type=NodeType.PARAGRAPH, bbox=(400, 100, 500, 150), text="x"
        )
        cols = [ColumnRegion(x=50, w=200), ColumnRegion(x=350, w=200)]
        self.assertEqual(ColumnLayout.assign_to_column(n, cols), 1)
