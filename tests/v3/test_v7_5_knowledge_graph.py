"""V7.5 Cross-Session Knowledge Graph — 增量传播与术语一致性.

Covers the V7.5 iteration (see doc/v7_operator_runtime_report.md §六):

  - KnowledgeGraph: shared entity / glossary / concept / citation records
    with mergeable, incrementally-propagated updates and serialization.
  - KnowledgePropagator: session → graph (propagate) and graph → config
    (prepare_config, terminology pulled into the next session).
  - RuntimeService(knowledge=...) integration: knowledge.propagated bus
    events, knowledge_stats, cross-session glossary reuse.

Run with:
    python -m pytest tests/v3/test_v7_5_knowledge_graph.py -v
"""
from __future__ import annotations

import pytest

from pdf2zh.v3.knowledge_graph import (
    KnowledgeGraph,
    KnowledgePropagator,
    PropagationReport,
)
from pdf2zh.v3.runtime_service import RuntimeService
from pdf2zh.v3.transformation_pipeline import PipelineConfig

BLOCKS = [
    {"id": "n0", "text": "LLMs use Transformer attention layers.",
     "type": "paragraph", "page": 0},
]


def analysis_fixture() -> dict:
    """Shape of the analysis view produced by AnalyzeOperator."""
    return {
        "entity": {"entities": {
            "LLM": {"canonical": "Large Language Model", "type": "concept",
                    "aliases": ["大语言模型"], "occurrences": 3},
            "Transformer": {"type": "model"},
        }},
        "concept": {"concepts": {
            "Neural Networks": {"parent": "ML", "children": ["Transformers"]},
        }},
        "citation": {"citations": {"Vaswani2017": {"page": 3}}},
        "summary": {},
    }


@pytest.fixture()
def graph() -> KnowledgeGraph:
    return KnowledgeGraph("test")


@pytest.fixture()
def service(tmp_path) -> RuntimeService:
    return RuntimeService(persistence_dir=str(tmp_path))


# ── Unit: record propagation ─────────────────────────────────────────

class TestKnowledgeGraph:
    def test_merge_analysis_entity_accumulation(self, graph):
        report = graph.merge_analysis(analysis_fixture(), session_id="s1")
        assert report.entities_added == 2
        assert report.concepts_added == 1
        assert report.citations_added == 1
        assert graph.stats()["entities"] == 2
        # second session upserts (not duplicates)
        report2 = graph.merge_analysis(analysis_fixture(), session_id="s2")
        assert report2.entities_added == 0
        assert report2.entities_updated == 2
        assert graph.stats()["entities"] == 2
        assert graph.session_ids() == ["s1", "s2"]

    def test_entity_occurrence_and_alias_accumulate(self, graph):
        graph.merge_analysis(analysis_fixture(), session_id="s1")
        llm = graph.get_entity("LLM")
        assert llm is not None
        assert llm.occurrence_count == 3
        assert llm.canonical_name == "Large Language Model"
        assert "大语言模型" in llm.aliases
        # normalize key handles whitespace/case
        assert graph.get_entity("  llm ") is llm

    def test_merge_glossary_dict(self, graph):
        report = graph.merge_glossary(
            {"LLM": "大语言模型", "Transformer": "变换器"}, session_id="s1")
        assert report.glossary_added == 2
        assert graph.glossary_map() == {
            "LLM": "大语言模型", "Transformer": "变换器"}

    def test_merge_glossary_pairs_and_entries(self, graph):
        graph.merge_glossary([("A", "甲"), ("B", "乙")], session_id="s1")
        graph.merge_glossary([{"source": "C", "target": "丙"}],
                             session_id="s2")
        assert set(graph.glossary_map()) == {"A", "B", "C"}

    def test_merge_glossary_glossary_manager(self, graph):
        from pdf2zh.v3.planner import GlossaryManager

        gm = GlossaryManager()
        gm.add_term("GPU", "图形处理器")
        graph.merge_glossary(gm, session_id="s1")
        assert graph.glossary_map() == {"GPU": "图形处理器"}

    def test_glossary_confidence_conflict(self, graph):
        graph.merge_glossary({"GPU": "图形处理器"}, "s1", confidence=0.8)
        graph.merge_glossary({"GPU": "图形处理器单元"}, "s2", confidence=0.5)
        # higher confidence mapping wins
        assert graph.get_glossary_term("GPU").target == "图形处理器"
        graph.merge_glossary({"GPU": "图形处理单元"}, "s3", confidence=0.95)
        assert graph.get_glossary_term("GPU").target == "图形处理单元"

    def test_merge_graphs(self, graph):
        other = KnowledgeGraph("other")
        other.merge_glossary({"Q": "问题"}, "sA")
        other.merge_analysis(analysis_fixture(), session_id="sB")
        report = graph.merge(other)
        assert report.glossary_added == 1
        assert graph.glossary_map() == {"Q": "问题"}
        assert graph.stats()["entities"] == 2

    def test_serialization_roundtrip(self, graph, tmp_path):
        graph.merge_analysis(analysis_fixture(), session_id="s1")
        graph.merge_glossary({"LLM": "大语言模型"}, session_id="s1")
        path = str(tmp_path / "kg.json")
        graph.save(path)
        loaded = KnowledgeGraph.load(path)
        assert loaded.stats() == graph.stats()
        assert loaded.glossary_map() == graph.glossary_map()
        assert loaded.get_entity("LLM").canonical_name == \
            graph.get_entity("LLM").canonical_name
        # to_dict / from_dict round-trips too
        rebuilt = KnowledgeGraph.from_dict(graph.to_dict())
        assert rebuilt.session_ids() == ["s1"]
        assert rebuilt.glossary_map() == graph.glossary_map()

    def test_clear_and_bool(self, graph):
        assert not graph
        graph.merge_glossary({"A": "甲"}, "s1")
        assert graph
        graph.clear()
        assert not graph
        assert graph.stats()["propagations"] == 0

    def test_glossary_prompt(self, graph):
        assert graph.glossary_prompt() == ""
        graph.merge_glossary({"LLM": "大语言模型"}, "s1")
        assert "LLM → 大语言模型" in graph.glossary_prompt()
        assert len(graph.glossary_prompt(max_terms=1).splitlines()) == 1

    def test_propagation_report_merge(self):
        a = PropagationReport(session_id="s1", entities_added=1,
                              glossary_added=2, total_entities=5)
        b = PropagationReport(session_id="s1", glossary_added=3,
                              total_glossary=9)
        a.merge(b)
        assert a.entities_added == 1
        assert a.glossary_added == 5
        assert a.total_entities == 5
        assert a.total_glossary == 9
        assert a.to_dict()["session_id"] == "s1"

    def test_propagation_history(self, graph):
        graph.merge_analysis(analysis_fixture(), session_id="s1")
        history = graph.propagation_history
        assert len(history) == 1
        assert history[0]["session_id"] == "s1"


# ── Unit: KnowledgePropagator ─────────────────────────────────────────

class TestKnowledgePropagator:
    def test_prepare_config_clones_and_merges(self, graph):
        graph.merge_glossary({"LLM": "大语言模型"}, session_id="s1")
        prop = KnowledgePropagator(graph)
        cfg = PipelineConfig()
        merged = prop.prepare_config(cfg)
        assert merged.glossary == {"LLM": "大语言模型"}
        # original config untouched (clone semantics)
        assert cfg.glossary == {}
        # config glossary wins over shared terms on conflict
        cfg2 = PipelineConfig(glossary={"LLM": "本地优先"})
        merged2 = prop.prepare_config(cfg2)
        assert merged2.glossary["LLM"] == "本地优先"

    def test_prepare_config_no_graph_returns_same(self):
        prop = KnowledgePropagator(KnowledgeGraph("empty"))
        cfg = PipelineConfig()
        assert prop.prepare_config(cfg) is cfg

    def test_capture_glossary_from_ctx(self, graph):
        from pdf2zh.v3.planner import GlossaryManager

        class FakePlanner:
            glossary = GlossaryManager()

        FakePlanner.glossary.add_term("BERT", "双向编码表示")

        class FakeCtx:
            config = PipelineConfig(glossary={"TF-IDF": "词频-逆文档频率"})
            extra = {"planner": FakePlanner()}

        prop = KnowledgePropagator(graph)
        captured = prop.capture_glossary(FakeCtx())
        assert captured == {"TF-IDF": "词频-逆文档频率",
                            "BERT": "双向编码表示"}

    def test_capture_analysis_from_ctx(self, graph):
        class FakeCtx:
            extra = {"analysis": analysis_fixture()}

        prop = KnowledgePropagator(graph)
        assert prop.capture_analysis(FakeCtx())["entity"] is not None

    def test_propagate_combines_analysis_and_glossary(self, graph):
        class FakeCtx:
            config = PipelineConfig(glossary={"MHA": "多头注意力"})
            extra = {"analysis": analysis_fixture()}

        prop = KnowledgePropagator(graph)
        report = prop.propagate(FakeCtx(), session_id="s9")
        assert report.entities_added == 2
        assert report.glossary_added == 1
        assert report.total_glossary == 1
        assert graph.glossary_map() == {"MHA": "多头注意力"}


# ── RuntimeService integration ───────────────────────────────────────

class TestRuntimeKnowledgeIntegration:
    def test_execute_propagates_knowledge(self, service):
        kg = KnowledgeGraph("shared")
        service.knowledge = kg
        service.knowledge_propagator = KnowledgePropagator(kg)
        s = service.open(BLOCKS)
        service.execute(s.session_id)
        assert kg.stats()["entities"] > 0
        assert kg.stats()["propagations"] == 1
        topics = [e["topic"] for e in service.bus.history()
                  if e["topic"] == "knowledge.propagated"]
        assert len(topics) == 1
        assert service.knowledge_stats()["enabled"] is True

    def test_knowledge_stats_disabled_by_default(self, service):
        stats = service.knowledge_stats()
        assert stats == {"enabled": False}

    def test_second_session_sees_shared_glossary(self, service):
        kg = KnowledgeGraph("shared")
        kg.merge_glossary({"LLM": "大语言模型"}, session_id="boot")
        service.knowledge = kg
        service.knowledge_propagator = KnowledgePropagator(kg)
        s = service.open(BLOCKS)
        ctx = service._build_context(s, None)
        assert ctx.config.glossary == {"LLM": "大语言模型"}

    def test_constructor_accepts_knowledge(self, tmp_path):
        kg = KnowledgeGraph("built-in")
        service = RuntimeService(persistence_dir=str(tmp_path), knowledge=kg)
        assert service.knowledge is kg
        assert service.knowledge_propagator is not None

