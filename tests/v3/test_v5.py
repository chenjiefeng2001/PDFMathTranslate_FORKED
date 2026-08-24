"""V5 Architecture Unit Tests — RuntimeContext, WorkflowEngine, ExecutionGraph,
CausalDiagnosticGraph, RuntimeSupervisor, Tracer, and Kernel integration.

Run with::
    python -m pytest tests/v3/test_v5.py -v
"""

from __future__ import annotations
import time, uuid
from typing import Dict, List, Optional

import pytest

# ── V5 Module Imports ──────────────────────────────────────────
from pdf2zh.v3.runtime_context import RuntimeConfig, LRUCache, RuntimeContext
from pdf2zh.v3.runtime_kernel import (
    RuntimeKernel,
    KnowledgeCenter,
    MemoryCenter,
    TelemetryCollector,
)
from pdf2zh.v3.workflow_engine import WorkflowNodeType, WorkflowNode, WorkflowEngine
from pdf2zh.v3.execution_graph import ExecutionNodeState, ExecutionNode, ExecutionGraph
from pdf2zh.v3.causal_graph import (
    Severity,
    RepairStatus,
    CausalNode,
    CausalDiagnosticGraph,
)
from pdf2zh.v3.runtime_supervisor import ResourceUsage, ResourceReport, ResourceManager
from pdf2zh.v3.runtime_supervisor import RecoveryManager, RuntimeSupervisor
from pdf2zh.v3.tracing import TraceSpan, Tracer
from pdf2zh.v3.scheduler import TaskStatus

# ═══════════════════════════════════════════════════════════════
# 1. RuntimeContext Tests
# ═══════════════════════════════════════════════════════════════


class TestLRUCache:
    def test_put_get(self):
        cache = LRUCache(max_size=5)
        cache.put("a", 1)
        cache.put("b", 2)
        assert cache.get("a") == 1
        assert cache.get("b") == 2
        assert cache.get("c") is None

    def test_eviction(self):
        cache = LRUCache(max_size=3)
        for i in range(5):
            cache.put(f"k{i}", i)
        assert cache.size == 3
        assert cache.get("k0") is None  # evicted
        assert cache.get("k4") == 4

    def test_lru_promotion(self):
        cache = LRUCache(max_size=3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        cache.get("a")  # promote a
        cache.put("d", 4)  # evicts b (least recently used)
        assert cache.get("b") is None
        assert cache.get("a") == 1

    def test_remove(self):
        cache = LRUCache(max_size=5)
        cache.put("x", 100)
        cache.remove("x")
        assert cache.get("x") is None

    def test_clear(self):
        cache = LRUCache(max_size=5)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.clear()
        assert cache.size == 0
        assert cache.hit_rate == 0.0

    def test_hit_rate(self):
        cache = LRUCache(max_size=5)
        cache.put("a", 1)
        cache.get("a")  # hit
        cache.get("b")  # miss
        assert 0 < cache.hit_rate < 1.0

    def test_stats(self):
        cache = LRUCache(max_size=100)
        cache.put("k", "v")
        cache.get("k")
        stats = cache.stats()
        assert stats["size"] == 1
        assert stats["max"] == 100
        assert stats["hits"] == 1


class TestRuntimeConfig:
    def test_defaults(self):
        config = RuntimeConfig()
        assert config.max_memory_entries == 10000
        assert config.max_cache_entries == 5000
        assert config.telemetry_enabled is True
        assert config.tracing_enabled is True
        assert config.auto_recovery is True

    def test_custom(self):
        config = RuntimeConfig(max_cache_entries=100, auto_recovery=False)
        assert config.max_cache_entries == 100
        assert config.auto_recovery is False


class TestRuntimeContext:
    def test_default_init(self):
        ctx = RuntimeContext()
        assert ctx.context_id is not None
        assert len(ctx.context_id) == 12
        assert ctx.knowledge is not None
        assert ctx.memory is not None
        assert ctx.telemetry is not None
        assert ctx.plugins is not None
        assert ctx.cache is not None

    def test_custom_init(self):
        knowledge = KnowledgeCenter()
        memory = MemoryCenter()
        telemetry = TelemetryCollector()
        cache = LRUCache(max_size=10)
        ctx = RuntimeContext(
            config=RuntimeConfig(tracing_enabled=False),
            knowledge=knowledge,
            memory=memory,
            telemetry=telemetry,
            cache=cache,
        )
        assert ctx.knowledge is knowledge
        assert ctx.memory is memory
        assert ctx.telemetry is telemetry
        assert ctx.cache is cache
        assert ctx.config.tracing_enabled is False

    def test_set_label(self):
        ctx = RuntimeContext()
        ctx.set_label("env", "test")
        assert ctx.get_label("env") == "test"
        assert ctx.get_label("missing") is None

    def test_stats(self):
        ctx = RuntimeContext()
        ctx.knowledge.learn("term", "definition")
        ctx.cache.put("key", "value")
        stats = ctx.stats()
        assert stats["context_id"] == ctx.context_id
        assert stats["knowledge"]["entries"] >= 1
        assert stats["cache"]["size"] == 1
        assert "plugins" in stats
        assert "memory" in stats

    def test_clear_state(self):
        ctx = RuntimeContext()
        ctx.knowledge.learn("x", "y")
        ctx.cache.put("a", "b")
        ctx.set_label("env", "test")
        ctx.clear_state()
        assert ctx.knowledge.entry_count() == 0
        assert ctx.cache.size == 0
        assert ctx.get_label("env") is None


# ═══════════════════════════════════════════════════════════════
# 2. WorkflowEngine Tests
# ═══════════════════════════════════════════════════════════════


class TestWorkflowEngine:
    def test_add_task(self):
        wf = WorkflowEngine()
        n = wf.add_task("parse", "Parse PDF")
        assert n.id == "parse"
        assert n.node_type == WorkflowNodeType.TASK
        assert wf.node_count == 1

    def test_add_condition(self):
        wf = WorkflowEngine()
        n = wf.add_condition(
            "check_lang",
            lambda ctx: ctx.get("lang") == "en",
            if_true="translate_en",
            if_false="translate_other",
        )
        assert n.node_type == WorkflowNodeType.CONDITION
        assert n.if_true == "translate_en"
        assert wf.node_count == 1

    def test_add_parallel(self):
        wf = WorkflowEngine()
        n = wf.add_parallel("para", branches=["branch_a", "branch_b"])
        assert n.node_type == WorkflowNodeType.PARALLEL
        assert n.parallel_branches == ["branch_a", "branch_b"]
        assert wf.node_count == 1

    def test_add_merge(self):
        wf = WorkflowEngine()
        n = wf.add_merge("merge_point", dependencies=["a", "b"])
        assert n.node_type == WorkflowNodeType.MERGE
        assert "a" in n.dependencies
        assert wf.node_count == 1

    def test_add_loop(self):
        wf = WorkflowEngine()
        n = wf.add_loop(
            "retry_loop",
            body=["translate"],
            condition=lambda ctx: False,
            max_iterations=3,
        )
        assert n.node_type == WorkflowNodeType.LOOP
        assert n.loop_body == ["translate"]
        assert n.max_iterations == 3
        assert wf.node_count == 1

    def test_duplicate_id_raises(self):
        wf = WorkflowEngine()
        wf.add_task("t1", "Task 1")
        with pytest.raises(ValueError, match="already exists"):
            wf.add_task("t1", "Duplicate")

    def test_dependencies(self):
        wf = WorkflowEngine()
        wf.add_task("a", "A")
        wf.add_task("b", "B", dependencies=["a"])
        wf.add_task("c", "C", dependencies=["b"])
        order = [n.id for n in wf.topological_sort()]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_get_ready_nodes(self):
        wf = WorkflowEngine()
        wf.add_task("a", "A")
        wf.add_task("b", "B", dependencies=["a"])
        ready = wf.get_ready_nodes()
        assert any(n.id == "a" for n in ready)
        assert not any(n.id == "b" for n in ready)

    def test_get_execution_plan(self):
        wf = WorkflowEngine()
        wf.add_task("parse", "Parse")
        wf.add_condition("check", lambda c: True, if_true="trans", if_false="skip")
        wf.add_parallel("para", branches=["a", "b"])
        wf.add_loop("loop", body=["x"], condition=lambda c: False)
        plan = wf.get_execution_plan()
        assert len(plan) == 4
        types = {p["type"] for p in plan}
        assert types == {"task", "condition", "parallel", "loop"}

    def test_to_task_graph(self):
        wf = WorkflowEngine()
        wf.add_task("a", "A")
        wf.add_task("b", "B", dependencies=["a"])
        tg = wf.to_task_graph()
        assert tg.task_count == 2

    def test_remove_node(self):
        wf = WorkflowEngine()
        wf.add_task("a", "A")
        wf.remove_node("a")
        assert wf.node_count == 0

    def test_get_node_by_name(self):
        wf = WorkflowEngine()
        wf.add_task("t1", "My Task")
        node = wf.get_node_by_name("My Task")
        assert node is not None
        assert node.id == "t1"

    def test_all_node_types_in_stats(self):
        wf = WorkflowEngine()
        wf.add_task("t", "T")
        wf.add_condition("c", lambda x: True)
        wf.add_parallel("p", branches=["a"])
        wf.add_merge("m")
        wf.add_loop("l", body=["x"], condition=lambda x: False)
        s = wf.stats()
        assert s["node_count"] == 5
        assert s["by_type"]["task"] == 1
        assert s["by_type"]["condition"] == 1
        assert s["by_type"]["parallel"] == 1
        assert s["by_type"]["merge"] == 1
        assert s["by_type"]["loop"] == 1

    def test_ready_nodes_respects_status(self):
        wf = WorkflowEngine()
        n = wf.add_task("t1", "T1")
        n.status = TaskStatus.DONE
        ready = wf.get_ready_nodes()
        assert len(ready) == 0


# ═══════════════════════════════════════════════════════════════
# 3. ExecutionGraph Tests
# ═══════════════════════════════════════════════════════════════


class TestExecutionGraph:
    def test_add_node(self):
        eg = ExecutionGraph()
        n = eg.add_node("p1", "Paragraph 1")
        assert n.node_id == "p1"
        assert n.dirty is True
        assert eg.node_count == 1

    def test_duplicate_raises(self):
        eg = ExecutionGraph()
        eg.add_node("x", "X")
        with pytest.raises(ValueError):
            eg.add_node("x", "X again")

    def test_dependencies(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        assert "a" in eg.get_node("b").depends_on
        assert "b" in eg.get_node("a").dependents

    def test_mark_dirty_cascade(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        eg.mark_clean("a")
        eg.mark_clean("b")
        eg.mark_dirty("a", cascade=True)
        assert eg.get_node("a").dirty is True
        assert eg.get_node("b").dirty is True

    def test_mark_dirty_no_cascade(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        eg.mark_clean("a")
        eg.mark_clean("b")
        eg.mark_dirty("a", cascade=False)
        assert eg.get_node("b").dirty is False

    def test_get_dirty_nodes(self):
        eg = ExecutionGraph()
        eg.add_node("clean", "Clean")
        eg.add_node("dirty", "Dirty")
        eg.mark_clean("clean")
        dirty = eg.get_dirty_nodes()
        assert any(n.node_id == "dirty" for n in dirty)
        assert not any(n.node_id == "clean" for n in dirty)

    def test_set_state(self):
        eg = ExecutionGraph()
        eg.add_node("p1", "P1")
        eg.set_state("p1", ExecutionNodeState.TRANSLATED)
        assert eg.get_node("p1").state == ExecutionNodeState.TRANSLATED

    def test_set_state_missing_raises(self):
        eg = ExecutionGraph()
        with pytest.raises(KeyError):
            eg.set_state("missing", ExecutionNodeState.DONE)

    def test_get_execution_order(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        eg.add_node("c", "C", depends_on=["b"])
        order = [n.node_id for n in eg.get_execution_order()]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_get_ready_nodes(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        eg.set_state("a", ExecutionNodeState.RENDERED)
        eg.mark_clean("a")
        ready = eg.get_ready_nodes()
        assert any(n.node_id == "b" for n in ready)
        assert not any(n.node_id == "a" for n in ready)

    def test_remove_node(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B", depends_on=["a"])
        eg.remove_node("a")
        assert eg.get_node("a") is None
        assert "a" not in eg.get_node("b").depends_on

    def test_reset_all(self):
        eg = ExecutionGraph()
        eg.add_node("p1", "P1")
        eg.set_state("p1", ExecutionNodeState.RENDERED)
        eg.mark_clean("p1")
        eg.reset_all()
        assert eg.get_node("p1").state == ExecutionNodeState.NEW
        assert eg.get_node("p1").dirty is True

    def test_stats(self):
        eg = ExecutionGraph()
        eg.add_node("a", "A")
        eg.add_node("b", "B")
        eg.set_state("a", ExecutionNodeState.TRANSLATED)
        s = eg.stats()
        assert s["total"] == 2
        assert s["dirty"] == 2
        assert s["by_state"]["new"] == 1
        assert s["by_state"]["translated"] == 1


# ═══════════════════════════════════════════════════════════════
# 4. CausalDiagnosticGraph Tests
# ═══════════════════════════════════════════════════════════════


class TestCausalDiagnosticGraph:
    def test_add_diagnostic(self):
        cdg = CausalDiagnosticGraph()
        n = cdg.add_diagnostic("Font missing", severity=Severity.HIGH, module="font")
        assert n.id.startswith("diag_")
        assert n.label == "Font missing"
        assert n.severity == Severity.HIGH
        assert cdg.node_count == 1

    def test_add_causal_chain(self):
        cdg = CausalDiagnosticGraph()
        nodes = cdg.add_causal_chain(
            [
                ("Font missing", Severity.HIGH, "font"),
                ("Layout overflow", Severity.HIGH, "layout"),
                ("Collision", Severity.CRITICAL, "layout"),
            ]
        )
        assert len(nodes) == 3
        assert len(nodes[0].cause_ids) == 0  # root cause
        assert len(nodes[1].cause_ids) == 1
        assert len(nodes[2].cause_ids) == 1

    def test_find_root_causes(self):
        cdg = CausalDiagnosticGraph()
        cdg.add_causal_chain(
            [
                ("Root", Severity.HIGH, "sys"),
                ("Effect", Severity.CRITICAL, "sys"),
            ]
        )
        roots = cdg.find_root_causes()
        assert len(roots) == 1
        assert roots[0].label == "Root"

    def test_leaf_causes(self):
        cdg = CausalDiagnosticGraph()
        cdg.add_causal_chain(
            [
                ("Root", Severity.HIGH, "sys"),
                ("Middle", Severity.HIGH, "sys"),
                ("Leaf", Severity.CRITICAL, "sys"),
            ]
        )
        leaves = cdg.get_leaf_causes()
        assert len(leaves) == 1
        assert leaves[0].label == "Leaf"

    def test_get_affected_nodes(self):
        cdg = CausalDiagnosticGraph()
        nodes = cdg.add_causal_chain(
            [
                ("Root", Severity.HIGH, "sys"),
                ("Middle", Severity.HIGH, "sys"),
                ("Leaf", Severity.CRITICAL, "sys"),
            ]
        )
        affected = cdg.get_affected_nodes(nodes[0].id)
        assert len(affected) == 3

    def test_get_causal_chain(self):
        cdg = CausalDiagnosticGraph()
        nodes = cdg.add_causal_chain(
            [
                ("Root", Severity.HIGH, "sys"),
                ("Middle", Severity.HIGH, "sys"),
                ("Leaf", Severity.CRITICAL, "sys"),
            ]
        )
        chain = cdg.get_causal_chain(nodes[2].id)
        assert len(chain) == 3
        assert chain[0].label == "Leaf"
        assert chain[2].label == "Root"

    def test_suggest_repairs(self):
        cdg = CausalDiagnosticGraph()
        cdg.add_diagnostic(
            "Missing font",
            severity=Severity.HIGH,
            module="font",
            repair_hint="Install font",
        )
        suggestions = cdg.suggest_repairs()
        assert len(suggestions) == 1
        assert "Install font" in suggestions[0][1]

    def test_mark_repaired(self):
        cdg = CausalDiagnosticGraph()
        n = cdg.add_diagnostic("Error", repair_hint="Fix")
        cdg.mark_repaired(n.id, "Fixed OK")
        assert n.repair_status == RepairStatus.APPLIED
        assert n.repair_result == "Fixed OK"

    def test_mark_failed(self):
        cdg = CausalDiagnosticGraph()
        n = cdg.add_diagnostic("Error", repair_hint="Fix")
        cdg.mark_failed(n.id, "Failed")
        assert n.repair_status == RepairStatus.FAILED

    def test_unresolved(self):
        cdg = CausalDiagnosticGraph()
        n1 = cdg.add_diagnostic("Fixed", repair_hint="Fix")
        n2 = cdg.add_diagnostic("Pending", repair_hint="Fix")
        cdg.mark_repaired(n1.id)
        unresolved = cdg.get_unresolved()
        assert len(unresolved) == 1
        assert unresolved[0].id == n2.id

    def test_auto_repair_suggestions(self):
        cdg = CausalDiagnosticGraph()
        cdg.add_diagnostic("Missing font", module="font", repair_hint="Install font")
        suggestions = cdg.auto_repair_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["hint"] == "Install font"
        assert suggestions[0]["module"] == "font"

    def test_remove_node(self):
        cdg = CausalDiagnosticGraph()
        n1 = cdg.add_diagnostic("Root")
        n2 = cdg.add_diagnostic("Child", cause_ids=[n1.id])
        cdg.remove_node(n1.id)
        assert cdg.get_node(n1.id) is None
        assert cdg.get_node(n2.id) is not None
        assert len(cdg.get_node(n2.id).cause_ids) == 0

    def test_stats(self):
        cdg = CausalDiagnosticGraph()
        cdg.add_diagnostic("Err1", severity=Severity.HIGH, module="font")
        cdg.add_diagnostic("Err2", severity=Severity.CRITICAL, module="layout")
        s = cdg.stats()
        assert s["total"] == 2
        assert s["by_severity"]["high"] == 1
        assert s["by_severity"]["critical"] == 1
        assert s["by_module"]["font"] == 1


# ═══════════════════════════════════════════════════════════════
# 5. RuntimeSupervisor Tests
# ═══════════════════════════════════════════════════════════════


class TestResourceManager:
    def test_track(self):
        rm = ResourceManager()
        rm.track("parse", 0.5)
        rm.track("translate", 1.2)
        report = rm.get_resource_report()
        assert report.total_operations == 2
        assert report.total_time == 1.7

    def test_track_error(self):
        rm = ResourceManager()
        rm.track_error()
        report = rm.get_resource_report()
        assert report.error_count == 1

    def test_avg_time(self):
        rm = ResourceManager()
        rm.track("op", 1.0)
        rm.track("op", 3.0)
        assert rm.avg_time_for("op") == 2.0

    def test_clear(self):
        rm = ResourceManager()
        rm.track("op", 1.0)
        rm.clear()
        report = rm.get_resource_report()
        assert report.total_operations == 0

    def test_empty_report(self):
        rm = ResourceManager()
        report = rm.get_resource_report()
        assert report.total_operations == 0


class TestRecoveryManager:
    def test_register_and_recover(self):
        rcm = RecoveryManager()
        rcm.register("font", lambda d: "Installed font: " + d.label)

        class MockDiag:
            id = "diag_1"
            module = "font"
            label = "Missing X"

        result = rcm.attempt_recovery(MockDiag())
        assert result == "Installed font: Missing X"

    def test_no_strategy(self):
        rcm = RecoveryManager()

        class MockDiag:
            id = "diag_1"
            module = "unknown"

        result = rcm.attempt_recovery(MockDiag())
        assert result is None

    def test_wildcard_strategy(self):
        rcm = RecoveryManager()
        rcm.register("*", lambda d: "Generic fix")

        class MockDiag:
            id = "d1"
            module = "anything"

        result = rcm.attempt_recovery(MockDiag())
        assert result == "Generic fix"

    def test_unregister(self):
        rcm = RecoveryManager()
        rcm.register("err", lambda d: "fix")
        rcm.unregister("err")

        class MockDiag:
            id = "d1"
            module = "err"

        assert rcm.attempt_recovery(MockDiag()) is None

    def test_history(self):
        rcm = RecoveryManager()
        rcm.register("err", lambda d: "fix")

        class MockDiag:
            id = "d1"
            module = "err"

        rcm.attempt_recovery(MockDiag())
        assert len(rcm.get_history()) == 1
        rcm.clear_history()
        assert len(rcm.get_history()) == 0


class TestRuntimeSupervisor:
    def test_init(self):
        sup = RuntimeSupervisor()
        assert sup.resource_manager is not None
        assert sup.recovery_manager is not None
        assert sup.diagnostic_graph is not None
        assert sup.uptime >= 0

    def test_check_health(self):
        sup = RuntimeSupervisor()
        assert sup.check_health() is True

    def test_record_operation(self):
        sup = RuntimeSupervisor()
        sup.record_operation("parse", 0.5, success=True)
        sup.record_operation("fail", 0.1, success=False)
        rpt = sup.resource_manager.get_resource_report()
        assert rpt.total_operations == 2
        assert rpt.error_count == 1

    def test_auto_diagnose(self):
        sup = RuntimeSupervisor()
        n = sup.auto_diagnose(
            "Failed to load font", module="font", repair_hint="Reinstall font"
        )
        assert n is not None
        assert n.label == "Failed to load font"

    def test_auto_repair(self):
        sup = RuntimeSupervisor()
        sup.recovery_manager.register("font", lambda d: "Fixed")
        sup.auto_diagnose("Font error", module="font", repair_hint="Reinstall")
        count = sup.auto_repair()
        assert count == 1

    def test_stats(self):
        sup = RuntimeSupervisor()
        sup.record_operation("parse", 0.5)
        s = sup.stats()
        assert "uptime" in s
        assert "healthy" in s
        assert "resources" in s
        assert "recovery" in s
        assert "diagnostic_graph" in s


# ═══════════════════════════════════════════════════════════════
# 6. Tracer Tests
# ═══════════════════════════════════════════════════════════════


class TestTracer:
    def test_start_span(self):
        tracer = Tracer()
        span = tracer.start_span("translate")
        assert span.operation == "translate"
        assert span.span_id != "disabled"
        assert tracer.active_span_count == 1

    def test_end_span(self):
        tracer = Tracer()
        span = tracer.start_span("op")
        tracer.end_span(span.span_id)
        assert span.is_completed
        assert span.duration >= 0
        assert tracer.active_span_count == 0

    def test_span_context_manager(self):
        tracer = Tracer()
        with tracer.span("outer") as outer:
            with tracer.span("inner") as inner:
                pass
        assert outer.is_completed
        assert inner.is_completed
        assert outer.duration >= inner.duration

    def test_nested_spans(self):
        tracer = Tracer()
        with tracer.span("parent"):
            with tracer.span("child"):
                pass
        tree = tracer.get_trace_tree()
        assert len(tree) == 1
        assert tree[0].operation == "parent"
        assert len(tree[0].children) == 1
        assert tree[0].children[0].operation == "child"

    def test_disabled_tracer(self):
        tracer = Tracer()
        tracer.set_enabled(False)
        with tracer.span("op") as span:
            assert span.span_id == "disabled"
        assert tracer.total_span_count == 0

    def test_export(self):
        tracer = Tracer()
        with tracer.span("root"):
            with tracer.span("child1"):
                pass
            with tracer.span("child2"):
                pass
        exported = tracer.export()
        assert len(exported) == 1
        assert exported[0]["operation"] == "root"
        assert len(exported[0]["children"]) == 2

    def test_clear(self):
        tracer = Tracer()
        tracer.start_span("op")
        tracer.clear()
        assert tracer.total_span_count == 0
        assert tracer.active_span_count == 0

    def test_get_span(self):
        tracer = Tracer()
        span = tracer.start_span("op")
        assert tracer.get_span(span.span_id) is span
        assert tracer.get_span("nonexistent") is None

    def test_attributes(self):
        tracer = Tracer()
        with tracer.span("op", attributes={"lang": "en", "size": 42}):
            pass
        tree = tracer.export()
        assert tree[0]["attributes"]["lang"] == "en"

    def test_telemetry_integration(self):
        telemetry = TelemetryCollector()
        tracer = Tracer(telemetry=telemetry)
        with tracer.span("translate"):
            pass
        summary = telemetry.summary()
        assert summary["total"] >= 1


# ═══════════════════════════════════════════════════════════════
# 7. Kernel Integration Tests (V5 lazy properties)
# ═══════════════════════════════════════════════════════════════


class TestKernelV5Integration:
    def test_runtime_context_property(self):
        kernel = RuntimeKernel()
        ctx = kernel.runtime_context
        assert ctx is not None
        assert ctx.context_id is not None
        assert ctx.knowledge is kernel.knowledge_center
        assert ctx.memory is kernel.memory_center
        assert ctx.telemetry is kernel.telemetry

    def test_workflow_engine_property(self):
        kernel = RuntimeKernel()
        wf = kernel.workflow_engine
        assert wf is not None
        assert wf.node_count == 0
        # Same instance on repeated access
        assert kernel.workflow_engine is wf

    def test_execution_graph_property(self):
        kernel = RuntimeKernel()
        eg = kernel.execution_graph
        assert eg is not None
        assert eg.node_count == 0
        assert kernel.execution_graph is eg

    def test_causal_graph_property(self):
        kernel = RuntimeKernel()
        cdg = kernel.causal_graph
        assert cdg is not None
        assert cdg.node_count == 0
        assert kernel.causal_graph is cdg

    def test_runtime_supervisor_property(self):
        kernel = RuntimeKernel()
        sup = kernel.runtime_supervisor
        assert sup is not None
        assert sup.resource_manager is not None
        assert kernel.runtime_supervisor is sup

    def test_tracer_property(self):
        kernel = RuntimeKernel()
        tracer = kernel.tracer
        assert tracer is not None
        assert tracer.telemetry is not None
        assert kernel.tracer is tracer

    def test_v5_stats_in_kernel(self):
        kernel = RuntimeKernel()
        # Trigger lazy init
        _ = kernel.runtime_context
        _ = kernel.workflow_engine
        _ = kernel.execution_graph
        stats = kernel.stats()
        assert "runtime_context" in stats
        assert "workflow_engine" in stats
        assert "execution_graph" in stats
        assert "causal_graph" not in stats  # not triggered
        assert "runtime_supervisor" not in stats
        assert "tracer" not in stats

    def test_tracer_records_to_telemetry(self):
        kernel = RuntimeKernel()
        tracer = kernel.tracer
        with tracer.span("translate_doc"):
            with tracer.span("llm_call"):
                pass
        exported = tracer.export()
        assert len(exported) == 1
        assert exported[0]["operation"] == "translate_doc"
        assert len(exported[0]["children"]) == 1
        summary = kernel.telemetry.summary()
        # Telemetry should include the span operations
        assert summary["total"] >= 2

    def test_full_v5_workflow_via_kernel(self):
        """Simulate a full V5 workflow through the kernel."""
        kernel = RuntimeKernel()
        tracer = kernel.tracer
        sup = kernel.runtime_supervisor
        wf = kernel.workflow_engine
        eg = kernel.execution_graph
        cdg = kernel.causal_graph
        ctx = kernel.runtime_context

        # 1) Build workflow
        wf.add_task("parse", "Parse PDF")
        wf.add_task("translate", "Translate", dependencies=["parse"])
        wf.add_task("render", "Render", dependencies=["translate"])
        assert wf.node_count == 3

        # 2) Build execution graph
        eg.add_node("parse_node", "Parse", depends_on=[])
        eg.add_node("trans_node", "Translate", depends_on=["parse_node"])
        assert eg.node_count == 2

        # 3) Trace an operation
        with tracer.span("full_pipeline"):
            sup.record_operation("parse", 0.5)
            sup.record_operation("translate", 1.2, success=True)

        # 4) Diagnose and repair
        cdg.add_diagnostic(
            "Font warning",
            severity=Severity.WARNING,
            module="font",
            repair_hint="Check fonts",
        )
        sup.recovery_manager.register("font", lambda d: "Fonts checked")
        repaired = sup.auto_repair()
        assert repaired == 1

        # 5) Verify
        assert kernel.workflow_engine is wf
        assert tracer.total_span_count >= 1
        assert cdg.get_unresolved() == []

    def test_v5_backward_compatibility(self):
        """Ensure V4 kernel APIs still work after V5 integration."""
        kernel = RuntimeKernel()
        kernel.initialize()
        # V4 APIs
        assert kernel.initialized is True
        assert kernel.uptime > 0
        assert kernel.kernel_id is not None
        kernel.emit("test_event", data="value")
        assert kernel.event_bus.event_count >= 1
        diag = kernel.diagnose("warning", "test", "testing")
        assert diag is not None

        # V4 Telemetry
        with kernel.telemetry.record_timed("test_op"):
            pass
        summary = kernel.telemetry.summary()
        assert summary["total"] >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
