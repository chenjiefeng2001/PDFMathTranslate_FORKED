"""Phase 2 P6: Legacy Adapter tests."""

import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
_HAS_V3 = False
try:
    from pdf2zh.v3.legacy_adapter import (
        AdapterStats,
        BaseAdapter,
        LegacyTranslatorAdapter,
        LegacyLayoutAdapter,
        LegacyRendererAdapter,
        LegacyConverterBridge,
    )
    from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType

    _HAS_V3 = True
except ImportError as e:
    print(f"Adapter import error: {e}")


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestAdapterStats(unittest.TestCase):
    def test_defaults(self):
        s = AdapterStats(adapter_name="test")
        self.assertEqual(s.nodes_processed, 0)
        self.assertEqual(s.adapter_name, "test")

    def test_to_dict(self):
        s = AdapterStats(adapter_name="t", nodes_processed=10, nodes_adapted=5)
        d = s.to_dict()
        self.assertEqual(d["adapter"], "t")
        self.assertEqual(d["processed"], 10)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestBaseAdapter(unittest.TestCase):
    def test_init(self):
        ba = BaseAdapter(name="base")
        self.assertEqual(ba.stats.adapter_name, "base")

    def test_reset_stats(self):
        ba = BaseAdapter(name="test")
        ba._stats.nodes_processed = 100
        ba.reset_stats()
        self.assertEqual(ba.stats.nodes_processed, 0)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyTranslatorAdapter(unittest.TestCase):
    def test_init(self):
        adapter = LegacyTranslatorAdapter()
        self.assertEqual(adapter.stats.adapter_name, "LegacyTranslatorAdapter(mock)")

    def test_translate_empty_graph(self):
        adapter = LegacyTranslatorAdapter()
        g = DocumentGraph()
        result = adapter.translate(g)
        self.assertIs(result, g)
        self.assertEqual(adapter.stats.nodes_processed, 0)

    def test_translate_with_nodes(self):
        adapter = LegacyTranslatorAdapter()
        g = DocumentGraph()
        g.add_node(
            DocumentNode("n1", NodeType.PARAGRAPH, (0, 0, 100, 20), text="Hello")
        )
        result = adapter.translate(g)
        self.assertEqual(adapter.stats.nodes_processed, 1)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyLayoutAdapter(unittest.TestCase):
    def test_init(self):
        adapter = LegacyLayoutAdapter()
        self.assertEqual(adapter.stats.adapter_name, "LegacyLayoutAdapter")

    def test_layout_empty(self):
        from pdf2zh.v3.visual_tree import VisualTree

        adapter = LegacyLayoutAdapter()
        from pdf2zh.v3.graph import DocumentGraph

        g = DocumentGraph()
        result = adapter.layout(g)
        self.assertIsNotNone(result)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyRendererAdapter(unittest.TestCase):
    def test_init(self):
        adapter = LegacyRendererAdapter()
        self.assertEqual(adapter.stats.adapter_name, "LegacyRendererAdapter")

    def test_render_empty_tree(self):
        from pdf2zh.v3.visual_tree import VisualTree

        adapter = LegacyRendererAdapter()
        vt = VisualTree()
        result = adapter.render(vt, "test_output.pdf")
        self.assertIsInstance(result, (str, bytes))


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyConverterBridge(unittest.TestCase):
    def test_init(self):
        bridge = LegacyConverterBridge()
        self.assertEqual(bridge.stats["calls"], 0)

    def test_convert_empty_graph(self):
        bridge = LegacyConverterBridge()
        g = DocumentGraph()
        result = bridge.convert(g)
        self.assertEqual(bridge.stats["calls"], 1)

    def test_stats(self):
        bridge = LegacyConverterBridge()
        self.assertIn("calls", bridge.stats)
        self.assertIn("translator", bridge.stats)

    def test_get_adapter_stats(self):
        bridge = LegacyConverterBridge()
        stats = bridge.get_adapter_stats()
        self.assertIn("translator", stats)
        self.assertIn("layout", stats)
        self.assertIn("renderer", stats)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyTranslatorAdapterWithGraph(unittest.TestCase):
    def _make_graph_with_text(self):
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                "p1",
                NodeType.PARAGRAPH,
                (0, 0, 200, 30),
                text="Test para 1.",
                page_num=0,
            )
        )
        g.add_node(
            DocumentNode(
                "p2",
                NodeType.PARAGRAPH,
                (0, 40, 200, 70),
                text="Test para 2.",
                page_num=0,
            )
        )
        g.add_node(
            DocumentNode(
                "h1",
                NodeType.HEADING,
                (0, 80, 200, 100),
                text="Test Heading",
                page_num=0,
            )
        )
        return g

    def test_translate_graph_with_v3_mock(self):
        adapter = LegacyTranslatorAdapter()
        g = self._make_graph_with_text()
        result = adapter.translate(g)
        self.assertEqual(adapter.stats.nodes_processed, 3)
        for node in result.nodes:
            self.assertIsNotNone(node.translated_text)

    def test_translate_with_custom_kwargs(self):
        adapter = LegacyTranslatorAdapter()
        g = self._make_graph_with_text()
        result = adapter.translate(g, source_lang="en", target_lang="zh")
        self.assertEqual(adapter.stats.nodes_processed, 3)


@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestLegacyConverterBridgeIntegration(unittest.TestCase):
    def test_full_pipeline_tracking(self):
        bridge = LegacyConverterBridge()
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                "n1", NodeType.PARAGRAPH, (0, 0, 100, 20), text="Test", page_num=0
            )
        )
        bridge.convert(g)
        stats = bridge.get_adapter_stats()
        self.assertEqual(
            stats["translator"]["adapter"], "LegacyTranslatorAdapter(mock)"
        )
        self.assertIn("processed", stats["translator"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
