import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
    from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
    _HAS = True
except ImportError as e:
    _HAS = False
    print(f"Analyzer import error: {e}")

@unittest.skipIf(not _HAS, "not importable")
class TestAnalyzerConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = AnalyzerConfig()
        self.assertTrue(cfg.refine_heading_levels)
        self.assertTrue(cfg.detect_formulas)
        self.assertEqual(cfg.lang_in, "auto")
    def test_custom(self):
        cfg = AnalyzerConfig(detect_formulas=False)
        self.assertFalse(cfg.detect_formulas)

@unittest.skipIf(not _HAS, "not importable")
class TestSemanticAnalyzer(unittest.TestCase):
    def setUp(self):
        self.ana = SemanticAnalyzer()
        self.g = DocumentGraph()
        self.g.add_node(DocumentNode("p1", NodeType.PARAGRAPH, (50, 50, 500, 70), text="para1"))
    def test_instantiate(self):
        self.assertIsNotNone(self.ana)
    def test_analyze_empty(self):
        g = DocumentGraph()
        self.assertIs(self.ana.analyze(g), g)
    def test_analyze_preserves(self):
        r = self.ana.analyze(self.g)
        self.assertIsNotNone(r.get_node("p1"))
    def test_analyze_heading(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("h1", NodeType.PARAGRAPH, (50, 50, 500, 65), text="Intro", metadata={"font_size": 18}))
        self.ana.analyze(g)
        self.assertIsNotNone(g.get_node("h1"))
    def test_analyze_caption(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("c1", NodeType.PARAGRAPH, (50, 50, 500, 65), text="Figure 1: test"))
        self.ana.analyze(g)
        self.assertIsNotNone(g.get_node("c1"))
    def test_analyze_formula(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("f1", NodeType.PARAGRAPH, (50, 50, 500, 80), text="y = mx + b"))
        self.ana.analyze(g)
        self.assertIsNotNone(g.get_node("f1"))
    def test_analyze_empty_text(self):
        g = DocumentGraph()
        g.add_node(DocumentNode("e1", NodeType.PARAGRAPH, (50, 50, 500, 70), text=""))
        self.ana.analyze(g)
        self.assertIsNotNone(g.get_node("e1"))
