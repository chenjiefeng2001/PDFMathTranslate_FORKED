"""V7 Runtime-First Unit Tests — 统一图基础设施与文档运行时.

Covers the V6.1 roadmap iteration (迭代后的报告，见 doc/v6_1_runtime_first_report.md):

  - 改进一（Graph 真正统一）: BaseGraph 统一骨架 + adapt() 鸭子类型适配，
    使 DocumentGraph / ExecutionGraph / ConstraintGraph 共享同一套
    DFS / BFS / Topological / Cycle / Merge / Clone / Serialize / Diff /
    Snapshot。
  - 改进二（Runtime First）: DocumentRuntime 全生命周期
    open / execute / pause / resume / rollback / diff / snapshot / close，
    Document 从一次性输入演化为一直活着的 Runtime 会话。
  - DocumentSession 状态机约束与非法转移拦截。
  - 多图（document / execution / constraint）统一视图。

Run with:
    python -m pytest tests/v3/test_v7_runtime_first.py -v
"""
from __future__ import annotations

import pytest

from pdf2zh.v3.base_graph import (
    BaseGraph, GraphNode, GraphEdge, GraphKind, GraphDiff,
    GraphSnapshot, GraphVisitor, adapt,
)
from pdf2zh.v3.document_runtime import (
    DocumentRuntime, DocumentSession, RuntimeCheckpoint, SessionState,
)
from pdf2zh.v3.graph import (
    DocumentGraph, DocumentNode, NodeType, Edge, EdgeType,
)
from pdf2zh.v3.constraint_graph import (
    ConstraintGraph, ConstraintRelation, build_constraint_graph_from_document,
)
from pdf2zh.v3.execution_graph import ExecutionGraph, ExecutionNodeState


def _doc_graph() -> DocumentGraph:
    """A tiny two-paragraph DocumentGraph used across adapt tests."""
    g = DocumentGraph()
    page = DocumentNode(id="page_0", node_type=NodeType.PAGE,
                        bbox=(0, 0, 612, 792), page_num=0)
    a = DocumentNode(id="a", node_type=NodeType.PARAGRAPH,
                     bbox=(72, 100, 540, 114), text="First paragraph.",
                     page_num=0, font_size=11)
    b = DocumentNode(id="b", node_type=NodeType.PARAGRAPH,
                     bbox=(72, 130, 540, 144), text="Second paragraph.",
                     page_num=0, font_size=11)
    g.add_node(page)
    g.add_node(a)
    g.add_node(b)
    g.add_edge(Edge("page_0", "a", EdgeType.CONTAINS))
    g.add_edge(Edge("page_0", "b", EdgeType.CONTAINS))
    g.add_edge(Edge("a", "b", EdgeType.FOLLOWS))
    return g


def _blocks() -> list:
    """Two pipeline-ready paragraph blocks."""
    return [
        {"id": "n0", "text": "Deep learning is powerful.",
         "type": "paragraph", "x": 72, "y": 100, "w": 468, "h": 14,
         "page": 0, "font_size": 11},
        {"id": "n1", "text": "Attention is all you need.",
         "type": "paragraph", "x": 72, "y": 120, "w": 468, "h": 14,
         "page": 0, "font_size": 11},
    ]


# ═══════════════════════════════════════════════════════════════════
# BaseGraph — 统一图骨架
# ═══════════════════════════════════════════════════════════════════

class TestBaseGraphBasics:
    def test_add_nodes_and_edges(self):
        g = BaseGraph(GraphKind.DOCUMENT, "t")
        g.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b", "follows"))
        assert g.node_count == 2 and g.edge_count == 1
        assert g.has_node("a") and g.get_node("b").label == ""
        assert g.out_edges("a")[0].target_id == "b"
        assert g.in_edges("b")[0].source_id == "a"

    def test_duplicate_node_raises(self):
        g = BaseGraph()
        g.add_node(GraphNode("a"))
        with pytest.raises(ValueError):
            g.add_node(GraphNode("a"))

    def test_add_edge_missing_node_raises(self):
        g = BaseGraph()
        g.add_node(GraphNode("a"))
        with pytest.raises(KeyError):
            g.add_edge(GraphEdge("a", "missing"))

    def test_remove_node_prunes_edges(self):
        g = BaseGraph()
        g.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b", "follows"))
        assert g.remove_node("a")
        assert g.edge_count == 0 and g.has_node("b")

    def test_set_property_bumps_version(self):
        g = BaseGraph()
        g.add_node(GraphNode("a"))
        g.set_property("a", "k", 1)
        assert g.get_node("a").version == 1


class TestBaseGraphTraversal:
    def _chain(self, ids, relation="follows"):
        g = BaseGraph()
        for nid in ids:
            g.add_node(GraphNode(nid))
        for src, tgt in zip(ids, ids[1:]):
            g.add_edge(GraphEdge(src, tgt, relation))
        return g

    def test_dfs_and_bfs(self):
        g = self._chain(["a", "b", "c"])
        assert g.dfs("a") == ["a", "b", "c"]
        assert g.bfs("a") == ["a", "b", "c"]

    def test_topological_sort(self):
        g = self._chain(["a", "b", "c"])
        assert g.topological_sort() == ["a", "b", "c"]

    def test_topological_sort_cycle_raises(self):
        g = BaseGraph()
        for nid in ("a", "b", "c"):
            g.add_node(GraphNode(nid))
        g.add_edge(GraphEdge("a", "b"))
        g.add_edge(GraphEdge("b", "c"))
        g.add_edge(GraphEdge("c", "a"))
        with pytest.raises(ValueError):
            g.topological_sort()

    def test_has_cycle_and_find_cycle(self):
        g = BaseGraph()
        for nid in ("a", "b", "c"):
            g.add_node(GraphNode(nid))
        g.add_edge(GraphEdge("a", "b"))
        g.add_edge(GraphEdge("b", "c"))
        g.add_edge(GraphEdge("c", "a"))
        assert g.has_cycle()
        cyc = g.find_cycle()
        assert cyc is not None and cyc[0] == cyc[-1]
        h = self._chain(["a", "b", "c"])
        assert not h.has_cycle() and h.find_cycle() is None

    def test_connected_components(self):
        g = BaseGraph()
        for nid in ("a", "b", "c", "d"):
            g.add_node(GraphNode(nid))
        g.add_edge(GraphEdge("a", "b"))
        g.add_edge(GraphEdge("c", "d"))
        comps = g.connected_components()
        assert sorted(len(c) for c in comps) == [2, 2]

    def test_reachable(self):
        g = self._chain(["a", "b", "c"])
        assert g.reachable_from("a") == {"a", "b", "c"}


class TestBaseGraphAlgebra:
    def test_serialize_roundtrip(self):
        g = BaseGraph(GraphKind.DOCUMENT, "t")
        g.add_node(GraphNode("a", properties={"p": 1}))
        g.add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b", "follows"))
        g2 = BaseGraph.from_dict(g.to_dict())
        assert g2.kind == GraphKind.DOCUMENT and g2.name == "t"
        assert g2.node_count == 2 and g2.edge_count == 1
        assert g2.get_node("a").properties == {"p": 1}
        g3 = BaseGraph.from_json(g.to_json())
        assert g3.node_count == 2 and g3.edge_count == 1

    def test_clone_is_independent(self):
        g = BaseGraph()
        g.add_node(GraphNode("a"))
        c = g.clone()
        c.set_property("a", "k", "v")
        assert g.get_node("a").properties == {}

    def test_merge(self):
        g1 = BaseGraph()
        g1.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g2 = BaseGraph()
        g2.add_node(GraphNode("b")).add_node(GraphNode("c"))
        merged = g1.merge(g2)
        assert merged.node_count == 3

    def test_subgraph(self):
        g = BaseGraph()
        for nid in ("a", "b", "c"):
            g.add_node(GraphNode(nid))
        g.add_edge(GraphEdge("a", "b"))
        g.add_edge(GraphEdge("b", "c"))
        sub = g.subgraph(["a", "b"])
        assert sub.node_count == 2 and sub.edge_count == 1

    def test_snapshot_restore(self):
        g = BaseGraph()
        g.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b", "follows"))
        snap = g.snapshot("v1")
        assert isinstance(snap, GraphSnapshot) and snap.name == "v1"
        g.set_property("a", "k", 1)
        g.remove_node("b")
        snap.restore_into(g)
        assert g.node_count == 2 and g.edge_count == 1
        assert g.get_node("a").properties == {}

    def test_diff_detects_changes(self):
        before = BaseGraph()
        before.add_node(GraphNode("a")).add_node(GraphNode("b"))
        before.add_edge(GraphEdge("a", "b", "follows"))
        after = BaseGraph()
        after.add_node(GraphNode("a", properties={"k": 1}))
        after.add_node(GraphNode("c"))
        after.add_edge(GraphEdge("a", "c", "follows"))
        diff = before.diff(after)
        assert [n.id for n in diff.added_nodes] == ["c"]
        assert [n.id for n in diff.removed_nodes] == ["b"]
        assert [nid for nid, _ in diff.updated_nodes] == ["a"]
        assert len(diff.added_edges) == 1 and len(diff.removed_edges) == 1
        assert diff.summary()["changed"]

    def test_snapshot_diff(self):
        g = BaseGraph()
        g.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b", "follows"))
        s1 = g.snapshot("v1")
        g.add_node(GraphNode("c"))
        s2 = g.snapshot("v2")
        assert s1.diff(s2).summary()["added_nodes"] == 1

    def test_visitor(self):
        seen = []

        class V(GraphVisitor):
            def visit_node(self, node, graph):
                seen.append(node.id)

        g = BaseGraph()
        g.add_node(GraphNode("a")).add_node(GraphNode("b"))
        g.add_edge(GraphEdge("a", "b"))
        g.accept(V())
        assert sorted(seen) == ["a", "b"]


# ═══════════════════════════════════════════════════════════════════
# adapt() — 鸭子类型统一适配
# ═══════════════════════════════════════════════════════════════════

class TestAdapt:
    def test_adapt_document_graph(self):
        bg = adapt(_doc_graph())
        assert bg.kind == GraphKind.DOCUMENT
        assert bg.node_count == 3 and bg.edge_count == 3
        # unified traversal over the concrete DocumentGraph
        assert bg.topological_sort() == ["page_0", "a", "b"]

    def test_adapt_constraint_graph(self):
        cg = ConstraintGraph()
        build_constraint_graph_from_document(cg, _doc_graph())
        bg = adapt(cg)
        assert bg.kind == GraphKind.CONSTRAINT
        assert bg.node_count == 2
        # edges read from the internal _edges dict, relation enum unwrapped
        relations = {e.relation for e in bg.edges}
        assert relations == {"must_below"}

    def test_adapt_execution_graph(self):
        eg = ExecutionGraph()
        eg.add_node("a", label="first", depends_on=[])
        eg.add_node("b", label="second", depends_on=["a"])
        eg.set_state("a", ExecutionNodeState.TRANSLATED)
        bg = adapt(eg)
        assert bg.kind == GraphKind.EXECUTION
        assert bg.node_count == 2
        # edges synthesized from depends_on
        assert [(e.source_id, e.target_id, e.relation) for e in bg.edges] \
            == [("a", "b", "depends_on")]
        assert bg.get_node("a").properties["state"] == "translated"
        assert bg.topological_sort() == ["a", "b"]

    def test_adapt_accepts_base_graph_identity(self):
        g = BaseGraph(GraphKind.DOCUMENT)
        g.add_node(GraphNode("a"))
        assert adapt(g, GraphKind.CUSTOM) is not g  # fresh view
        assert adapt(g).node_count == 1


# ═══════════════════════════════════════════════════════════════════
# DocumentSession — 状态机
# ═══════════════════════════════════════════════════════════════════

class TestSessionStateMachine:
    def test_legal_lifecycle(self):
        s = DocumentSession([])
        assert s.state == SessionState.CREATED
        s.transition(SessionState.OPENED, event="open")
        s.transition(SessionState.READY, event="ready")
        s.transition(SessionState.EXECUTING, event="execute")
        s.transition(SessionState.PAUSED, event="pause")
        s.transition(SessionState.EXECUTING, event="resume")
        s.transition(SessionState.COMPLETED, event="done")
        s.transition(SessionState.ROLLED_BACK, event="rollback")
        s.transition(SessionState.EXECUTING, event="rerun")
        s.transition(SessionState.COMPLETED, event="done")
        s.transition(SessionState.CLOSED, event="close")
        assert s.state == SessionState.CLOSED
        assert s.state_trace()[0] == "created"

    def test_illegal_transition_raises(self):
        s = DocumentSession([])
        with pytest.raises(RuntimeError):
            s.transition(SessionState.COMPLETED)  # CREATED -> COMPLETED illegal

    def test_execute_requires_prior_states(self):
        s = DocumentSession([])
        s.transition(SessionState.OPENED)
        with pytest.raises(RuntimeError):
            s.transition(SessionState.EXECUTING)  # OPENED -> EXECUTING illegal


# ═══════════════════════════════════════════════════════════════════
# DocumentRuntime — 文档运行时（Runtime First）
# ═══════════════════════════════════════════════════════════════════

class TestDocumentRuntimeLifecycle:
    def test_full_lifecycle(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks(), document_id="doc1")
        assert session.state == SessionState.READY
        assert rt.status(session.session_id)["state"] == "ready"

        output = rt.execute(session.session_id)
        assert session.state == SessionState.COMPLETED
        assert len(session.translations) == 2
        assert session.metrics["quality_score"] == 1.0
        assert session.metrics["total_nodes"] == 2

        paused = rt.pause(session.session_id)
        assert paused["state"] == "paused"

        rt.resume(session.session_id)
        assert session.state == SessionState.COMPLETED
        assert session.metrics["resume_count"] == 1

        # snapshots / diff
        rt.snapshot(session.session_id, label="v1")
        assert session.checkpoints[-1].label == "v1"
        diff = rt.diff(session.session_id)
        assert diff["before"] and diff["after"]

        # rollback restores the pre-execute state
        rb = rt.rollback(session.session_id, checkpoint_label="execute_start")
        assert rb["state"] == "rolled_back"
        assert len(session.translations) == 0

        close = rt.close(session.session_id)
        assert close["state"] == "closed"
        assert rt.status(session.session_id)["state"] == "closed"

    def test_open_execute_close_via_active_session(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks())
        rt.execute()  # no explicit session_id -> active session
        rt.close()
        assert session.state == SessionState.CLOSED

    def test_unknown_session_raises(self):
        rt = DocumentRuntime()
        with pytest.raises(KeyError):
            rt.status("nope")

    def test_illegal_state_guards(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks())
        rt.close(session.session_id)
        with pytest.raises(RuntimeError):
            rt.execute(session.session_id)  # closed -> executing illegal

    def test_document_graph_input(self):
        rt = DocumentRuntime()
        session = rt.open(_doc_graph())
        rt.execute(session.session_id)
        assert len(session.translations) == 2
        assert session.graph is not None

    def test_pause_before_execute_allowed(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks())
        rt.pause(session.session_id)
        assert session.state == SessionState.PAUSED

    def test_resume_requires_paused(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks())
        with pytest.raises(RuntimeError):
            rt.resume(session.session_id)  # ready -> resume illegal


# ═══════════════════════════════════════════════════════════════════
# DocumentRuntime — 多图统一视图
# ═══════════════════════════════════════════════════════════════════

class TestDocumentRuntimeGraphs:
    def test_three_unified_views(self):
        rt = DocumentRuntime()
        session = rt.open(_blocks())
        rt.execute(session.session_id)
        views = rt.graphs(session.session_id)
        assert set(views.keys()) == {"document", "execution", "constraint"}
        doc_view = views["document"]
        # unified operations work on every view
        assert doc_view.kind == GraphKind.DOCUMENT
        assert doc_view.node_count >= 2
        assert not doc_view.has_cycle()
        json_payload = doc_view.to_json()
        assert "nodes" in json_payload

    def test_register_custom_graph(self):
        from pdf2zh.v3.base_graph import GraphNode

        rt = DocumentRuntime()
        session = rt.open(_blocks())
        g = BaseGraph(GraphKind.KNOWLEDGE, "glossary")
        g.add_node(GraphNode("term1"))
        rt.register_graph(session.session_id, GraphKind.KNOWLEDGE, g)
        views = rt.graphs(session.session_id)
        assert views["knowledge"].node_count == 1
