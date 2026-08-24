"""Tests for V3 Legacy Adapter (Module: legacy_adapter.py)."""

import pytest
from pdf2zh.v3.legacy_adapter import (
    AdapterStats,
    BaseAdapter,
    LegacyTranslatorAdapter,
    LegacyLayoutAdapter,
    LegacyRendererAdapter,
    LegacyConverterBridge,
)
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.visual_tree import VisualTree, Page, BoundingBox


class TestAdapterStats:
    def test_defaults(self):
        s = AdapterStats()
        assert s.nodes_processed == 0
        assert s.nodes_adapted == 0
        assert s.errors == 0

    def test_to_dict(self):
        s = AdapterStats(
            adapter_name="test", nodes_processed=10, nodes_adapted=8, errors=1
        )
        d = s.to_dict()
        assert d["adapter"] == "test"
        assert d["processed"] == 10
        assert d["adapted"] == 8
        assert d["errors"] == 1


class TestBaseAdapter:
    def test_init(self):
        a = BaseAdapter("my_adapter")
        assert a.stats.adapter_name == "my_adapter"

    def test_reset_stats(self):
        a = BaseAdapter("test")
        a._stats.nodes_processed = 100
        a.reset_stats()
        assert a.stats.nodes_processed == 0


class TestLegacyTranslatorAdapter:
    def test_init(self):
        a = LegacyTranslatorAdapter()
        assert a.stats.adapter_name == "LegacyTranslatorAdapter(mock)"

    def test_init_with_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            LegacyTranslatorAdapter(engine_name="nonexistent_999")

    def test_init_with_known_engine(self):
        engines = LegacyTranslatorAdapter._discover_engines()
        if "google" in engines:
            a = LegacyTranslatorAdapter(engine_name="google")
            assert "google" in a.stats.adapter_name.lower()

    def test_discover_engines_returns_dict(self):
        engines = LegacyTranslatorAdapter._discover_engines()
        assert isinstance(engines, dict)

    def test_translate_empty_graph(self):
        a = LegacyTranslatorAdapter()
        g = DocumentGraph()
        result = a.translate(g)
        assert isinstance(result, DocumentGraph)

    def test_translate_with_default(self):
        a = LegacyTranslatorAdapter()
        g = DocumentGraph()
        n = DocumentNode(
            id="n1",
            node_type=NodeType.PARAGRAPH,
            bbox=(0, 0, 100, 50),
            text="Hello world",
        )
        g.add_node(n)
        result = a.translate(g)
        assert result.get_node("n1") is not None

    def test_stats_updated(self):
        a = LegacyTranslatorAdapter()
        g = DocumentGraph()
        for i in range(5):
            g.add_node(
                DocumentNode(
                    id=f"n{i}",
                    node_type=NodeType.PARAGRAPH,
                    bbox=(0, 0, 100, 50),
                    text=f"Text {i}",
                )
            )
        a.translate(g)
        assert a.stats.nodes_processed == 5


class TestLegacyLayoutAdapter:
    def test_init(self):
        a = LegacyLayoutAdapter()
        assert a.stats.adapter_name == "LegacyLayoutAdapter"

    def test_layout_empty_graph(self):
        a = LegacyLayoutAdapter()
        g = DocumentGraph()
        result = a.layout(g)

    def test_layout_with_nodes(self):
        a = LegacyLayoutAdapter()
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                id="n1", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 100, 50), text="Test"
            )
        )
        a.layout(g)


class TestLegacyRendererAdapter:
    def test_init(self):
        a = LegacyRendererAdapter()
        assert a.stats.adapter_name == "LegacyRendererAdapter"

    def test_render_empty_tree(self):
        a = LegacyRendererAdapter()
        tree = VisualTree()
        result = a.render(tree, "out.pdf")
        assert isinstance(result, str)


class TestLegacyConverterBridge:
    def test_init(self):
        b = LegacyConverterBridge()
        assert b.stats["calls"] == 0

    def test_convert_empty(self):
        b = LegacyConverterBridge()
        g = DocumentGraph()
        result = b.convert(g)
        assert isinstance(result, DocumentGraph)
        assert b.stats["calls"] == 1

    def test_adapter_stats(self):
        b = LegacyConverterBridge()
        s = b.get_adapter_stats()
        assert "translator" in s
        assert "layout" in s
        assert "renderer" in s

    def test_convert_with_nodes(self):
        b = LegacyConverterBridge()
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                id="n1",
                node_type=NodeType.PARAGRAPH,
                bbox=(0, 0, 100, 50),
                text="Hello",
            )
        )
        g.add_node(
            DocumentNode(
                id="n2", node_type=NodeType.HEADING, bbox=(0, 0, 100, 50), text="Title"
            )
        )
        result = b.convert(g)
        assert result.get_node("n1") is not None
