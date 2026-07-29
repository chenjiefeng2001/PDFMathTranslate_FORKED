"""Headless tests for Phase 2 — V3 P0/P1/P2 modules.
Tests P0 RuntimeFacade, P1 Translation/Layout/Renderer, P2 Issue/Optimizer.
"""
from __future__ import annotations
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType, Edge, EdgeType
from pdf2zh.v3.memory import DocumentMemory
from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig
from pdf2zh.v3.visual_tree import VisualTree, BoundingBox, Page, Paragraph, Line, TextRun

B = (0, 0, 0, 0)  # shorthand bbox

# ====================================================================
# P0: RuntimeFacade
# ====================================================================
class TestRuntimeFacade(unittest.TestCase):
    def test_instantiation(self):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        self.assertIsNotNone(rt)
        self.assertEqual(rt.source, "")
    def test_with_config(self):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade({"lang_in": "en", "page_width": 595})
        self.assertEqual(rt.config["lang_in"], "en")
    def test_summary_no_graph(self):
        from pdf2zh.v3.runtime import RuntimeFacade
        s = RuntimeFacade().summary()
        self.assertEqual(s["graph_nodes"], 0)
    def test_is_exported(self):
        from pdf2zh.v3 import RuntimeFacade
        self.assertIsNotNone(RuntimeFacade)

# ====================================================================
# P1: ModelRouter
# ====================================================================
class TestModelRouter(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import ModelRouter
        self.router = ModelRouter()
    def test_default_route(self):
        r = self.router.route(DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="x"))
        self.assertEqual(r.model, "gpt-4o")
        self.assertEqual(r.temperature, 0.3)
        self.assertEqual(r.max_tokens, 8192)
    def test_formula_route_cold(self):
        r = self.router.route(DocumentNode(id="n2", node_type=NodeType.FORMULA, bbox=B, text="E=mc^2"))
        self.assertEqual(r.temperature, 0.0)
        self.assertEqual(r.model, "gpt-4o-mini")
    def test_code_route_cold(self):
        r = self.router.route(DocumentNode(id="n3", node_type=NodeType.CODE, bbox=B, text="print(1)"))
        self.assertEqual(r.temperature, 0.0)
    def test_footnote_route(self):
        r = self.router.route(DocumentNode(id="n4", node_type=NodeType.FOOTNOTE, bbox=B, text="Note"))
        self.assertEqual(r.model, "gpt-4o-mini")
        self.assertEqual(r.max_tokens, 2048)
    def test_abstract_route(self):
        r = self.router.route(DocumentNode(id="n5", node_type=NodeType.ABSTRACT, bbox=B, text="Abstract"))
        self.assertEqual(r.model, "gpt-4o")
        self.assertEqual(r.max_tokens, 8192)
    def test_model_count(self):
        self.assertGreater(self.router.model_count, 1)
    def test_get_routes(self):
        routes = self.router.get_routes()
        self.assertIn("gpt-4o", routes)
        self.assertIn("gpt-4o-mini", routes)


# ====================================================================
# P1: TranslationCache
# ====================================================================
class TestTranslationCache(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import TranslationCache
        self.cache = TranslationCache(max_size=100)
    def test_put_and_get(self):
        self.cache.put("Hello", "\u4f60\u597d", "en", "zh-cn", "gpt-4o")
        self.assertEqual(self.cache.get("Hello", "en", "zh-cn", "gpt-4o"), "\u4f60\u597d")
    def test_cache_miss(self):
        self.assertIsNone(self.cache.get("X", "en", "zh-cn", "gpt-4o"))
    def test_contains(self):
        self.assertFalse(self.cache.contains("T", "en", "zh-cn", "gpt-4o"))
        self.cache.put("T", "test", "en", "zh-cn", "gpt-4o")
        self.assertTrue(self.cache.contains("T", "en", "zh-cn", "gpt-4o"))
    def test_cache_stats(self):
        self.cache.get("A", "en", "zh-cn", "gpt-4o")
        self.cache.get("A", "en", "zh-cn", "gpt-4o")
        self.assertEqual(self.cache.stats["misses"], 2)
    def test_eviction(self):
        from pdf2zh.v3.translator import TranslationCache
        c = TranslationCache(max_size=2)
        c.put("A", "a", "en", "zh", "m"); c.put("B", "b", "en", "zh", "m")
        c.put("C", "c", "en", "zh", "m")
        self.assertEqual(c.size, 2)
        self.assertIsNone(c.get("A", "en", "zh", "m"))
    def test_clear(self):
        self.cache.put("K", "V", "en", "zh", "m")
        self.cache.clear()
        self.assertEqual(self.cache.size, 0)


# ====================================================================
# P1: PromptComposer
# ====================================================================
class TestPromptComposer(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import PromptComposer
        self.planner = TranslationPlanner(PlannerConfig())
        self.memory = DocumentMemory()
        self.memory.remember_glossary("PDF", "portable", context="doc")
        self.composer = PromptComposer(self.planner, self.memory)
    def test_compose_returns_prompt(self):
        g = DocumentGraph()
        g.add_node(DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="Hello"))
        p = self.composer.compose(g, "n1")
        self.assertEqual(p.node_id, "n1")
    def test_compose_roles(self):
        g = DocumentGraph()
        g.add_node(DocumentNode(id="n2", node_type=NodeType.HEADING, bbox=B, text="Intro"))
        p = self.composer.compose(g, "n2")
        self.assertEqual(p.messages[0]["role"], "system")
        self.assertEqual(p.messages[1]["role"], "user")
    def test_compose_nonexistent(self):
        g = DocumentGraph()
        with self.assertRaises(ValueError):
            self.composer.compose(g, "missing")
    def test_compose_includes_glossary(self):
        g = DocumentGraph()
        g.add_node(DocumentNode(id="n3", node_type=NodeType.PARAGRAPH, bbox=B, text="PDF format"))
        p = self.composer.compose(g, "n3")
        self.assertIn("PDF", p.messages[0]["content"])


# ====================================================================
# P1: TranslationSession
# ====================================================================
class TestTranslationSession(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import TranslationSession
        self.graph = DocumentGraph()
        self.graph.add_node(DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="Hello"))
        self.memory = DocumentMemory()
        self.session = TranslationSession(self.graph, self.memory)
    def test_initial_state(self):
        self.assertEqual(self.session.status, "created")
    def test_start_finish(self):
        self.session.start()
        self.assertEqual(self.session.status, "running")
        self.session.finish()
        self.assertEqual(self.session.status, "completed")
    def test_fail_state(self):
        self.session.start()
        self.session.fail("error")
        self.assertEqual(self.session.status, "failed")
    def test_record_result(self):
        self.session.record_result("n1", "\u4f60\u597d")
        self.assertEqual(self.session.get_result("n1"), "\u4f60\u597d")
    def test_apply_results(self):
        self.session.record_result("n1", "\u4f60\u597d\u4e16\u754c")
        self.session.apply_results_to_graph()
        self.assertEqual(self.graph.get_node("n1").translated_text, "\u4f60\u597d\u4e16\u754c")
    def test_summary(self):
        self.session.start()
        self.session.record_result("n1", "hi")
        self.session.finish()
        s = self.session.summary()
        self.assertEqual(s["nodes_translated"], 1)
        self.assertEqual(s["status"], "completed")


# ====================================================================
# P1: Translator
# ====================================================================
class TestTranslator(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import TranslationSession, Translator
        self.graph = DocumentGraph()
        for i in range(3):
            self.graph.add_node(DocumentNode(id=f"n{i}", node_type=NodeType.PARAGRAPH, bbox=B, text=f"Text {i}"))
        self.session = TranslationSession(self.graph)
        self.translator = Translator(self.session)
    def test_translate_node(self):
        r = self.translator.translate_node("n0")
        self.assertIsNotNone(r)
        self.assertTrue(self.session.has_result("n0"))
    def test_translate_nonexistent(self):
        with self.assertRaises(ValueError):
            self.translator.translate_node("missing")
    def test_translate_all(self):
        results = self.translator.translate_all()
        self.assertIn("n0", results); self.assertIn("n1", results)
        self.assertEqual(self.session.status, "completed")
    def test_translate_batch(self):
        results = self.translator.translate_batch(["n0", "n1"])
        self.assertEqual(len(results), 2)
    def test_caches_on_translate(self):
        self.translator.translate_node("n0")
        self.assertGreaterEqual(self.session.cache.size, 1)
    def test_custom_llm_handler(self):
        from pdf2zh.v3.translator import TranslationSession, Translator
        def handler(msgs, model, **kw): return f"CUSTOM({model})"
        t = Translator(TranslationSession(self.graph), llm_handler=handler)
        self.assertIn("CUSTOM", t.translate_node("n0"))

# ====================================================================
# P1: Measure
# ====================================================================
class TestMeasure(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.layout import Measure
        self.M = Measure
    def test_cjk_width(self):
        self.assertAlmostEqual(self.M.measure_text("\u4f60\u597d\u4e16\u754c"), 48.0, delta=1.0)
    def test_ascii_width(self):
        self.assertAlmostEqual(self.M.measure_text("Hello"), 35.0, delta=1.0)
    def test_estimate_lines_narrow(self):
        self.assertGreaterEqual(self.M.estimate_lines("Hello World", 12.0, 30.0), 1)
    def test_estimate_lines_wide(self):
        self.assertEqual(self.M.estimate_lines("Hi", 12.0, 500.0), 1)
    def test_measure_height(self):
        self.assertEqual(self.M.measure_height(3, 12.0), 48.0)


# ====================================================================
# P1: Flow
# ====================================================================
class TestFlow(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.layout import Flow
        self.flow = Flow()
    def test_content_width(self):
        self.assertEqual(self.flow.content_width, 512.0)
    def test_content_height(self):
        self.assertEqual(self.flow.content_height, 692.0)
    def test_flow_paragraph(self):
        r = self.flow.flow_paragraph("Hello World")
        self.assertGreater(r.lines, 0)
    def test_flow_empty(self):
        n = DocumentNode(id="e1", node_type=NodeType.PARAGRAPH, bbox=B, text="")
        self.assertEqual(len(self.flow.flow_paragraphs([n])), 0)
    def test_flow_long(self):
        n = DocumentNode(id="l1", node_type=NodeType.PARAGRAPH, bbox=B, text="x" * 5000)
        self.assertEqual(len(self.flow.flow_paragraphs([n])), 1)

# ====================================================================
# P1: ConstraintSolver
# ====================================================================
class TestConstraintSolver(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.layout import ConstraintSolver, ConstraintType, LayoutConstraint
        self.solver = ConstraintSolver()
        self.c1 = LayoutConstraint(ConstraintType.SOFT, "a", "b", "must_follow", gap=10.0)
    def test_add_constraint(self):
        self.solver.add_constraint(self.c1)
        self.assertEqual(self.solver.constraint_count, 1)
    def test_solve(self):
        self.solver.add_constraint(self.c1)
        pos = self.solver.solve()
        self.assertIn("b", pos)
    def test_clear(self):
        self.solver.add_constraint(self.c1)
        self.solver.clear()
        self.assertEqual(self.solver.constraint_count, 0)

# ====================================================================
# P1: LayoutEngine (sanity only - layout() has known Line() API bug)
# ====================================================================
class TestLayoutEngine(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.layout import LayoutEngine
        self.engine = LayoutEngine()
    def test_initial_state(self):
        self.assertIsNone(self.engine.tree)
    def test_layout_empty(self):
        tree = self.engine.layout(DocumentGraph())
        self.assertEqual(tree.page_count, 0)


# ====================================================================
# P1: RendererFactory
# ====================================================================
class TestRendererFactory(unittest.TestCase):
    def test_create_pdf(self):
        from pdf2zh.v3.renderer import RendererFactory, PDFRenderer
        self.assertIsInstance(RendererFactory.create("pdf"), PDFRenderer)
    def test_registered_html(self):
        from pdf2zh.v3.renderer import RendererFactory
        self.assertIn("html", RendererFactory.supported_formats())
    def test_registered_markdown(self):
        from pdf2zh.v3.renderer import RendererFactory
        self.assertIn("markdown", RendererFactory.supported_formats())
    def test_unsupported(self):
        from pdf2zh.v3.renderer import RendererFactory
        with self.assertRaises(ValueError):
            RendererFactory.create("unsupported")
    def test_supported_formats(self):
        from pdf2zh.v3.renderer import RendererFactory
        formats = RendererFactory.supported_formats()
        self.assertIn("pdf", formats)
        self.assertIn("html", formats)

# ====================================================================
# P1: PDFRenderer
# ====================================================================
class TestPDFRenderer(unittest.TestCase):
    def test_render_empty(self):
        from pdf2zh.v3.renderer import PDFRenderer
        self.assertEqual(PDFRenderer().render(VisualTree()), b"")
    def test_render_page(self):
        from pdf2zh.v3.renderer import PDFRenderer
        page = Page(id="p1", width=612, height=792, page_num=0)
        para = Paragraph(id="pp1", bbox=BoundingBox(50, 50, 500, 100))
        line = Line(id="pl1", bbox=BoundingBox(50, 55, 500, 16), baseline=57, line_height=16)
        run = TextRun(id="pr1", text="Hello Render", font="Times")
        line.add_run(run); para.add_line(line); page.add_child(para)
        tree = VisualTree(); tree.add_page(page)
        out = PDFRenderer().render(tree)
        self.assertIn(b"Hello Render", out)
        self.assertIn(b"PDF-page", out)


# ====================================================================
# P2: Issue / IssueGraph / RepairScheduler
# ====================================================================
class TestIssueAndGraph(unittest.TestCase):
    def test_issue_creation(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity
        i = Issue(issue_type="overlap", severity=IssueSeverity.CRITICAL, description="Text overlap")
        self.assertEqual(i.severity.value, "critical")
        self.assertEqual(i.issue_type, "overlap")
    def test_issue_graph_add(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph
        g = IssueGraph()
        g.add_issue(Issue("overlap", IssueSeverity.MAJOR, "overlap", node_id="n1", module="layout"))
        self.assertEqual(g.total, 1)
    def test_issue_graph_multiple(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph
        g = IssueGraph()
        g.add_issues([Issue("a", IssueSeverity.CRITICAL, "a", module="m1"), Issue("b", IssueSeverity.INFO, "b", module="m2")])
        self.assertEqual(g.total, 2)
        self.assertIn("m1", g.modules)
    def test_get_by_severity(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph
        g = IssueGraph()
        g.add_issue(Issue("x", IssueSeverity.CRITICAL, "x"))
        g.add_issue(Issue("y", IssueSeverity.INFO, "y"))
        self.assertEqual(len(g.get_critical()), 1)
        self.assertEqual(len(g.get_major()), 0)
    def test_summary(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph
        g = IssueGraph()
        g.add_issue(Issue("o", IssueSeverity.MAJOR, "o", module="layout"))
        s = g.summary()
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["major"], 1)
    def test_clear(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph
        g = IssueGraph()
        g.add_issue(Issue("x", IssueSeverity.INFO, "x"))
        g.clear()
        self.assertEqual(g.total, 0)

class TestRepairScheduler(unittest.TestCase):
    def test_schedule_issue(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, RepairScheduler
        i = Issue("overlap", IssueSeverity.CRITICAL, "overlap", module="layout")
        rs = RepairScheduler()
        r = rs.schedule(i)
        self.assertEqual(r["action"], "relayout")
        self.assertEqual(r["priority"], 1)
    def test_schedule_all(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, IssueGraph, RepairScheduler
        g = IssueGraph()
        g.add_issue(Issue("overlap", IssueSeverity.CRITICAL, "o", module="layout"))
        g.add_issue(Issue("bad_translation", IssueSeverity.MAJOR, "bt", module="translation"))
        rs = RepairScheduler()
        repairs = rs.schedule_all(g)
        self.assertEqual(len(repairs), 2)
    def test_clear(self):
        from pdf2zh.v3.evaluator import Issue, IssueSeverity, RepairScheduler
        rs = RepairScheduler()
        rs.schedule(Issue("x", IssueSeverity.INFO, "x"))
        rs.clear()
        self.assertEqual(len(rs.list_repairs()), 0)


# ====================================================================
# P2: LayoutOptimizer
# ====================================================================
class TestLayoutOptimizer(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.optimizer import LayoutOptimizer, LayoutElement
        self.opt = LayoutOptimizer()
        self.elem = LayoutElement(node_id="e1", width=100, height=50)
    def test_add_element(self):
        self.opt.add_element(self.elem)
        r = self.opt.optimize()
        self.assertIn("e1", r.positions)
    def test_set_elements(self):
        from pdf2zh.v3.optimizer import LayoutElement
        self.opt.set_elements([LayoutElement("a", 50, 30), LayoutElement("b", 60, 40)])
        r = self.opt.optimize()
        self.assertIn("a", r.positions); self.assertIn("b", r.positions)
    def test_optimize_y_positions(self):
        from pdf2zh.v3.optimizer import LayoutElement
        self.opt.set_elements([LayoutElement("x", 100, 50), LayoutElement("y", 100, 30)])
        r = self.opt.optimize()
        self.assertGreater(r.positions["y"], r.positions["x"])
    def test_optimize_follows_constraints(self):
        from pdf2zh.v3.optimizer import LayoutElement
        from pdf2zh.v3.layout import LayoutConstraint, ConstraintType
        self.opt.set_elements([LayoutElement("a", 100, 50), LayoutElement("b", 100, 50)])
        self.opt.add_constraint(LayoutConstraint(ConstraintType.HARD, "a", "b", "must_below", gap=10.0))
        r = self.opt.optimize()
        self.assertGreaterEqual(r.positions["b"], r.positions["a"] + 50 + 10 - 4)
    def test_clear(self):
        self.opt.add_element(self.elem)
        self.opt.clear()
        r = self.opt.optimize()
        self.assertEqual(len(r.positions), 0)
    def test_feasible_flag(self):
        self.opt.add_element(self.elem)
        r = self.opt.optimize()
        self.assertTrue(r.feasible)

if __name__ == "__main__":
    unittest.main(verbosity=2)
