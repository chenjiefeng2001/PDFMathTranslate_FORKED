"""Headless tests for V4 Runtime Kernel (Module: runtime_kernel.py)."""

from __future__ import annotations
import os, sys, time, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.runtime_kernel import *

# ═══ EventBus Tests ═══


class TestEventBus:
    def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []
        bus.subscribe(EventType.NODE_ADDED, lambda e: received.append(e.data))
        bus.emit(EventType.NODE_ADDED, node_id="n1", value=42)
        assert len(received) == 1 and received[0] == {"node_id": "n1", "value": 42}

    def test_two_subscribers(self):
        bus = EventBus()
        r = []
        bus.subscribe(EventType.NODE_UPDATED, lambda e: r.append("a"))
        bus.subscribe(EventType.NODE_UPDATED, lambda e: r.append("b"))
        bus.emit(EventType.NODE_UPDATED)
        assert r == ["a", "b"]

    def test_wildcard(self):
        bus = EventBus()
        c = []
        bus.subscribe_all(lambda e: c.append(e.type.value))
        bus.emit(EventType.ISSUE_DETECTED)
        bus.emit(EventType.TRANSLATION_COMPLETED)
        assert c == ["issue_detected", "translation_completed"]

    def test_unsubscribe(self):
        bus = EventBus()

        def h(e):
            pass

        bus.subscribe(EventType.ERROR_OCCURRED, h)
        assert bus.subscriber_count(EventType.ERROR_OCCURRED) == 1
        bus.unsubscribe(EventType.ERROR_OCCURRED, h)
        assert bus.subscriber_count(EventType.ERROR_OCCURRED) == 0

    def test_event_counter(self):
        bus = EventBus()
        assert bus.event_count == 0
        bus.emit(EventType.KERNEL_INITIALIZED)
        bus.emit(EventType.ERROR_OCCURRED)
        assert bus.event_count == 2

    def test_history_order(self):
        bus = EventBus()
        bus.emit(EventType.NODE_ADDED, node="n1")
        bus.emit(EventType.NODE_UPDATED, node="n2")
        h = bus.get_history(5)
        assert len(h) == 2 and h[0].type == EventType.NODE_UPDATED

    def test_history_by_type(self):
        bus = EventBus()
        bus.emit(EventType.NODE_ADDED, node="n1")
        bus.emit(EventType.ISSUE_DETECTED, i="x")
        bus.emit(EventType.NODE_ADDED, node="n2")
        assert len(bus.get_history_by_type(EventType.NODE_ADDED)) == 2

    def test_clear_history(self):
        bus = EventBus()
        bus.emit(EventType.NODE_ADDED)
        bus.clear_history()
        assert len(bus.get_history()) == 0

    def test_max_history(self):
        bus = EventBus()
        bus._max_history = 5
        for i in range(10):
            bus.emit(EventType.NODE_ADDED, idx=i)
        assert len(bus._history) == 5 and bus._history[0].data["idx"] == 5

    def test_event_fields(self):
        ev = bus.emit(EventType.NODE_ADDED) if (bus := EventBus()) else None
        assert ev and ev.event_id and ev.timestamp > 0

    def test_bad_handler_does_not_crash(self):
        bus = EventBus()
        bus.subscribe(
            EventType.ERROR_OCCURRED, lambda e: (_ for _ in ()).throw(ValueError("bad"))
        )
        bus.emit(EventType.ERROR_OCCURRED)  # no crash

    def test_stats(self):
        bus = EventBus()
        bus.subscribe(EventType.NODE_ADDED, lambda e: None)
        bus.emit(EventType.NODE_ADDED)
        s = bus.stats()
        assert s["total_events"] == 1

    def test_emit_event_direct(self):
        bus = EventBus()
        r = []
        bus.subscribe(EventType.ERROR_OCCURRED, lambda e: r.append(e))
        bus.emit_event(Event(type=EventType.ERROR_OCCURRED, data={"msg": "fail"}))
        assert len(r) == 1 and r[0].data["msg"] == "fail"

    def test_subscriber_count_total(self):
        bus = EventBus()
        bus.subscribe(EventType.NODE_ADDED, lambda e: None)
        bus.subscribe(EventType.NODE_REMOVED, lambda e: None)
        assert bus.subscriber_count() == 2


# ═══ StateMachine Tests ═══


class TestNodeStateMachine:
    def test_initialize(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        assert sm.get_state("n1") == NodeLifecycleState.NEW

    def test_none_state(self):
        assert NodeStateMachine().get_state("x") is None

    def test_valid_transition(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        assert sm.transition("n1", NodeLifecycleState.PARSED) is True

    def test_invalid_transition(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        assert sm.transition("n1", NodeLifecycleState.RENDERED) is False

    def test_full_pipeline(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        pipeline = [
            NodeLifecycleState.PARSED,
            NodeLifecycleState.NORMALIZED,
            NodeLifecycleState.ANALYZED,
            NodeLifecycleState.PLANNED,
            NodeLifecycleState.TRANSLATED,
            NodeLifecycleState.LAYOUTED,
            NodeLifecycleState.RENDERED,
            NodeLifecycleState.VERIFIED,
            NodeLifecycleState.ARCHIVED,
        ]
        for s in pipeline:
            assert sm.transition("n1", s)
        assert sm.get_state("n1") == NodeLifecycleState.ARCHIVED
        assert sm.transition_count == len(pipeline)

    def test_error_recovery(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        sm.transition("n1", NodeLifecycleState.ERROR)
        assert sm.can_transition("n1", NodeLifecycleState.NEW)
        assert sm.can_transition("n1", NodeLifecycleState.TRANSLATED)

    def test_can_transition(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        assert sm.can_transition("n1", NodeLifecycleState.PARSED)
        assert not sm.can_transition("n1", NodeLifecycleState.RENDERED)

    def test_idempotent(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        sm.transition("n1", NodeLifecycleState.PARSED)
        assert sm.transition("n1", NodeLifecycleState.PARSED)
        assert sm.transition_count == 1

    def test_get_nodes_in_state(self):
        sm = NodeStateMachine()
        sm.set_state("n1", NodeLifecycleState.PARSED)
        sm.set_state("n2", NodeLifecycleState.PARSED)
        sm.set_state("n3", NodeLifecycleState.ANALYZED)
        assert set(sm.get_nodes_in_state(NodeLifecycleState.PARSED)) == {"n1", "n2"}

    def test_get_all_states(self):
        sm = NodeStateMachine()
        sm.set_state("n1", NodeLifecycleState.NEW)
        sm.set_state("n2", NodeLifecycleState.ERROR)
        assert sm.get_all_states() == {
            "n1": NodeLifecycleState.NEW,
            "n2": NodeLifecycleState.ERROR,
        }

    def test_on_state_change(self):
        sm = NodeStateMachine()
        log = []
        sm.on_state_change(lambda n, o, nw: log.append((n, o.value, nw.value)))
        sm.initialize("n1")
        sm.transition("n1", NodeLifecycleState.PARSED)
        assert log == [("n1", "new", "parsed")]

    def test_node_count(self):
        sm = NodeStateMachine()
        sm.set_state("n1", NodeLifecycleState.NEW)
        sm.set_state("n2", NodeLifecycleState.PARSED)
        assert sm.node_count == 2

    def test_reset(self):
        sm = NodeStateMachine()
        sm.initialize("n1")
        sm.transition("n1", NodeLifecycleState.PARSED)
        sm.reset()
        assert sm.node_count == 0 and sm.transition_count == 0

    def test_terminal_and_error(self):
        assert NodeLifecycleState.ARCHIVED.is_terminal
        assert NodeLifecycleState.ERROR.is_terminal
        assert NodeLifecycleState.ERROR.is_error
        assert not NodeLifecycleState.NEW.is_error


# ═══ DiagnosticCenter Tests ═══


class TestDiagnosticCenter:
    def test_report(self):
        dc = DiagnosticCenter()
        d = dc.report("warning", "layout", "Overlap", node_id="n1")
        assert (
            d.severity == DiagnosticSeverity.WARNING
            and d.module == "layout"
            and d.message == "Overlap"
            and d.node_id == "n1"
        )

    def test_convenience(self):
        dc = DiagnosticCenter()
        assert dc.debug("t", "d").severity == DiagnosticSeverity.DEBUG
        assert dc.info("t", "i").severity == DiagnosticSeverity.INFO
        assert dc.warning("t", "w").severity == DiagnosticSeverity.WARNING
        assert dc.error("t", "e").severity == DiagnosticSeverity.ERROR
        assert dc.critical("t", "c").severity == DiagnosticSeverity.CRITICAL

    def test_count(self):
        dc = DiagnosticCenter()
        dc.report("info", "a", "m1")
        dc.report("info", "b", "m2")
        dc.report("error", "c", "m3")
        assert dc.count() == 3 and dc.count("info") == 2 and dc.count("error") == 1

    def test_get_errors(self):
        dc = DiagnosticCenter()
        dc.report("warning", "w", "warn")
        dc.report("error", "e", "err")
        dc.report("critical", "c", "crit")
        assert len(dc.get_errors()) == 2

    def test_get_warnings(self):
        dc = DiagnosticCenter()
        dc.report("warning", "w", "warn")
        dc.report("info", "i", "info")
        assert len(dc.get_warnings()) == 1

    def test_get_by_module(self):
        dc = DiagnosticCenter()
        dc.report("info", "layout", "l1")
        dc.report("info", "layout", "l2")
        dc.report("info", "trans", "t1")
        assert (
            len(dc.get_by_module("layout")) == 2 and len(dc.get_by_module("trans")) == 1
        )

    def test_get_by_node(self):
        dc = DiagnosticCenter()
        dc.report("info", "a", "m", node_id="n1")
        dc.report("info", "b", "m", node_id="n2")
        assert len(dc.get_by_node("n1")) == 1 and len(dc.get_by_node("n3")) == 0

    def test_get_all(self):
        dc = DiagnosticCenter()
        dc.report("info", "a", "x")
        dc.report("warning", "b", "y")
        assert len(dc.get_all()) == 2

    def test_summary(self):
        dc = DiagnosticCenter()
        dc.report("info", "a", "x")
        dc.report("warning", "b", "y")
        dc.report("error", "c", "z")
        s = dc.summary()
        assert (
            s["info"] == 1 and s["warning"] == 1 and s["error"] == 1 and s["total"] == 3
        )

    def test_to_dict(self):
        dc = DiagnosticCenter()
        dc.report("info", "m", "test", node_id="n1", fix_hint="fix")
        r = dc.to_dict_list()[0]
        assert (
            r["severity"] == "info" and r["node_id"] == "n1" and r["fix_hint"] == "fix"
        )

    def test_clear(self):
        dc = DiagnosticCenter()
        dc.report("info", "a", "m")
        dc.clear()
        assert dc.count() == 0

    def test_diag_autofields(self):
        d = Diagnostic(DiagnosticSeverity.INFO, "test", "msg")
        assert d.diagnostic_id and d.timestamp > 0

    def test_max_cap(self):
        dc = DiagnosticCenter()
        dc._max = 50  # cap internal limit
        for i in range(100):
            dc.report("info", "test", f"msg-{i}")
        assert dc.count() == 50 and dc.get_all()[0].message == "msg-50"


# ═══ MemoryCenter Tests ═══


class MockBackend:
    def __init__(self):
        self._d = {}

    def put(self, k, v):
        self._d[k] = v

    def get(self, k):
        return self._d.get(k)

    def delete(self, k):
        self._d.pop(k, None)

    def clear(self):
        self._d.clear()


class TestMemoryCenter:
    def test_put_get(self):
        mc = MemoryCenter(MockBackend(), MockBackend(), MockBackend())
        mc.put("k", "v")
        assert mc.get("k") == "v"

    def test_missing(self):
        assert MemoryCenter(MockBackend()).get("k") is None

    def test_memory_preferred(self):
        mem = MockBackend()
        cache = MockBackend()
        mc = MemoryCenter(mem, cache, MockBackend())
        mc.put("k", "mem_v")
        cache.put("k", "cache_v")
        assert mc.get("k") == "mem_v"

    def test_falls_to_cache(self):
        cache = MockBackend()
        cache.put("k", "cache_v")
        assert MemoryCenter(MockBackend(), cache, MockBackend()).get("k") == "cache_v"

    def test_falls_to_persistent(self):
        per = MockBackend()
        per.put("k", "per_v")
        assert MemoryCenter(MockBackend(), MockBackend(), per).get("k") == "per_v"

    def test_falls_through_tiers(self):
        mem = MockBackend()
        cache = MockBackend()
        per = MockBackend()
        per.put("k", "pv")
        mc = MemoryCenter(mem, cache, per)
        val = mc.get("k")
        assert val == "pv"

    def test_has(self):
        mc = MemoryCenter(MockBackend())
        mc.put("k", "v")
        assert mc.has("k") and not mc.has("x")

    def test_delete_all_tiers(self):
        m = MockBackend()
        c = MockBackend()
        p = MockBackend()
        for b in (m, c, p):
            b.put("k", "v")
        MemoryCenter(m, c, p).delete("k")
        assert all(b.get("k") is None for b in (m, c, p))

    def test_clear_all(self):
        m = MockBackend()
        m.put("k1", "v1")
        c = MockBackend()
        c.put("k2", "v2")
        mc = MemoryCenter(m, c)
        mc.clear_all()
        assert m.get("k1") is None and c.get("k2") is None

    def test_hit_rate(self):
        mc = MemoryCenter(MockBackend())
        mc.get("a")
        mc.get("b")
        mc.put("c", "v")
        mc.get("c")
        assert 0 < mc.hit_rate < 1.0

    def test_perfect_hit_rate(self):
        mc = MemoryCenter(MockBackend())
        mc.put("k", "v")
        mc.get("k")
        assert mc.hit_rate == 1.0

    def test_stats(self):
        mc = MemoryCenter(MockBackend())
        mc.put("k", "v")
        mc.get("k")
        s = mc.stats()
        assert "lookups" in s and "memory_hits" in s

    def test_no_crash_empty(self):
        mc = MemoryCenter()
        assert mc.get("x") is None
        mc.put("x", "v")
        mc.delete("x")

    def test_broken_backend(self):
        class B(MockBackend):
            def get(self, k):
                raise RuntimeError("broken")

        assert MemoryCenter(B()).get("x") is None

    def test_backend_properties(self):
        mc = MemoryCenter()
        mc.memory = MockBackend()
        mc.cache = MockBackend()
        mc.persistent = MockBackend()
        assert mc.memory and mc.cache and mc.persistent


# ═══ PluginManager Tests ═══


class _PluginForTest(Plugin):
    plugin_id = "test_plugin"
    version = "2.0.0"
    description = "Test plugin"


class _PluginForTestManager:
    def test_register_get(self):
        pm = PluginManager()
        p = _PluginForTest()
        pm.register(p)
        assert pm.get("test_plugin") is p

    def test_dup_raises(self):
        pm = PluginManager()
        pm.register(_PluginForTest())
        import pytest

        pytest.raises(ValueError, pm.register, _PluginForTest())

    def test_empty_id_raises(self):
        class E(Plugin):
            plugin_id = ""

        import pytest

        pytest.raises(ValueError, PluginManager().register, E())

    def test_unregister(self):
        pm = PluginManager()
        p = _PluginForTest()
        pm.register(p)
        pm.unregister("test_plugin")
        assert pm.get("test_plugin") is None

    def test_get_all(self):
        pm = PluginManager()

        class P1(Plugin):
            plugin_id = "p1"

        class P2(Plugin):
            plugin_id = "p2"

        pm.register(P1())
        pm.register(P2())
        assert len(pm.get_all()) == 2

    def test_lifecycle(self):
        pm = PluginManager()
        p = _PluginForTest()
        pm.register(p)
        assert p.state == PluginState.REGISTERED
        pm.initialize_all(None)
        assert p.state == PluginState.INITIALIZED
        pm.start_all()
        assert p.state == PluginState.STARTED
        pm.stop_all()
        assert p.state == PluginState.STOPPED

    def test_get_by_state(self):
        pm = PluginManager()
        p = _PluginForTest()
        pm.register(p)
        pm.initialize_all(None)
        assert len(pm.get_by_state(PluginState.INITIALIZED)) == 1
        pm.start_all()
        assert len(pm.get_by_state(PluginState.STARTED)) == 1

    def test_init_error(self):
        pm = PluginManager()

        class B(Plugin):
            plugin_id = "bad"

            def initialize(self, k):
                raise RuntimeError("fail")

        p = B()
        pm.register(p)
        pm.initialize_all(None)
        assert p.state == PluginState.ERROR

    def test_start_error(self):
        pm = PluginManager()

        class B(Plugin):
            plugin_id = "bad"

            def start(self):
                raise RuntimeError("fail")

        p = B()
        pm.register(p)
        pm.initialize_all(None)
        pm.start_all()
        assert p.state == PluginState.ERROR

    def test_list_plugins(self):
        pm = PluginManager()
        pm.register(_PluginForTest())
        pm.initialize_all(None)
        pm.start_all()
        l = pm.list_plugins()
        assert l[0]["id"] == "test_plugin" and l[0]["state"] == "started"

    def test_counts(self):
        pm = PluginManager()
        pm.register(_PluginForTest())
        assert pm.plugin_count == 1 and pm.running_count == 0
        pm.initialize_all(None)
        pm.start_all()
        assert pm.running_count == 1
        pm.stop_all()
        assert pm.running_count == 0


# ═══ RuntimeKernel Integration Tests ═══


class TestRuntimeKernel:
    def test_init(self):
        rk = RuntimeKernel()
        assert not rk.initialized
        rk.initialize()
        assert rk.initialized

    def test_kernel_id(self):
        assert len(RuntimeKernel().kernel_id) == 8

    def test_start_shutdown(self):
        rk = RuntimeKernel()
        rk.start()
        assert rk.initialized
        rk.shutdown()
        assert not rk.initialized

    def test_uptime(self):
        rk = RuntimeKernel()
        assert rk.uptime == 0.0
        rk.initialize()
        assert rk.uptime >= 0.0

    def test_service_locator(self):
        from pdf2zh.v3.service import ServiceRegistry

        rk = RuntimeKernel()
        assert rk.service_locator is ServiceRegistry.get_instance()

    def test_emit_event(self):
        rk = RuntimeKernel()
        rk.initialize()
        r = []
        rk.on(EventType.TRANSLATION_COMPLETED, lambda e: r.append(e))
        rk.emit(EventType.TRANSLATION_COMPLETED, node_id="n1")
        assert len(r) == 1

    def test_on_error(self):
        rk = RuntimeKernel()
        r = []
        rk.on_error(lambda e: r.append(e))
        rk.emit(EventType.ERROR_OCCURRED, error="test")
        assert len(r) == 1

    def test_node_lifecycle(self):
        rk = RuntimeKernel()
        rk.initialize()
        rk.initialize_node("doc1")
        assert rk.get_node_state("doc1") == NodeLifecycleState.NEW
        assert rk.transition_node("doc1", NodeLifecycleState.PARSED)
        assert rk.transition_node("doc1", NodeLifecycleState.NORMALIZED)
        assert rk.get_node_state("doc1") == NodeLifecycleState.NORMALIZED
        assert not rk.transition_node("doc1", NodeLifecycleState.RENDERED)

    def test_transition_emits_event(self):
        rk = RuntimeKernel()
        rk.initialize()
        e = []
        rk.on(EventType.NODE_UPDATED, lambda ev: e.append(ev.data))
        rk.initialize_node("n1")
        rk.transition_node("n1", NodeLifecycleState.PARSED)
        assert len(e) == 1 and e[0]["state"] == "parsed"

    def test_diagnose_emits_issue_for_errors(self):
        rk = RuntimeKernel()
        rk.initialize()
        i = []
        rk.on(EventType.ISSUE_DETECTED, lambda e: i.append(e.data))
        rk.diagnose("error", "trans", "API failure", fix_hint="retry")
        assert len(i) == 1 and i[0]["diagnostic"]["severity"] == "error"

    def test_diagnose_skips_info(self):
        rk = RuntimeKernel()
        rk.initialize()
        i = []
        rk.on(EventType.ISSUE_DETECTED, lambda e: i.append(e))
        rk.diagnose("info", "test", "info msg")
        assert len(i) == 0

    def test_kernel_initialized_event(self):
        rk = RuntimeKernel()
        e = []
        rk.on(EventType.KERNEL_INITIALIZED, lambda ev: e.append(ev))
        rk.initialize()
        assert len(e) == 1

    def test_kernel_shutdown_event(self):
        rk = RuntimeKernel()
        rk.initialize()
        e = []
        rk.on(EventType.KERNEL_SHUTDOWN, lambda ev: e.append(ev))
        rk.shutdown()
        assert len(e) == 1

    def test_stats_structure(self):
        rk = RuntimeKernel()
        rk.initialize()
        s = rk.stats()
        for k in (
            "initialized",
            "kernel_id",
            "event_bus",
            "state_machine",
            "diagnostic_center",
            "memory_center",
            "plugin_manager",
            "services",
        ):
            assert k in s

    def test_summary(self):
        rk = RuntimeKernel()
        rk.initialize()
        s = rk.summary()
        assert "RuntimeKernel" in s and "RUNNING" in s

    def test_unknown_node_state(self):
        rk = RuntimeKernel()
        rk.initialize()
        assert rk.get_node_state("nonexistent") is None

    def test_memory_no_backends(self):
        rk = RuntimeKernel()
        rk.initialize()
        rk.memory_center.put("k", "v")
        assert rk.memory_center.get("k") is None

    def test_plugin_init(self):
        rk = RuntimeKernel()
        rk.initialize()
        p = _PluginForTest()
        rk.plugin_manager.register(p)
        rk.plugin_manager.initialize_all(rk)
        assert p.state == PluginState.INITIALIZED and p.kernel is rk

    def test_service_register(self):
        from pdf2zh.v3.service import MemoryService

        rk = RuntimeKernel()
        rk.initialize()
        svc = MemoryService()
        rk.register_service(MemoryService, svc)
        assert rk.get_service(MemoryService) is svc

    def test_start_initializes(self):
        rk = RuntimeKernel()
        rk.start()
        assert rk.initialized

    def test_shutdown_idempotent(self):
        rk = RuntimeKernel()
        rk.initialize()
        rk.shutdown()
        rk.shutdown()
        assert not rk.initialized

    def test_diagnose_returns_diagnostic(self):
        rk = RuntimeKernel()
        rk.initialize()
        d = rk.diagnose("warning", "layout", "overlap", node_id="n1", fix_hint="shift")
        assert (
            d.severity == DiagnosticSeverity.WARNING
            and d.node_id == "n1"
            and d.fix_hint == "shift"
        )

    def test_diagnose_adds_to_center(self):
        rk = RuntimeKernel()
        rk.initialize()
        rk.diagnose("info", "t", "m1")
        rk.diagnose("error", "t", "m2")
        assert (
            rk.diagnostic_center.count() == 2
            and rk.diagnostic_center.count("error") == 1
        )

    def test_invalid_transition_no_event(self):
        rk = RuntimeKernel()
        rk.initialize()
        e = []
        rk.on(EventType.NODE_UPDATED, lambda ev: e.append(ev))
        rk.initialize_node("n1")
        rk.transition_node("n1", NodeLifecycleState.RENDERED)
        assert len(e) == 0

    def test_summary_after_shutdown(self):
        rk = RuntimeKernel()
        rk.initialize()
        rk.shutdown()
        assert "STOPPED" in rk.summary()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=long"])


class TestPriorityLevel:
    def test_priority_values(self):
        assert PriorityLevel.LOW.value == 0
        assert PriorityLevel.NORMAL.value == 1
        assert PriorityLevel.HIGH.value == 2
        assert PriorityLevel.CRITICAL.value == 3


class TestDeadLetterRecord:
    def test_creation(self):
        dl = DeadLetterRecord(
            event=Event(type=EventType.ERROR_OCCURRED), error="failed"
        )
        assert dl.error == "failed" and dl.timestamp > 0


class TestEventBusDeadLetter:
    def test_dead_letter_count(self):
        bus = EventBus()
        bus.subscribe_all(lambda e: (_ for _ in ()).throw(Exception("x")))
        bus.emit(EventType.NODE_ADDED)
        assert bus.dead_letter_count == 1

    def test_dead_letter_stats(self):
        bus = EventBus()
        bus.subscribe_all(lambda e: (_ for _ in ()).throw(Exception("x")))
        bus.emit(EventType.NODE_ADDED, x=1)
        assert "dead_letter_count" in bus.stats()


class TestCapabilityPluginMgr:
    def test_find_by_cap(self):
        pm = PluginManager()

        class P(CapabilityPlugin):
            plugin_id = "p"
            capabilities = {Capability.PARSER}

        pm.register(P())
        assert len(pm.find_by_capability(Capability.PARSER)) == 1

    def test_get_best(self):
        pm = PluginManager()

        class A(CapabilityPlugin):
            plugin_id = "a"
            capabilities = {Capability.PARSER}
            capability_priority = 30

        class B(CapabilityPlugin):
            plugin_id = "b"
            capabilities = {Capability.PARSER}
            capability_priority = 90

        pm.register(A())
        pm.register(B())
        assert pm.get_best_for_capability(Capability.PARSER).plugin_id == "b"


class TestRuntimeTransaction:
    def test_begin_commit(self):
        rk = RuntimeKernel()
        rk.state_machine.initialize("n1")
        txn = rk.begin_transaction()
        assert txn.active
        rk.state_machine.transition("n1", NodeLifecycleState.PARSED)
        rk.commit_transaction()
        assert not txn.active

    def test_rollback_restores(self):
        rk = RuntimeKernel()
        rk.state_machine.initialize("n1")
        rk.state_machine.transition("n1", NodeLifecycleState.PARSED)
        rk.begin_transaction()
        rk.state_machine.transition("n1", NodeLifecycleState.NORMALIZED)
        rk.rollback_transaction()
        assert rk.get_node_state("n1") == NodeLifecycleState.PARSED


class TestKnowledgeCenter:
    def test_learn_query(self):
        kc = KnowledgeCenter()
        kc.learn("PDF", "Portable Document Format")
        assert kc.query("PDF").definition == "Portable Document Format"

    def test_aliases(self):
        kc = KnowledgeCenter()
        kc.learn("PDF", aliases={"pdf"})
        assert kc.resolve_alias("pdf") == "PDF"

    def test_cross_refs(self):
        kc = KnowledgeCenter()
        kc.learn("PDF", cross_refs={"doc"})
        assert "doc" in kc.query_cross_references("PDF")

    def test_clear(self):
        kc = KnowledgeCenter()
        kc.learn("X")
        kc.clear()
        assert kc.entry_count() == 0


class TestDiagnosticGraph:
    def test_add_and_fix(self):
        dg = DiagnosticGraph()
        d = Diagnostic(severity=DiagnosticSeverity.ERROR, module="t", message="m")
        n = dg.add_diagnostic(d)
        assert dg.total_count() == 1
        dg.add_fix(n.diagnostic_id, "done", True)
        assert dg.resolve_count() == 1

    def test_clear(self):
        dg = DiagnosticGraph()
        dg.add_diagnostic(
            Diagnostic(severity=DiagnosticSeverity.INFO, module="t", message="m")
        )
        dg.clear()
        assert dg.total_count() == 0


class TestTelemetry:
    def test_record_and_summary(self):
        tc = TelemetryCollector()
        tc.record("op", 100.0)
        tc.record("op", 200.0)
        s = tc.summary()
        assert s["total"] == 2 and s["operations"]["op"]["avg_ms"] == 150.0

    def test_errors(self):
        tc = TelemetryCollector()
        tc.record("op", 10.0, True)
        tc.record("op", 20.0, False)
        assert tc.get_errors("op")["op"] == 1 and tc.summary()["error_rate"] == 0.5

    def test_timed_context(self):
        tc = TelemetryCollector()
        with tc.record_timed("tr"):
            pass
        assert tc.summary()["total"] == 1


class TestRuntimeKernelV4:
    def test_learn_and_telemetry(self):
        rk = RuntimeKernel()
        rk.learn("PDF")
        rk.record_telemetry("op", 1.0)
        s = rk.stats()
        assert s["knowledge_center"]["entries"] == 1 and s["telemetry"]["total"] == 1

    def test_diagnose_graph_integration(self):
        rk = RuntimeKernel()
        n = rk.diagnose_graph(DiagnosticSeverity.ERROR, "ly", "overflow")
        rk.resolve_diagnostic(n.diagnostic_id, "done", True)
        assert rk.diagnostic_graph.resolve_count() == 1

    def test_transaction_integration(self):
        rk = RuntimeKernel()
        rk.state_machine.initialize("n1")
        rk.begin_transaction()
        rk.state_machine.transition("n1", NodeLifecycleState.PARSED)
        rk.rollback_transaction()
        assert rk.get_node_state("n1") == NodeLifecycleState.NEW


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=long"])
