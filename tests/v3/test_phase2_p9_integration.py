# -*- coding: utf-8 -*-
"""Integration tests for Phase 2 pipeline components."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType, Edge, EdgeType
    from pdf2zh.v3.scheduler import TaskGraph, Task, Executor
    _HAS = True
except ImportError as e:
    _HAS = False
    print(f"Integration import error: {e}")

@unittest.skipIf(not _HAS, "not importable")
class TestPipelineComposition(unittest.TestCase):
    def test_graph_add_nodes(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("n1", NodeType.PARAGRAPH, (0, 0, 100, 20), text="Hello"))
        g.add_node(DocumentNode("n2", NodeType.PARAGRAPH, (0, 30, 100, 50), text="World"))
        self.assertEqual(len(g.nodes), 2)

    def test_graph_get_node(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("n1", NodeType.PARAGRAPH, (0, 0, 100, 20)))
        self.assertIsNotNone(g.get_node("n1"))

    def test_graph_add_edge(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("n1", NodeType.PARAGRAPH, (0, 0, 100, 20)))
        g.add_node(DocumentNode("n2", NodeType.PARAGRAPH, (0, 30, 100, 50)))
        g.add_edge(Edge("n1", "n2", EdgeType.FOLLOWS))
        self.assertEqual(len(g.get_edges()), 1)

    def test_pipeline_task_graph(self):
        tg = TaskGraph()
        tg.add_task(Task("parse", "Parse", module="parser"))
        tg.add_task(Task("analyze", "Analyze", module="analyzer", dependencies={"parse"}))
        tg.add_task(Task("translate", "Translate", module="translator", dependencies={"analyze"}))
        exec_ = Executor(tg)
        results = exec_.run_all()
        self.assertEqual(len(results), 3)

    def test_pipeline_topo_order(self):
        tg = TaskGraph()
        tg.add_task(Task("a", "A", priority=10))
        tg.add_task(Task("b", "B", priority=20, dependencies={"a"}))
        tg.add_task(Task("c", "C", priority=30, dependencies={"b"}))
        order = [t.id for t in tg.topological_sort()]
        self.assertEqual(order, ["a", "b", "c"])

    def test_graph_edge_with_weight(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("a", NodeType.PARAGRAPH, (0, 0, 100, 20)))
        g.add_node(DocumentNode("b", NodeType.PARAGRAPH, (0, 30, 100, 50)))
        g.add_edge(Edge("a", "b", EdgeType.FOLLOWS, weight=0.8))
        self.assertEqual(g.get_edges()[0].weight, 0.8)

    def test_graph_get_nodes_by_type(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("p1", NodeType.PARAGRAPH, (0, 0, 100, 20)))
        g.add_node(DocumentNode("h1", NodeType.HEADING, (0, 30, 100, 50)))
        paras = g.get_nodes_by_type(NodeType.PARAGRAPH)
        self.assertEqual(len(paras), 1)

    def test_node_width_height(self):
        n = DocumentNode("n1", NodeType.PARAGRAPH, (10, 20, 110, 70))
        self.assertAlmostEqual(n.width, 100.0)
        self.assertAlmostEqual(n.height, 50.0)

    def test_node_coords(self):
        n = DocumentNode("n1", NodeType.PARAGRAPH, (10, 20, 110, 70))
        self.assertAlmostEqual(n.x0, 10.0)
        self.assertAlmostEqual(n.y0, 20.0)

    def test_node_metadata(self):
        n = DocumentNode("n1", NodeType.PARAGRAPH, (0, 0, 100, 20), metadata={"key": "val"})
        self.assertEqual(n.metadata["key"], "val")
