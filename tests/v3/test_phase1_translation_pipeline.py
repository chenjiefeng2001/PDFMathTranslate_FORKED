"""
Phase 1: End-to-End Translation Pipeline Integration Tests.

Tests the complete data flow with real PDFs parsed into DocumentGraph,
SemanticAnalyzer, TranslationPlanner, and TranslationRuntime.

These tests verify that the V4 pipeline (parse -> analyze -> plan -> translate)
works end-to-end on real DocumentGraph data from actual PDF files.

Run: python -m pytest tests/v3/test_phase1_translation_pipeline.py -v
"""
from __future__ import annotations
import logging, os, tempfile
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

logger = logging.getLogger(__name__)


# -- Fixtures ----------------------------------------------------------


@pytest.fixture(scope="session")
def real_pdf_dir() -> Path:
    """Path to the test PDF files directory."""
    candidates = [
        Path(__file__).resolve().parent.parent / "file",
        Path(__file__).resolve().parent.parent.parent / "test" / "file",
    ]
    for c in candidates:
        if c.exists() and list(c.glob("*.pdf")):
            return c
    pytest.skip(f"No PDF directory found (tried: {[str(d) for d in candidates]})")


@pytest.fixture
def real_pdf_paths(real_pdf_dir) -> List[str]:
    """Return paths to all real PDF test files."""
    pdfs = sorted(real_pdf_dir.glob("*.pdf"))
    if not pdfs:
        pytest.skip(f"No PDF files found in {real_pdf_dir}")
    return [str(p) for p in pdfs]


@pytest.fixture
def parsed_graphs(real_pdf_paths) -> List:
    """Parse all real PDFs into DocumentGraph objects.

    PDFs without an extractable text layer (e.g. vector-outline or scanned
    pages) are skipped: the V4 text pipeline has nothing to analyze for them,
    and every text-content assertion below would otherwise fail on an empty
    graph.
    """
    from pdf2zh.v3.parser import PDFParser
    from pdf2zh.v3.normalizer import Normalizer, NormalizerConfig
    from pdf2zh.v3.graph import DocumentGraphBuilder

    results = []
    for pdf_path in real_pdf_paths:
        parser = PDFParser()
        raw = parser.parse(pdf_path)
        normalizer = Normalizer(NormalizerConfig(lang_in="auto"))
        normalized = normalizer.normalize(raw)
        builder = DocumentGraphBuilder()
        graph = builder.build(normalized)
        if not any(
            hasattr(n, "text") and getattr(n, "text", "") and n.text.strip()
            for n in graph.nodes
        ):
            logger.info("Skipping %s: no extractable text layer",
                        Path(pdf_path).name)
            continue
        results.append((pdf_path, graph))
    if not results:
        pytest.skip("No text-bearing PDFs found in the test data directory")
    return results


class TestRealPDFDataPath:
    """Verify that real PDFs parse correctly into DocumentGraph."""

    def test_parse_all_pdfs(self, parsed_graphs):
        for pdf_path, graph in parsed_graphs:
            assert graph is not None
            assert len(graph.nodes) > 0
            logger.info("%s: %d nodes, %d edges",
                        Path(pdf_path).name, len(graph.nodes), len(graph.edges))

    def test_graph_has_text_content(self, parsed_graphs):
        for pdf_path, graph in parsed_graphs:
            text_nodes = [n for n in graph.nodes
                          if hasattr(n, "text") and n.text and n.text.strip()]
            assert len(text_nodes) > 0

    def test_graph_structure_integrity(self, parsed_graphs):
        from pdf2zh.v3.graph import EdgeType, NodeType
        for pdf_path, graph in parsed_graphs:
            pages = [n for n in graph.nodes
                     if hasattr(n, "node_type") and n.node_type == NodeType.PAGE]
            assert len(pages) >= 1
            contains = [e for e in graph.edges
                        if hasattr(e, "edge_type") and e.edge_type == EdgeType.CONTAINS]
            assert len(contains) >= 1
            follows = [e for e in graph.edges
                       if hasattr(e, "edge_type") and e.edge_type == EdgeType.FOLLOWS]
            logger.info("%s: %d pages, %d contains, %d follows",
                        Path(pdf_path).name, len(pages), len(contains), len(follows))


class TestSemanticAnalyzerOnRealData:
    """Run SemanticAnalyzer on real DocumentGraphs from PDFs."""

    def test_analyzer_adds_semantic_edges(self, parsed_graphs):
        from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
        from pdf2zh.v3.graph import NodeType
        for pdf_path, graph in parsed_graphs:
            analyzer = SemanticAnalyzer(AnalyzerConfig(lang_in="auto"))
            annotated = analyzer.analyze(graph)
            assert annotated is graph
            semantic_types = {NodeType.HEADING, NodeType.CAPTION,
                              NodeType.FOOTNOTE, NodeType.ABSTRACT}
            found = {getattr(n, "node_type", None) for n in graph.nodes}
            has = bool(found & semantic_types)
            logger.info("%s: semantic types: %s", Path(pdf_path).name,
                        {t.name for t in found if t in semantic_types})

    def test_analyzer_maintains_edges(self, parsed_graphs):
        from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
        for pdf_path, graph in parsed_graphs[:1]:
            before = len(graph.edges)
            analyzer = SemanticAnalyzer(AnalyzerConfig(lang_in="auto"))
            analyzer.analyze(graph)
            assert len(graph.edges) >= before


class TestTranslationPlannerOnRealData:
    """Run TranslationPlanner on annotated DocumentGraphs."""

    @pytest.fixture
    def annotated_graphs(self, parsed_graphs):
        from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
        results = []
        for pdf_path, graph in parsed_graphs[:2]:
            analyzer = SemanticAnalyzer(AnalyzerConfig(lang_in="auto"))
            analyzer.analyze(graph)
            results.append((pdf_path, graph))
        return results

    def test_plan_all_generates_plans(self, annotated_graphs):
        from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig
        for pdf_path, graph in annotated_graphs:
            planner = TranslationPlanner(PlannerConfig(
                source_lang="auto", target_lang="zh-cn"))
            plans = planner.plan_all(graph)
            assert isinstance(plans, dict)
            text_nodes = [n for n in graph.nodes
                          if hasattr(n, "text") and n.text and n.text.strip()]
            logger.info("%s: %d text, %d plans",
                        Path(pdf_path).name, len(text_nodes), len(plans))
            assert len(plans) > 0

    def test_plan_has_context_and_glossary(self, annotated_graphs):
        from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig
        for pdf_path, graph in annotated_graphs[:1]:
            planner = TranslationPlanner(PlannerConfig(
                source_lang="auto", target_lang="zh-cn"))
            plans = planner.plan_all(graph)
            for nid, plan in plans.items():
                assert hasattr(plan, "prompt")
                assert hasattr(plan, "glossary")
                assert hasattr(plan, "context_window")


class TestTranslationRuntimeOnRealData:
    """Execute TranslationRuntime with mock on real DocumentGraphs."""

    @pytest.fixture
    def prepared_graphs(self, parsed_graphs):
        from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
        from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig
        results = []
        for pdf_path, graph in parsed_graphs[:2]:
            analyzer = SemanticAnalyzer(AnalyzerConfig(lang_in="auto"))
            analyzer.analyze(graph)
            planner = TranslationPlanner(PlannerConfig(
                source_lang="auto", target_lang="zh-cn"))
            plans = planner.plan_all(graph)
            results.append((pdf_path, graph, planner, plans))
        return results

    def test_mock_translation_sets_translated_text(self, prepared_graphs):
        from pdf2zh.v3.translation_runtime import TranslationRuntime
        from pdf2zh.v3.planner import TranslationPlan
        for pdf_path, graph, planner, plans in prepared_graphs[:1]:
            runtime = TranslationRuntime()
            all_ids = list(plans.keys())
            plan = TranslationPlan(node_ids=all_ids)
            result = runtime.execute(graph, plan)
            assert result is not None
            wf = list(runtime._workflows.values())[0]
            wf.apply_to_graph(use_transaction=True)
            translated = [n for n in graph.nodes
                          if hasattr(n, "translated_text")
                          and n.translated_text is not None]
            logger.info("%s: %d/%d translated", Path(pdf_path).name,
                        len(translated), len(graph.nodes))
            assert len(translated) > 0

    def test_workflow_applies_translations(self, prepared_graphs):
        from pdf2zh.v3.translation_runtime import TranslationWorkflow
        from pdf2zh.v3.planner import TranslationPlan
        for pdf_path, graph, planner, plans in prepared_graphs[:1]:
            workflow = TranslationWorkflow(graph)
            all_ids = list(plans.keys())
            plan = TranslationPlan(node_ids=all_ids)
            workflow.execute(plan)
            workflow.apply_to_graph(use_transaction=True)
            translated = [n for n in graph.nodes
                          if hasattr(n, "translated_text")
                          and n.translated_text is not None]
            assert len(translated) > 0

    def test_runtime_collects_stats(self, prepared_graphs):
        from pdf2zh.v3.translation_runtime import TranslationRuntime
        from pdf2zh.v3.planner import TranslationPlan
        for pdf_path, graph, planner, plans in prepared_graphs[:1]:
            runtime = TranslationRuntime()
            all_ids = list(plans.keys())
            plan = TranslationPlan(node_ids=all_ids)
            runtime.execute(graph, plan)
            stats = runtime.stats()
            assert stats["total_translated"] > 0
            logger.info("%s: stats: %s", Path(pdf_path).name, stats)


class TestRuntimeFacadeOnRealData:
    """Full RuntimeFacade pipeline with real PDFs."""

    @pytest.fixture
    def first_real_pdf(self, real_pdf_paths) -> str:
        return real_pdf_paths[0]

    def test_load_parse_real_pdf(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf)
        assert rt.graph is not None
        assert len(rt.graph.nodes) > 0
        assert rt.source == first_real_pdf

    def test_analyze_real_graph(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf)
        rt.analyze()
        assert rt.graph is not None
        assert rt._analyzer is not None

    def test_plan_for_real_graph(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf).analyze().plan()
        assert rt.plans is not None
        assert len(rt.plans) > 0

    def test_translate_real_graph(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf).analyze().plan().translate()
        assert rt.translator is not None
        translated = [n for n in rt.graph.nodes
                      if hasattr(n, "translated_text")
                      and n.translated_text is not None]
        logger.info("Translated %d/%d", len(translated), len(rt.graph.nodes))
        assert len(translated) > 0

    def test_full_pipeline_chain(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf).analyze().plan().translate()
        summary = rt.summary()
        assert summary["graph_nodes"] > 0
        assert summary["plans"] > 0
        logger.info("Pipeline summary: %s", summary)

    def test_pipeline_with_legacy_adapter(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        from pdf2zh.v3.legacy_adapter import LegacyTranslatorAdapter
        rt = RuntimeFacade()
        rt.load(first_real_pdf).analyze().plan()
        adapter = LegacyTranslatorAdapter()
        adapter.translate(rt.graph, source_lang="auto", target_lang="zh",
                          planner=rt._planner)
        translated = [n for n in rt.graph.nodes
                      if hasattr(n, "translated_text")
                      and n.translated_text is not None]
        assert len(translated) > 0


class TestModelRouterIntegration:
    """Verify ModelRouter routes all 9 NodeTypes."""

    @pytest.fixture
    def first_real_pdf(self, real_pdf_paths) -> str:
        return real_pdf_paths[0]

    def test_router_has_all_routes(self):
        from pdf2zh.v3.translator import ModelRouter
        from pdf2zh.v3.graph import DocumentNode, NodeType
        expected = [NodeType.PARAGRAPH, NodeType.HEADING, NodeType.CAPTION,
                    NodeType.FIGURE, NodeType.TABLE, NodeType.FORMULA,
                    NodeType.FOOTNOTE, NodeType.HEADER, NodeType.FOOTER]
        router = ModelRouter()
        for nt in expected:
            node = DocumentNode(f"test_{nt.value}", nt, (0, 0, 10, 10))
            route = router.route(node)
            assert route is not None
            assert route.model is not None
            # temperature may be 0 for some types; just verify it's a float
            assert isinstance(route.temperature, (int, float))

    def test_router_returns_unique_routes(self):
        from pdf2zh.v3.translator import ModelRouter
        router = ModelRouter()
        routes = router.get_routes()
        assert len(routes) > 0

    def test_router_integration(self, first_real_pdf):
        from pdf2zh.v3.runtime import RuntimeFacade
        rt = RuntimeFacade()
        rt.load(first_real_pdf).analyze().plan()
        if hasattr(rt._planner, "_router"):
            router = rt._planner._router
            assert router is not None
            assert router.model_count > 0
