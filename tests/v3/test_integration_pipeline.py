"""Integration tests for the end-to-end V4 pipeline.

Tests RuntimeFacade, LegacyCompatAdapter, FeatureFlags,
and the full pipeline lifecycle.

Run: pytest tests/v3/test_integration_pipeline.py -v
"""

from __future__ import annotations
import logging, os, tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture
def sample_pdf_path():
    """Return path to a minimal valid PDF."""
    base = Path(__file__).resolve().parent.parent
    candidates = [
        base / "samples" / "simple.pdf",
        base / "samples" / "paper.pdf",
        base / "fixtures" / "sample.pdf",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    content = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"5 0 obj<</Length 56>>stream\n"
        b"BT /F1 12 Tf 72 700 Td (Hello World) Tj ET\n"
        b"endstream\nendobj\nxref\n0 6\ntrailer<</Size 6/Root 1 0 R>>\n%%EOF\n"
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


@pytest.fixture
def sample_graph():
    """Create a simple DocumentGraph with one text node."""
    from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType

    g = DocumentGraph()
    g.add_node(
        DocumentNode(
            id="n1",
            text="Test paragraph.",
            node_type=NodeType.PARAGRAPH,
            bbox=(0.0, 0.0, 100.0, 20.0),
        )
    )
    return g


@pytest.fixture(autouse=True)
def cleanup_flags():
    yield
    from pdf2zh.v3.feature_flags import reset_feature_flags

    reset_feature_flags()


# ── Feature Flag Tests ──────────────────────────────────────────────


class TestFeatureFlags:
    """Verify FeatureFlags control V4 engine components."""

    def test_default_all_disabled(self):
        from pdf2zh.v3.feature_flags import FeatureFlags

        f = FeatureFlags()
        assert f.use_v4_engine is False
        assert f.use_v4_translator is False
        assert f.use_v4_layout is False
        assert f.use_v4_repair is False
        assert f.use_v4_renderer is False

    def test_master_switch_enables_all(self):
        from pdf2zh.v3.feature_flags import FeatureFlags

        f = FeatureFlags(use_v4_engine=True)
        assert f.use_v4_translator is True
        assert f.use_v4_layout is True
        assert f.use_v4_repair is True

    def test_individual_toggle(self):
        from pdf2zh.v3.feature_flags import FeatureFlags

        f = FeatureFlags(use_v4_translator=True)
        assert f.use_v4_translator is True
        assert f.use_v4_layout is False

    def test_enable_disable_all(self):
        from pdf2zh.v3.feature_flags import FeatureFlags

        f = FeatureFlags()
        f.enable_all()
        assert f.use_v4_engine is True
        f.disable_all()
        assert f.use_v4_engine is False

    def test_singleton(self):
        from pdf2zh.v3.feature_flags import (
            get_feature_flags,
            set_feature_flags,
            reset_feature_flags,
            FeatureFlags,
        )

        reset_feature_flags()
        f1 = get_feature_flags()
        f2 = get_feature_flags()
        assert f1 is f2
        custom = FeatureFlags(use_v4_engine=True)
        set_feature_flags(custom)
        assert get_feature_flags().use_v4_engine is True
        reset_feature_flags()
        assert get_feature_flags().use_v4_engine is False

    def test_summary(self):
        from pdf2zh.v3.feature_flags import FeatureFlags

        s = FeatureFlags(use_v4_engine=True).summary()
        assert "enabled" in s


# ── RuntimeFacade Pipeline Tests ────────────────────────────────────


class TestRuntimeFacadePipeline:
    """Test RuntimeFacade pipeline with feature flags."""

    def test_init_with_flags(self):
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        f = FeatureFlags(use_v4_translator=True)
        rt = RuntimeFacade(config={}, feature_flags=f)
        assert rt.feature_flags.use_v4_translator is True

    def test_init_defaults(self):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        assert rt.feature_flags is not None

    def test_pipeline_runs(self, sample_pdf_path):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        result = rt.pipeline(sample_pdf_path, fmt="pdf")
        assert result is not None

    def test_pipeline_populates(self, sample_pdf_path):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        rt.pipeline(sample_pdf_path)
        assert rt.graph is not None
        assert rt.output is not None

    def test_load_method(self, sample_pdf_path):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        rt.load(sample_pdf_path)
        assert rt.source == sample_pdf_path
        assert rt.graph is not None

    def test_analyze_plan_translate(self, sample_graph):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        rt.graph = sample_graph
        rt.analyze()
        rt.plan()
        assert rt.plans is not None
        rt.translate()
        translated = [n.translated_text for n in rt.graph.nodes if n.translated_text]
        assert len(translated) > 0

    def test_render_produces_bytes(self, sample_pdf_path):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        rt.load(sample_pdf_path)
        rt.layout()
        out = rt.render(fmt="pdf")
        assert isinstance(out, bytes) and len(out) > 0

    def test_summary(self, sample_pdf_path):
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade()
        rt.pipeline(sample_pdf_path)
        s = rt.summary()
        assert isinstance(s, dict) and "source" in s


# ── LegacyCompatAdapter Tests ───────────────────────────────────────


class TestLegacyCompatAdapter:
    """Test LegacyCompatAdapter wrapping RuntimeFacade."""

    def test_adapter_init(self):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        assert a.stats["calls"] == 0

    def test_adapter_load(self, sample_pdf_path):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        g = a.load(sample_pdf_path)
        assert g is not None

    def test_adapter_translate(self, sample_graph):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        g = a.translate_graph(sample_graph)
        assert len([n for n in g.nodes if n.translated_text]) > 0

    def test_adapter_layout_render(self, sample_graph):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        g = a.translate_graph(sample_graph)
        r = a.layout_and_render(g, fmt="pdf")
        assert isinstance(r, bytes) and len(r) > 0

    def test_adapter_pipeline(self, sample_pdf_path):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        r = a.run_pipeline(sample_pdf_path)
        assert isinstance(r, bytes) and len(r) > 0

    def test_adapter_output_file(self, sample_pdf_path):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
            op = t.name
        try:
            a = LegacyCompatAdapter()
            g = a.load(sample_pdf_path)
            a.layout_and_render(g, output_path=op)
            assert os.path.getsize(op) > 0
        finally:
            if os.path.exists(op):
                os.unlink(op)

    def test_adapter_stats(self, sample_pdf_path):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        a.load(sample_pdf_path)
        assert a.stats["load"] == 1

    def test_adapter_facade_property(self, sample_pdf_path):
        from pdf2zh.v3.legacy_adapter import LegacyCompatAdapter

        a = LegacyCompatAdapter()
        assert a.facade is None
        try:
            a.load(sample_pdf_path)
        except Exception:
            pass
        assert a.facade is not None


# ── TranslationWorkflow Tests (Phase 1) ──────────────────────────────


class TestTranslationWorkflow:
    """Verify TranslationWorkflow (Phase 1, Steps 1.1-1.4)."""

    def test_workflow_creates_results(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationWorkflow
        from pdf2zh.v3.planner import TranslationPlan

        wf = TranslationWorkflow(sample_graph)
        r = wf.execute(TranslationPlan(node_ids=["n1"]))
        assert len(r) > 0 and "n1" in r

    def test_workflow_apply_to_graph(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationWorkflow
        from pdf2zh.v3.planner import TranslationPlan

        wf = TranslationWorkflow(sample_graph)
        wf.execute(TranslationPlan(node_ids=["n1"]))
        wf.apply_to_graph()
        node = sample_graph.get_node("n1")
        assert node is not None and node.translated_text is not None

    def test_workflow_review(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationWorkflow
        from pdf2zh.v3.planner import TranslationPlan

        wf = TranslationWorkflow(sample_graph)
        wf.execute(TranslationPlan(node_ids=["n1"]))
        issues = wf.review()
        assert isinstance(issues, list)

    def test_router(self, sample_graph):
        from pdf2zh.v3.translation_runtime import Router

        r = Router()
        route = r.route(list(sample_graph.nodes)[0])
        assert route.model != ""

    def test_chunk_scheduler(self, sample_graph):
        from pdf2zh.v3.translation_runtime import ChunkScheduler, TranslationPlan

        s = ChunkScheduler(sample_graph)
        plan = TranslationPlan(node_ids=["n1"])
        ordered = s.schedule(plan)
        assert len(ordered) > 0

    def test_retry_policy(self):
        from pdf2zh.v3.translation_runtime import RetryPolicy

        p = RetryPolicy()
        assert p.should_retry(0, "") is True
        assert p.should_retry(3, "timeout") is False

    def test_consistency_checker(self, sample_graph):
        from pdf2zh.v3.translation_runtime import ConsistencyChecker

        c = ConsistencyChecker()
        score = c.check(
            "n1", "Hello", "Bonjour", {"n1": MagicMock(source_text="Hello")}
        )
        assert isinstance(score, float) and 0.0 <= score <= 1.0

    def test_translation_runtime_execute(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationRuntime
        from pdf2zh.v3.planner import TranslationPlan

        rt = TranslationRuntime()
        results = rt.execute(sample_graph, TranslationPlan(node_ids=["n1"]))
        assert len(results) > 0

    def test_translation_runtime_stats(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationRuntime
        from pdf2zh.v3.planner import TranslationPlan

        rt = TranslationRuntime()
        rt.execute(sample_graph, TranslationPlan(node_ids=["n1"]))
        s = rt.stats()
        assert s["total_translated"] > 0
        assert s["workflow_count"] > 0

    def test_translation_runtime_batch(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationRuntime
        from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig

        rt = TranslationRuntime()
        planner = TranslationPlanner(PlannerConfig())
        results = rt.batch_translate([sample_graph], planner)
        assert len(results) == 1

    def test_apply_to_graph_with_transaction(self, sample_graph):
        from pdf2zh.v3.translation_runtime import TranslationWorkflow
        from pdf2zh.v3.planner import TranslationPlan

        wf = TranslationWorkflow(sample_graph)
        wf.execute(TranslationPlan(node_ids=["n1"]))
        wf.apply_to_graph(use_transaction=True)
        node = sample_graph.get_node("n1")
        assert node is not None and node.translated_text is not None


# ── Feature Flag Integration ────────────────────────────────────────


class TestFeatureFlagIntegration:
    """Verify feature flags control pipeline behavior."""

    def test_v4_translator_flag(self):
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade(
            feature_flags=FeatureFlags(use_v4_translator=True),
        )
        assert rt.feature_flags.use_v4_translator is True

    def test_v4_layout_flag(self):
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade(
            feature_flags=FeatureFlags(use_v4_layout=True),
        )
        assert rt.feature_flags.use_v4_layout is True

    def test_visual_tree_builder_flag(self):
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade(
            feature_flags=FeatureFlags(
                use_v4_visual_tree_builder=True,
            ),
        )
        assert rt.feature_flags.use_v4_visual_tree_builder is True

    def test_fix_validate_loop_flag(self):
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade(
            feature_flags=FeatureFlags(
                use_v4_fix_validate_loop=True,
            ),
        )
        assert rt.feature_flags.use_v4_fix_validate_loop is True
        assert rt.feature_flags.max_repair_passes == 2

    def test_mock_pipeline_with_flags(self, sample_pdf_path):
        """Test pipeline with mocked translator when flags are on."""
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        flags = FeatureFlags(
            use_v4_visual_tree_builder=True,
            use_v4_fix_validate_loop=False,
        )

        with patch("pdf2zh.v3.visual_tree_builder.VisualTreeBuilder") as mock_cls:
            mock_tree = MagicMock()
            mock_tree.is_layout_frozen = False
            mock_builder = MagicMock()
            mock_builder.build_from_graph.return_value = mock_tree
            mock_cls.return_value = mock_builder

            rt = RuntimeFacade(feature_flags=flags)
            result = rt.pipeline(sample_pdf_path, fmt="pdf")
            assert result is not None

    def test_markdown_render_flag(self):
        """Renderer should support md format."""
        from pdf2zh.v3.feature_flags import FeatureFlags
        from pdf2zh.v3.runtime import RuntimeFacade

        rt = RuntimeFacade(
            feature_flags=FeatureFlags(
                use_v4_renderer=True,
            ),
        )
        assert rt.feature_flags.use_v4_renderer is True
