"""Headless tests for the pdf2zh EventBus / delta-sync layer (Stage 2).

Verifies the "Worker publishes events -> EventBus -> UI consumes events"
architecture:

  1. Typed domain events (fields, ``event_type``, ``to_dict``).
  2. EventBus publish/subscribe/filter/unsubscribe + delta cursors.
  3. EventBus thread-safety.
  4. RuntimeService listener API (add/remove/clear + notify on ``_emit_event``).
  5. TaskEventBridge translation (low-level records -> typed domain events).
  6. app.drain_events delta sync contract (19+1 tuple, untouched components
     return the no-op ``gr.update()`` patch).
  7. Control handlers publish events instead of mutating UI directly.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.gui.events import (
    ALL_EVENT_TYPES,
    EVENT_BUS,
    DiagnosticsUpdated,
    EventBus,
    FileGenerated,
    PreviewReady,
    TaskCancelled,
    TaskEvent,
    TaskFailed,
    TaskFinished,
    TaskMessageChanged,
    TaskPaused,
    TaskProgressChanged,
    TaskResumed,
    TaskSkipped,
    TaskStageChanged,
    TaskStarted,
)
from pdf2zh.gui.event_bridge import TaskEventBridge


def _task_state(**kwargs):
    from pdf2zh.services.runtime_service import TaskState

    task_id = kwargs.pop("task_id", "t1")
    return TaskState(task_id=task_id, **kwargs)


class _FakeService:
    """Minimal RuntimeService stand-in exposing the listener API."""

    def __init__(self, state=None):
        self.listeners = []
        self.state = state

    def add_event_listener(self, fn):
        if fn not in self.listeners:
            self.listeners.append(fn)

    def remove_event_listener(self, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    def clear_event_listeners(self):
        self.listeners.clear()

    def get_task_state(self, task_id):
        return self.state


@pytest.fixture(autouse=True)
def _clean_global_bus_and_store():
    """Isolate tests from the app-wide singleton bus / GUI store."""
    yield
    EVENT_BUS.reset()
    from pdf2zh.gui.state import GLOBAL_TASK_STORE

    for tid in list(GLOBAL_TASK_STORE.list_tasks()):
        GLOBAL_TASK_STORE.remove(tid)


# =============================================================================
# 1. Typed domain events
# =============================================================================


class TestTaskEvent:
    def test_event_type_is_class_name(self):
        assert TaskStarted(task_id="t1").event_type == "TaskStarted"
        assert (
            TaskProgressChanged(task_id="t1", progress=5.0).event_type
            == "TaskProgressChanged"
        )

    def test_is_task_event_subclass(self):
        for cls in ALL_EVENT_TYPES:
            assert issubclass(cls, TaskEvent)

    def test_to_dict_includes_type_and_fields(self):
        ev = TaskStageChanged(
            task_id="t1", stage="parsing", prev_stage="pending", progress=10.0
        )
        d = ev.to_dict()
        assert d["event_type"] == "TaskStageChanged"
        assert d["task_id"] == "t1"
        assert d["stage"] == "parsing"
        assert d["prev_stage"] == "pending"
        assert d["progress"] == 10.0

    def test_all_event_types_registered(self):
        names = {c.__name__ for c in ALL_EVENT_TYPES}
        for expected in (
            "TaskStarted",
            "TaskStageChanged",
            "TaskProgressChanged",
            "TaskMessageChanged",
            "TaskPaused",
            "TaskResumed",
            "TaskSkipped",
            "TaskCancelled",
            "TaskFailed",
            "TaskFinished",
            "FileGenerated",
            "PreviewReady",
            "DiagnosticsUpdated",
        ):
            assert expected in names


# =============================================================================
# 2. EventBus publish / subscribe / delta cursors
# =============================================================================


class TestEventBus:
    def test_publish_assigns_monotonic_sequences(self):
        bus = EventBus()
        e1 = bus.publish(TaskStarted(task_id="t1"))
        e2 = bus.publish(TaskProgressChanged(task_id="t1", progress=10.0))
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert bus.last_sequence("t1") == 2

    def test_subscribe_receives_all_events(self):
        bus = EventBus()
        got = []
        bus.subscribe(got.append)
        bus.publish(TaskStarted(task_id="t1"))
        bus.publish(TaskFinished(task_id="t1"))
        assert [e.event_type for e in got] == ["TaskStarted", "TaskFinished"]

    def test_subscribe_filters_by_event_type(self):
        bus = EventBus()
        got = []
        bus.subscribe(got.append, event_types=["TaskProgressChanged"])
        bus.publish(TaskStarted(task_id="t1"))
        bus.publish(TaskProgressChanged(task_id="t1", progress=5.0))
        assert [e.event_type for e in got] == ["TaskProgressChanged"]

    def test_subscribe_filters_by_task_id(self):
        bus = EventBus()
        got = []
        bus.subscribe(got.append, task_id="t1")
        bus.publish(TaskStarted(task_id="t1"))
        bus.publish(TaskStarted(task_id="t2"))
        assert [e.task_id for e in got] == ["t1"]

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        got = []
        sub = bus.subscribe(got.append)
        assert bus.unsubscribe(sub) is True
        assert bus.unsubscribe(sub) is False
        bus.publish(TaskStarted(task_id="t1"))
        assert got == []

    def test_events_since_delta_cursor(self):
        bus = EventBus()
        bus.publish(TaskStarted(task_id="t1"))
        bus.publish(TaskProgressChanged(task_id="t1", progress=10.0))
        bus.publish(TaskProgressChanged(task_id="t1", progress=20.0))
        assert [e.sequence for e in bus.events_since("t1", 0)] == [1, 2, 3]
        assert [e.sequence for e in bus.events_since("t1", 2)] == [3]
        assert bus.has_events("t1", 2) is True
        assert bus.has_events("t1", 3) is False

    def test_events_per_task_are_isolated(self):
        bus = EventBus()
        bus.publish(TaskStarted(task_id="t1"))
        bus.publish(TaskStarted(task_id="t2"))
        assert bus.last_sequence("t1") == 1
        assert bus.last_sequence("t2") == 2
        assert len(bus.events_since("t1", 0)) == 1
        assert len(bus.events_since("t2", 0)) == 1

    def test_events_after_global_cursor_crosses_tasks(self):
        bus = EventBus()
        bus.publish(TaskStarted(task_id="t1"))  # seq 1
        bus.publish(TaskStarted(task_id="t2"))  # seq 2
        bus.publish(TaskProgressChanged(task_id="t1", progress=10.0))  # seq 3
        bus.publish(TaskMessageChanged(task_id="t2", message="x"))  # seq 4
        after = bus.events_after(1)
        assert [e.sequence for e in after] == [2, 3, 4]
        assert [e.task_id for e in after] == ["t2", "t1", "t2"]
        # publication order preserved, not per-task order
        assert after[1].event_type == "TaskProgressChanged"
        assert bus.events_after(4) == []
        assert bus.events_after(0)[0].sequence == 1

    def test_events_after_respects_bounded_history(self):
        bus = EventBus(max_history_per_task=2)
        for i in range(4):
            bus.publish(TaskProgressChanged(task_id="t1", progress=float(i)))
        # seqs 1,2 rolled off; global cursor still returns surviving events
        assert [e.sequence for e in bus.events_after(2)] == [3, 4]

    def test_replay_chronological_order(self):
        bus = EventBus()
        for pct in (10, 20, 30):
            bus.publish(TaskProgressChanged(task_id="t1", progress=pct))
        assert [e.progress for e in bus.replay("t1")] == [10.0, 20.0, 30.0]
        assert len(bus.replay("t1", limit=2)) == 2

    def test_clear_and_reset(self):
        bus = EventBus()
        got = []
        bus.subscribe(got.append)
        bus.publish(TaskStarted(task_id="t1"))
        bus.clear("t1")
        assert bus.replay("t1") == []
        bus.publish(TaskStarted(task_id="t2"))
        bus.reset()
        assert bus.replay("t2") == []
        assert bus.subscriber_count() == 0
        assert bus.last_sequence("t2") == 0

    def test_max_history_per_task(self):
        bus = EventBus(max_history_per_task=3)
        for i in range(5):
            bus.publish(TaskProgressChanged(task_id="t1", progress=float(i)))
        assert len(bus.replay("t1")) == 3
        # oldest event rolled off; cursor still correct
        assert bus.last_sequence("t1") == 5

    def test_thread_safety(self):
        bus = EventBus()
        seqs = []
        seq_lock = threading.Lock()
        errors = []

        def collect(ev):
            with seq_lock:
                seqs.append(ev.sequence)

        bus.subscribe(collect)

        def worker(tag):
            try:
                for i in range(150):
                    bus.publish(
                        TaskProgressChanged(task_id=tag, progress=float(i))
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"t{n}",)) for n in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(seqs) == 4 * 150
        assert len(set(seqs)) == 4 * 150  # no duplicate / lost sequences


# =============================================================================
# 3. RuntimeService listener API (Worker -> EventBus bridge hook)
# =============================================================================


class TestRuntimeServiceListeners:
    def test_add_remove_clear_listeners(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        got = []
        svc.add_event_listener(got.append)
        svc.add_event_listener(got.append)  # duplicates ignored
        svc._emit_event("t1", TaskStage.PARSING.value, 10.0, "hi")
        assert len(got) == 1
        assert got[0].stage == "parsing"
        assert got[0].progress == 10.0

        svc.remove_event_listener(got.append)
        svc._emit_event("t1", TaskStage.PLANNING.value, 20.0)
        assert len(got) == 1

        svc.clear_event_listeners()
        svc._emit_event("t1", TaskStage.TRANSLATING.value, 30.0)
        assert len(got) == 1

    def test_listener_error_does_not_break_emit(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()

        def bad(ev):
            raise RuntimeError("boom")

        got = []
        svc.add_event_listener(bad)
        svc.add_event_listener(got.append)
        svc._emit_event("t1", TaskStage.PARSING.value, 5.0)
        assert len(got) == 1


# =============================================================================
# 4. TaskEventBridge translation
# =============================================================================


class TestTaskEventBridge:
    def test_start_stop_idempotent(self):
        svc = _FakeService()
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        assert bridge.listening is False
        bridge.start()
        bridge.start()
        assert bridge.listening is True
        assert len(svc.listeners) == 1
        bridge.stop()
        bridge.stop()
        assert bridge.listening is False
        assert len(svc.listeners) == 0

    def test_translates_progress_stream(self):
        from pdf2zh.services.runtime_service import (
            TaskProgressEvent,
            TaskStage,
        )

        svc = _FakeService()
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        bridge.start()

        stream = [
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.PARSING.value,
                progress=5.0, message="Starting...",
            ),
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.PARSING.value,
                progress=10.0, message="Parsing PDF...",
            ),
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.TRANSLATING.value,
                progress=50.0, message="Translating...",
            ),
        ]
        for e in stream:
            svc.listeners[0](e)

        replay = bus.replay("t1")
        types = [e.event_type for e in replay]
        assert "TaskStarted" in types
        # TaskStageChanged once per distinct stage (pending -> parsing -> translating)
        assert types.count("TaskStageChanged") == 2
        assert types.count("TaskProgressChanged") == 3
        assert types.count("TaskMessageChanged") == 3
        assert "TaskFinished" not in types

    def test_pending_stage_does_not_emit_started(self):
        from pdf2zh.services.runtime_service import (
            TaskProgressEvent,
            TaskStage,
        )

        svc = _FakeService()
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        bridge.start()
        svc.listeners[0](
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.PENDING.value, progress=0.0
            )
        )
        types = [e.event_type for e in bus.replay("t1")]
        assert "TaskStarted" not in types
        assert "TaskProgressChanged" in types

    def test_completed_publishes_terminal_and_output_events(self, tmp_path):
        from pdf2zh.services.runtime_service import (
            TaskProgressEvent,
            TaskStage,
        )

        out_file = tmp_path / "dual.pdf"
        out_file.write_bytes(b"%PDF-1.7 fake")
        state = _task_state(
            status="completed",
            result_files=[{"name": "dual.pdf", "path": str(out_file)}],
            result_zip=str(out_file),
            preview_path=str(out_file),
            diagnostic_summary="All checks passed",
            quality_scores={"translation_score": 95.0},
        )
        svc = _FakeService(state=state)
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        bridge.start()
        svc.listeners[0](
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.COMPLETED.value,
                progress=100.0, message="Complete!",
            )
        )
        types = [e.event_type for e in bus.replay("t1")]
        for expected in (
            "TaskStarted",
            "TaskStageChanged",
            "TaskProgressChanged",
            "TaskMessageChanged",
            "TaskFinished",
            "FileGenerated",
            "PreviewReady",
            "DiagnosticsUpdated",
        ):
            assert expected in types, expected
        file_ev = [
            e for e in bus.replay("t1") if e.event_type == "FileGenerated"
        ][0]
        assert file_ev.files[0]["name"] == "dual.pdf"
        assert file_ev.zip_path == str(out_file)
        preview_ev = [
            e for e in bus.replay("t1") if e.event_type == "PreviewReady"
        ][0]
        assert preview_ev.preview_path == str(out_file)

    def test_failed_publishes_task_failed(self):
        from pdf2zh.services.runtime_service import (
            TaskProgressEvent,
            TaskStage,
        )

        svc = _FakeService()
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        bridge.start()
        svc.listeners[0](
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.FAILED.value,
                progress=100.0, message="boom",
            )
        )
        failed = [
            e for e in bus.replay("t1") if e.event_type == "TaskFailed"
        ][0]
        assert failed.message == "boom"

    def test_integration_with_real_runtime_service(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        bus = EventBus()
        bridge = TaskEventBridge(bus=bus, service=svc)
        bridge.start()
        # Emulate the worker path: service emits -> bridge converts.
        svc._emit_event("t1", TaskStage.PARSING.value, 5.0, "Starting")
        svc._emit_event("t1", TaskStage.TRANSLATING.value, 50.0, "Working")
        types = [e.event_type for e in bus.replay("t1")]
        assert "TaskStarted" in types
        assert types.count("TaskStageChanged") == 2
        bridge.stop()


# =============================================================================
# 5. app.py delta sync (drain_events) contract
# =============================================================================


def _register_gui_task(task_id="t1"):
    """Make ``task_id`` resolvable by the app without the real worker."""
    from pdf2zh.gui.state import GLOBAL_TASK_STORE, TaskState

    GLOBAL_TASK_STORE.set(task_id, TaskState(task_id=task_id))


_NOOP = {"__type__": "update"}


class TestDeltaSync:
    def test_sync_status_no_task_returns_idle_20(self):
        import pdf2zh.gui.app as app

        assert len(app.sync_status("")) == 20
        assert len(app._idle_updates()) == 20

    def test_drain_events_arity_and_idle_cursor(self):
        import pdf2zh.gui.app as app

        out, consumed = app.drain_events("", ("", 0))
        assert len(out) == 20
        assert consumed == ("", 0)

    def test_drain_events_untouched_components_are_noop(self):
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        EVENT_BUS.publish(
            TaskProgressChanged(task_id="t1", progress=35.0, stage="translating")
        )
        out, consumed = app.drain_events("t1", ("t1", 0))

        # cursor advanced past the progress event
        assert consumed[0] == "t1"
        assert consumed[1] == EVENT_BUS.last_sequence("t1")
        # progress bar re-rendered with the new value
        assert "value" in out[0]
        assert "35.0%" in str(out[0]["value"])
        # untouched components stay no-op patches
        assert out[11] == _NOOP  # result_selector
        assert out[12] == _NOOP  # download_single
        assert out[13] == _NOOP  # download_zip
        assert out[16] == _NOOP  # task_id unchanged

    def test_drain_events_second_drain_is_noop(self):
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        EVENT_BUS.publish(
            TaskProgressChanged(task_id="t1", progress=40.0, stage="translating")
        )
        _, consumed = app.drain_events("t1", ("t1", 0))
        out, consumed2 = app.drain_events("t1", consumed)
        assert consumed == consumed2
        assert all(u == _NOOP for u in out)

    def test_drain_events_terminal_flow_publishes_outputs(self, tmp_path):
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        mono = tmp_path / "doc-mono.pdf"
        dual = tmp_path / "doc-dual.pdf"
        mono.write_bytes(b"%PDF-1.7 fake mono")
        dual.write_bytes(b"%PDF-1.7 fake dual")

        EVENT_BUS.publish(TaskStarted(task_id="t1"))
        EVENT_BUS.publish(TaskFinished(task_id="t1"))
        EVENT_BUS.publish(
            FileGenerated(
                task_id="t1",
                files=[
                    {"name": "doc-mono.pdf", "path": str(mono)},
                    {"name": "doc-dual.pdf", "path": str(dual)},
                ],
                zip_path=str(dual),
            )
        )
        EVENT_BUS.publish(PreviewReady(task_id="t1", preview_path=str(dual)))
        EVENT_BUS.publish(
            DiagnosticsUpdated(
                task_id="t1",
                diagnostic_summary="All good",
                quality_scores={"translation_score": 90.0},
            )
        )
        out, consumed = app.drain_events("t1", ("t1", 0))
        assert consumed[0] == "t1"

        # result selector now offers the generated files
        sel = out[11]
        assert sel.get("choices") == ["doc-mono.pdf", "doc-dual.pdf"]
        assert sel.get("visible") is True
        # downloads reveal the produced artifacts
        assert out[12].get("value") == str(mono)
        assert out[13].get("value") == str(dual)
        # preview iframe points at the dual output
        assert "pdf-iframe-container" in str(out[15].get("value"))
        assert "doc-dual.pdf" in str(out[15].get("value"))
        # task id resolved into the UI state
        assert out[16] == "t1"
        # translate button re-enabled at completion
        assert out[2].get("interactive") is True

    def test_drain_events_task_switch_does_full_render(self):
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        EVENT_BUS.publish(TaskStarted(task_id="t1"))
        out, consumed = app.drain_events("t1", ("old_task", 3))
        # full re-render: task id populated, busy buttons locked
        assert out[16] == "t1"
        assert out[2].get("interactive") is False
        assert consumed[0] == "t1"

    def test_progress_event_does_not_touch_status_badge(self):
        """Flicker guard: high-frequency progress events must only re-render
        the progress bar. The badge (with its pulse animation) is tied to the
        stage; re-setting it per progress event would restart its animation
        and cause visible periodic flicker."""
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        EVENT_BUS.publish(
            TaskProgressChanged(task_id="t1", progress=35.0, stage="translating")
        )
        out, _ = app.drain_events("t1", ("t1", 0))
        # badge (last sync slot) untouched -> no DOM churn / animation restart
        assert out[19] == _NOOP
        assert "value" in out[0]
        assert "35.0%" in str(out[0]["value"])

    def test_stage_change_renders_badge_once(self):
        """The badge IS re-rendered when the stage itself changes."""
        import pdf2zh.gui.app as app

        _register_gui_task("t1")
        EVENT_BUS.publish(
            TaskStageChanged(
                task_id="t1", stage="translating", prev_stage="parsing", progress=50.0
            )
        )
        out, _ = app.drain_events("t1", ("t1", 0))
        assert out[19] != _NOOP
        assert "运行中 / Running" in str(out[19]["value"])

    def test_control_handlers_publish_events(self):
        import pdf2zh.gui.app as app

        tid = app.on_cancel("t1")
        assert tid == "t1"
        app.on_pause("t1")
        app.on_resume("t1")
        app.on_skip("t1")
        types = [e.event_type for e in EVENT_BUS.replay("t1")]
        for expected in (
            "TaskCancelled",
            "TaskPaused",
            "TaskResumed",
            "TaskSkipped",
        ):
            assert expected in types, expected
        cancelled = [e for e in EVENT_BUS.replay("t1")
                     if e.event_type == "TaskCancelled"][0]
        assert cancelled.message == "Cancelled by user"

    def test_event_renderers_are_registered_for_every_event(self):
        import pdf2zh.gui.app as app

        for cls in ALL_EVENT_TYPES:
            assert (
                cls.__name__ in app._EVENT_RENDERERS
            ), f"no renderer for {cls.__name__}"
