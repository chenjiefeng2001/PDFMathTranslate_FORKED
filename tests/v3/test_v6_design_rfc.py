"""V6.0 Design RFC Unit Tests — 约束布局求解 / 统一渲染适配 / 端到端管线.

Covers the Design RFC deliverables implemented in v6.0:

  - Layer A: ModelSelector (physical rows -> logical chunks)
  - Layer B: RelayoutSolver (constraint graph build + native solve)
  - Layer C: OutputAssembler (bboxes -> assembly manifest)
  - RelayoutEngine facade
  - RenderAdapter (HTML float / text / native PDF)
  - RuleBasedProvider (dependency-free headless translator)
  - TransformationPipeline end-to-end run
  - Translator glossary regression (PromptComposer vs tuple glossary)

Run with:
    python -m pytest tests/v3/test_v6_design_rfc.py -v
"""
from __future__ import annotations

import pytest

from pdf2zh.v3.visual_tree import BoundingBox
from pdf2zh.v3.relayout_engine import (
    RelayoutConfig, RelayoutResult, ModelSelector,
    RelayoutSolver, OutputAssembler, RelayoutEngine,
)
from pdf2zh.v3.render_adapter import (
    RenderBlock, HTMLFloatRenderer, TextRenderer, RenderAdapter,
)
from pdf2zh.v3.transformation_pipeline import (
    PipelineConfig, RuleBasedProvider, TransformationPipeline,
)
from pdf2zh.v3.review_agent import ReviewAgent, QualityPipeline


def _item(nid: str, x: float, y: float, w: float = 200.0, h: float = 14.0):
    return type("Item", (), {"id": nid, "bbox": BoundingBox(x, y, w, h)})()


# ═══════════════════════════════════════════════════════════════
# Layer A — ModelSelector
# ═══════════════════════════════════════════════════════════════

class TestModelSelector:
    def test_merges_close_lines_into_one_chunk(self):
        sel = ModelSelector(line_gap=2.0)
        items = [_item("a", 10, 10), _item("b", 10, 26), _item("c", 10, 42)]
        chunks = sel.select(items)
        # all three lines are close -> one logical chunk (paragraph)
        assert len(chunks) == 1
        assert [i.id for i in chunks[0]] == ["a", "b", "c"]

    def test_split_on_large_gap(self):
        sel = ModelSelector(line_gap=2.0)
        items = [_item("a", 10, 10), _item("b", 10, 26), _item("c", 10, 60)]
        chunks = sel.select(items)
        assert len(chunks) == 2
        assert [i.id for i in chunks[0]] == ["a", "b"]
        assert [i.id for i in chunks[1]] == ["c"]

    def test_split_on_size_change(self):
        sel = ModelSelector(line_gap=2.0)
        items = [_item("a", 10, 10, h=14), _item("h", 10, 26, h=22)]
        chunks = sel.select(items)
        assert len(chunks) == 2

    def test_empty(self):
        assert ModelSelector().select([]) == []

# ═══════════════════════════════════════════════════════════════
# Layer B — RelayoutSolver (constraint graph + native solve)
# ═══════════════════════════════════════════════════════════════

class TestRelayoutSolver:
    def test_build_graph_creates_nodes_and_must_below_edges(self):
        solver = RelayoutSolver()
        items = [[_item("a", 10, 10)], [_item("b", 10, 40)]]
        graph = solver.build_graph(items, page_num=1)
        assert graph.node_count == 2
        edges = graph.edges
        assert len(edges) == 1
        assert edges[0].relation.value == "must_below"
        assert edges[0].priority.value == "soft"

    def test_solve_returns_resolved_bbox_per_chunk(self):
        solver = RelayoutSolver()
        chunks = [[_item("a", 10, 10)], [_item("b", 10, 40)]]
        layout = solver.solve(chunks, page_num=0)
        assert set(layout.keys()) == {"chunk_a", "chunk_b"}
        bb = layout["chunk_a"]
        assert bb.x == 10.0
        assert bb.width == 200.0

    def test_solve_empty(self):
        layout = RelayoutSolver().solve([], page_num=0)
        assert layout == {}


# ═══════════════════════════════════════════════════════════════
# Layer C — OutputAssembler
# ═══════════════════════════════════════════════════════════════

class TestOutputAssembler:
    def test_manifest_sorted_by_reading_order(self):
        layout = {
            "chunk_b": BoundingBox(10, 40, 200, 14),
            "chunk_a": BoundingBox(10, 10, 200, 14),
        }
        blocks = OutputAssembler.assemble(layout, {"chunk_a": ["a1"], "chunk_b": ["b1"]})
        assert [b["id"] for b in blocks] == ["chunk_a", "chunk_b"]
        assert blocks[0]["source_ids"] == ["a1"]
        assert blocks[0]["w"] == 200.0


# ═══════════════════════════════════════════════════════════════
# RelayoutEngine facade
# ═══════════════════════════════════════════════════════════════

class TestRelayoutEngine:
    def test_run_single_page(self):
        engine = RelayoutEngine(RelayoutConfig(chunk_line_gap=2.0))
        items = [_item("a", 10, 10), _item("b", 10, 26), _item("c", 10, 60)]
        result = engine.run([{"index": 0, "items": items}])
        assert result.pages == 1
        assert len(result.layouts) == 1
        assert len(result.blocks) == 2  # (a,b) and c
        assert result.to_dict()["pages"] == 1

    def test_run_multi_page(self):
        engine = RelayoutEngine()
        pages = [
            {"index": 0, "items": [_item("a", 10, 10)]},
            {"index": 1, "items": [_item("b", 10, 10)]},
        ]
        result = engine.run(pages)
        assert result.pages == 2
        assert set(result.layouts.keys()) == {0, 1}


# ═══════════════════════════════════════════════════════════════
# RenderAdapter — HTML float / text / native PDF
# ═══════════════════════════════════════════════════════════════

class TestRenderAdapter:
    def _blocks(self):
        return [
            RenderBlock("p1", "paragraph", "Hello world."),
            RenderBlock("h1", "heading", "Title"),
            RenderBlock("f1", "figure", "", image_path="img.png"),
            RenderBlock("cap1", "caption", "Figure 1"),
        ]

    def test_html_float_layout(self):
        html = HTMLFloatRenderer().render(self._blocks())
        assert "float:right" in html
        assert "<img src='img.png'" in html
        assert "Hello world." in html
        assert html.rstrip().endswith("</html>")

    def test_text_reading_order(self):
        text = TextRenderer().render(self._blocks())
        assert "Hello world." in text
        assert "== Title ==" in text
        assert "[caption] Figure 1" in text

    def test_native_pdf_bytes(self):
        data = RenderAdapter().render(self._blocks(), fmt="pdf")
        assert data[:8] == b"%PDF-1.4"
        assert data.rstrip().endswith(b"%%EOF")

    def test_build_blocks_from_manifest(self):
        manifest = {
            "blocks": [
                {"id": "chunk_a", "x": 0, "y": 0, "w": 10, "h": 10,
                 "source_ids": ["a"]},
            ]
        }
        blocks = RenderAdapter.build_blocks(manifest, {"chunk_a": "你好"})
        assert blocks[0].text == "你好"

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            RenderAdapter().render(self._blocks(), fmt="doc")


# ═══════════════════════════════════════════════════════════════
# RuleBasedProvider
# ═══════════════════════════════════════════════════════════════

class TestRuleBasedProvider:
    def test_preserves_tokens_and_numbers(self):
        prov = RuleBasedProvider()
        resp = prov.complete([
            {"role": "system", "content": "translate"},
            {"role": "user", "content": "Text to translate:\nLoss = CE + L2"},
        ])
        assert resp.provider == "rule-based"
        assert "L2" in resp.text
        assert "CE" in resp.text
        assert "Loss" in resp.text


# ═══════════════════════════════════════════════════════════════
# TransformationPipeline — end-to-end headless run
# ═══════════════════════════════════════════════════════════════

class TestTransformationPipeline:
    def test_run_text_end_to_end(self):
        cfg = PipelineConfig(glossary={"Transformer": "Transformer"})
        pipeline = TransformationPipeline(cfg)
        out = pipeline.run_text([
            "The Transformer uses x=5 and y=3.",
            "Attention is all you need.",
            "Loss = CE + L2",
        ])
        assert out.stats.total_nodes == 3
        assert out.stats.translated == 3
        assert out.stats.review_errors == 0
        assert out.stats.quality_score == 1.0
        # every format produced output
        for fmt in ("html", "text", "pdf"):
            assert fmt in out.rendered
            assert len(out.rendered[fmt]) > 0
        # translations applied
        sample = next(iter(out.translations.values()))
        assert sample.startswith("【译】")

    def test_translations_land_in_rendered_html(self):
        pipeline = TransformationPipeline()
        out = pipeline.run_text(["Deep learning works."])
        html = out.rendered["html"].decode("utf-8")
        assert "Deep learning works." in html

    def test_build_graph_from_blocks_creates_reading_edges(self):
        from pdf2zh.v3.graph import EdgeType
        graph = TransformationPipeline.build_graph_from_blocks([
            {"id": "a", "text": "one", "type": "paragraph", "x": 0, "y": 0,
             "w": 100, "h": 14, "page": 0},
            {"id": "b", "text": "two", "type": "paragraph", "x": 0, "y": 30,
             "w": 100, "h": 14, "page": 0},
        ])
        assert len(graph.nodes) == 3  # a, b, page_0
        follows = [e for e in graph.edges if e.edge_type == EdgeType.FOLLOWS]
        assert len(follows) == 1
        assert follows[0].source_id == "a"
        assert follows[0].target_id == "b"

    def test_glossary_in_prompt_does_not_crash(self):
        """Regression: PromptComposer now tolerates tuple-based glossary."""
        pipeline = TransformationPipeline(PipelineConfig(
            glossary={"Transformer": "Transformer"},
        ))
        out = pipeline.run_text(["The Transformer is a model."])
        assert out.stats.translated == 1
        assert "Transformer" in out.translations[list(out.translations)[0]]


# ═══════════════════════════════════════════════════════════════
# Quality gate sanity (ReviewAgent/QualityPipeline integration)
# ═══════════════════════════════════════════════════════════════

class TestQualityGateIntegration:
    def test_identity_translation_fails_review(self):
        reviewer = ReviewAgent()
        result = reviewer.review("n1", "hello world", "hello world")
        assert not result.passed
        assert any(i.code == "UNTRANSLATED" for i in result.issues)

    def test_quality_pipeline_reports_score(self):
        qp = QualityPipeline()
        report = qp.run({
            "n1": {"source": "hello world", "translated": "你好 世界"},
        })
        assert report["errors"] == 0
        assert report["quality_score"] == 1.0
        assert report["final_translations"]["n1"] == "你好 世界"

