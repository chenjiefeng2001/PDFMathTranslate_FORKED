"""Tests for LayoutGraph / DAG-based reading order (Phase 3)."""
import unittest
from pdf2zh.layout_graph import LayoutGraph, TextNode


class TestLayoutGraph(unittest.TestCase):
    """Test DAG-based reading order analysis."""

    def setUp(self):
        self.graph = LayoutGraph()

    def test_empty_graph(self):
        """Empty graph should return empty list."""
        result = self.graph.topological_sort()
        self.assertEqual(result, [])

    def test_single_node(self):
        """Single node graph should return that node."""
        self.graph.add_node(TextNode(id=0, x0=0, y0=100, x1=100, y1=200))
        result = self.graph.topological_sort()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 0)

    def test_spatial_sort_single_column(self):
        """Nodes in one column should sort top-to-bottom."""
        nodes = [
            TextNode(id=0, x0=0, y0=300, x1=100, y1=400),  # Top(y=300)
            TextNode(id=1, x0=0, y0=100, x1=100, y1=200),  # Bottom(y=100)
            TextNode(id=2, x0=0, y0=200, x1=100, y1=300),  # Middle(y=200)
        ]
        for n in nodes:
            self.graph.add_node(n)
        result = self.graph.topological_sort()
        # Should be top-to-bottom: 1, 2, 0
        self.assertEqual(result[0].id, 0)  # top
        self.assertEqual(result[1].id, 2)  # middle
        self.assertEqual(result[2].id, 1)  # bottom

    def test_spatial_sort_multi_column(self):
        """Nodes in two columns should sort left-to-right, then top-to-bottom."""
        nodes = [
            # Left column
            TextNode(id=0, x0=0, y0=300, x1=100, y1=400),  # Bottom-left
            TextNode(id=1, x0=0, y0=100, x1=100, y1=200),  # Bottom-left(y=100)
            # Right column
            TextNode(id=2, x0=200, y0=300, x1=300, y1=400),  # Top-right(y=300)
            TextNode(id=3, x0=200, y0=100, x1=300, y1=200),  # Bottom-right(y=100)
        ]
        for n in nodes:
            self.graph.add_node(n)
        result = self.graph.topological_sort()
        # Should be left column top-to-bottom, then right column top-to-bottom
        self.assertEqual(result[0].id, 0)  # Top-left
        self.assertEqual(result[1].id, 1)  # Bottom-left
        self.assertEqual(result[2].id, 2)  # Top-right
        self.assertEqual(result[3].id, 3)  # Bottom-right

    def test_topological_sort_with_edges(self):
        """Explicit edges should determine reading order."""
        nodes = [
            TextNode(id=0, x0=0, y0=300, x1=100, y1=400),  # Bottom
            TextNode(id=1, x0=0, y0=100, x1=100, y1=200),  # Top
        ]
        for n in nodes:
            self.graph.add_node(n)
        self.graph.add_edge(0, 1)  # Bottom before top (unusual)
        result = self.graph.topological_sort()
        self.assertEqual(result[0].id, 0)
        self.assertEqual(result[1].id, 1)

    def test_cycle_detection_falls_back(self):
        """Graph with cycle should fall back to spatial sort."""
        nodes = [
            TextNode(id=0, x0=0, y0=100, x1=100, y1=200),
            TextNode(id=1, x0=0, y0=200, x1=100, y1=300),
        ]
        for n in nodes:
            self.graph.add_node(n)
        self.graph.add_edge(0, 1)
        self.graph.add_edge(1, 0)  # Back edge creates cycle
        result = self.graph.topological_sort()
        self.assertEqual(len(result), 2)  # Should still work

    def test_detect_multi_column_single(self):
        """Single column should detect as 1 column."""
        self.graph.add_node(TextNode(id=0, x0=0, y0=100, x1=100, y1=200))
        self.graph.add_node(TextNode(id=1, x0=0, y0=200, x1=100, y1=300))
        self.assertEqual(self.graph.detect_multi_column(), 1)

    def test_detect_multi_column_two(self):
        """Two non-overlapping column blocks should detect as 2 columns."""
        self.graph.add_node(TextNode(id=0, x0=0, y0=100, x1=100, y1=200))
        self.graph.add_node(TextNode(id=1, x0=300, y0=100, x1=400, y1=200))
        cols = self.graph.detect_multi_column()
        self.assertEqual(cols, 2, f"Expected 2, got {cols}. Intervals: {[(n.x0,n.x1) for n in self.graph.nodes]}")

    def test_detect_multi_column_empty(self):
        """Empty graph should detect as 1 column."""
        self.assertEqual(self.graph.detect_multi_column(), 1)


class TestTextNode(unittest.TestCase):
    """Test TextNode data class."""

    def test_default_values(self):
        """TextNode should have sensible defaults."""
        node = TextNode(id=0, x0=0, y0=0, x1=100, y1=200)
        self.assertEqual(node.text, "")
        self.assertEqual(node.font_size, 0.0)
        self.assertEqual(node.page_num, 0)


if __name__ == "__main__":
    unittest.main()
