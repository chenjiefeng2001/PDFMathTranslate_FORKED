"""V7.2-V7.3 Operator-Based Runtime — Document Intelligence Runtime.

Covers the V7 architecture iteration (see doc/v7_operator_runtime_report.md):

  - V7.0 Property Graph: Schema / Query / Traversal / Index (GraphDatabase
    style, duck-type compatible with BaseGraph.adapt).
  - V7.1 Operator Graph: OperatorContext / OperatorGraph / Registry and
    DependencyScheduler pruning (full run vs incremental sub-graph run).
  - V7.2 State Snapshot: RuntimeSnapshot WAL-style capture, SnapshotDiff,
    IncrementalEngine planning from document / snapshot diffs.
  - V7.3 Runtime Service: RuntimeService session lifecycle, execution,
    incremental re-run, snapshot / rollback, persist / restore, event bus,
    resource quotas, session manager.

Run with:
    python -m pytest tests/v3/test_v7_2_runtime_service.py -v
"""
from __future__ import annotations

import os

import pytest

from pdf2zh.v3.graph_property import (
    PropertyGraph, PropertySchema, create_property_graph_from_document,
)
from pdf2zh.v3.operators import OperatorGraph, OperatorRegistry
from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot, SnapshotDiff
from pdf2zh.v3.runtime_service import (
    ResourceManager, RuntimeService, SessionManager,
)
from pdf2zh.v3.base_graph import adapt


@pytest.fixture()
def blocks() -> list:
    return [
        {"id": "n0", "text": "Transformer models achieve state of the art "
                             "results.", "type": "paragraph", "page": 0},
        {"id": "n1", "text": "E = mc^2 is a famous formula.", "type": "formula",
         "page": 0},
    ]


@pytest.fixture()
def service(tmp_path) -> RuntimeService:
    return RuntimeService(persistence_dir=str(tmp_path))


# ── V7.0 Property Graph ────────────────────────────────────────────────

class TestPropertyGraph:
    def test_index_and_query(self):
        pg = PropertyGraph(
            name="doc",
            schema=PropertySchema(node_types={"Paragraph": {"page"}}))
        pg.add_node("n0", node_type="Paragraph", page=3, language="en")
        pg.add_node("n1", node_type="Paragraph", page=3, language="fr")
        pg.add_node("n2", node_type="Heading", page=1)
        pg.add_edge("n0", "n1", relation="follows")

        assert pg.ids_of_type("Paragraph") == ["n0", "n1"]
        assert pg.lookup("page", 3) == ["n0", "n1"]
        # MATCH Paragraph WHERE page == 3
        assert sorted(pg.query().where_type("Paragraph").where(page=3).ids()) \
            == ["n0", "n1"]
        # traversal
        assert pg.query().out("n0") == {"n1"}
        assert pg.query().in_("n1") == {"n0"}
        assert pg.neighbors("n2") == set()

    def test_upsert_refreshes_index(self):
        pg = PropertyGraph()
        pg.add_node("a", node_type="Paragraph", page=1)
        pg.upsert_node("a", page=2)
        assert pg.lookup("page", 1) == []
        assert pg.lookup("page", 2) == ["a"]

    def test_schema_strict_enforced(self):
        pg = PropertyGraph(schema=PropertySchema(
            node_types={"Paragraph": {"page"}}, strict=True))
        with pytest.raises(ValueError):
            pg.add_node("a", node_type="Paragraph", unknown="x")

    def test_serialization_roundtrip(self):
        pg = PropertyGraph(name="g")
        pg.add_node("a", node_type="Paragraph", page=1)
        pg.add_node("b", node_type="Paragraph", page=2)
        pg.add_edge("a", "b", relation="follows")
        clone = PropertyGraph.from_dict(pg.to_dict())
        assert len(clone) == 2
        assert clone.query().where(page=1).ids() == ["a"]
        assert clone.lookup("page", 2) == ["b"]

    def test_adapt_compatible(self, service):
        session = service.open(
            [{"id": "x", "text": "Hi.", "type": "paragraph", "page": 0}])
        service.execute(session.session_id)
        prop = create_property_graph_from_document(session.document_graph)
        bg = adapt(prop)
        assert bg.node_count == 1

    def test_property_graph_from_document(self, service):
        session = service.open(
            [{"id": "a", "text": "One.", "type": "paragraph", "page": 0},
             {"id": "b", "text": "Two.", "type": "formula", "page": 0}])
        service.execute(session.session_id)
        prop = session.graphs["property"]
        assert sorted(prop.node_types()) == ["formula", "paragraph"]
        assert prop.query().where_type("paragraph").where(page=0).ids() == ["a"]


# ── V7.2 State Snapshot ────────────────────────────────────────────────

class TestStateSnapshot:
    def test_snapshot_capture_and_restore(self, service, blocks):
        session = service.open(blocks)
        service.execute(session.session_id)
        snap = service.snapshot(session.session_id, label="v1")
        assert isinstance(snap, RuntimeSnapshot)
        assert snap.label == "v1"
        assert snap.graphs and snap.translations

        session.translations["n0"] = "mutated"
        service.rollback(session.session_id)
        assert session.translations["n0"] != "mutated"

    def test_snapshot_persistence(self, service, blocks):
        session = service.open(blocks)
        service.execute(session.session_id)
        path = service.persist(session.session_id, label="v1")
        assert os.path.exists(path)
        session.translations["n0"] = "mutated"
        service.restore(session.session_id, path)
        assert session.translations["n0"] != "mutated"

    def test_snapshot_diff(self, service):
        orig = [{"id": "n0", "text": "Hello.", "type": "paragraph", "page": 0}]
        s = service.open(orig)
        service.execute(s.session_id)
        snap1 = service.snapshot(s.session_id, label="v1")
        s.document[0]["text"] = "Hello CHANGED."
        service.execute_incremental(s.session_id, ["n0"])
        snap2 = service.snapshot(s.session_id, label="v2")
        d = SnapshotDiff.between(snap1, snap2)
        assert d.changed is True
        assert d.updated_components
        assert not d.is_empty

    def test_incremental_plan_from_document_diff(self, service):
        orig = [{"id": "n0", "text": "Alpha.", "type": "paragraph", "page": 0},
                {"id": "n1", "text": "Beta.", "type": "paragraph", "page": 0}]
        new = [dict(b) for b in orig]
        new[0]["text"] = "Alpha CHANGED."
        plan = service.incremental.plan(orig, new)
        assert plan.changed == ["n0"]
        assert plan.affected == ["n0"]

    def test_incremental_plan_from_snapshots(self, service):
        orig = [{"id": "n0", "text": "Alpha.", "type": "paragraph", "page": 0}]
        s = service.open(orig)
        service.execute(s.session_id)
        snap1 = service.snapshot(s.session_id, label="v1")
        s.document[0]["text"] = "Alpha CHANGED."
        service.execute_incremental(s.session_id, ["n0"])
        snap2 = service.snapshot(s.session_id, label="v2")
        plan = service.incremental.plan(snap1, snap2)
        assert plan.changed == ["n0"]


# ── V7.3 Runtime Service ───────────────────────────────────────────────

class TestRuntimeService:
    def test_session_lifecycle(self, service, blocks):
        session = service.open(blocks)
        assert session.session_id in service.sessions.list_ids()
        service.execute(session.session_id)
        assert session.state.name == "COMPLETED"
        assert session.translations
        service.close(session.session_id)
        assert session.session_id not in service.sessions.list_ids()

    def test_incremental_run_only_affected(self, service):
        orig = [{"id": "n0", "text": "One.", "type": "paragraph", "page": 0},
                {"id": "n1", "text": "Two.", "type": "paragraph", "page": 0}]
        s = service.open(orig)
        service.execute(s.session_id)
        s.document[0]["text"] = "One CHANGED."
        service.execute_incremental(s.session_id, ["n0"])
        trace = [t["operator"] for t in service.operator_graph.trace]
        assert "parse" in trace          # source re-parse is required
        assert "analyze" not in trace    # planning/analysis is skipped
        assert "render" in trace
        assert service.scheduler.stats()["last"]["incremental"] is True

    def test_event_bus(self, service, blocks):
        service.open(blocks)
        topics = [e["topic"] for e in service.bus.history()]
        assert "session.opened" in topics

    def test_resource_manager_quota(self):
        rm = ResourceManager({"llm": 1})
        assert rm.acquire("llm", timeout=1.0) is True
        assert rm.acquire("llm", timeout=0.05) is False
        rm.release("llm")
        assert rm.acquire("llm", timeout=0.05) is True

    def test_session_manager_limit(self):
        mgr = SessionManager(max_sessions=1)
        mgr.create({}, "d1")
        with pytest.raises(RuntimeError):
            mgr.create({}, "d2")
        assert mgr.stats()["count"] == 1

    def test_status_and_telemetry(self, service, blocks):
        session = service.open(blocks)
        service.execute(session.session_id)
        status = service.status(session.session_id)
        assert status["nodes"] >= 1
        assert status["translated"] >= 1
        assert session.telemetry["scheduler"]


# ── V7.1 Operator Graph ────────────────────────────────────────────────

class TestOperatorGraph:
    def test_execution_order_and_pruning(self, service):
        graph = service.operator_graph
        order = graph.order()
        assert order == ["parse", "analyze", "layout", "plan", "translate",
                         "review", "render"]
        # pruning from 'translate' keeps only the downstream sub-graph
        pruned = graph.prune_from("translate")
        assert pruned == ["translate", "review", "render"]
        # dependents set
        assert graph.dependents("parse") == {
            "analyze", "layout", "plan", "render", "review", "translate"}
        assert graph.dependents("layout") == {"render"}

    def test_run_trace(self, service):
        session = service.open(
            [{"id": "a", "text": "Hi.", "type": "paragraph", "page": 0}])
        out = service.execute(session.session_id)
        assert out.stats.total_nodes >= 1
        assert set(out.rendered) == {"html", "pdf", "text"}
