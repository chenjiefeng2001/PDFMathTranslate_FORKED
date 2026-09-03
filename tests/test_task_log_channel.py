"""Tests for the task log channel / engine-log bridge / trace CLI flags.

Covers the v1.1 logging surface:
- ``RuntimeService._emit_log`` produces ``TaskLogEvent`` lines (metadata ts /
  level / engine / kind), collapses consecutive duplicates and caps floods;
- ``_maybe_detail_log`` samples structured fine-grained counts into readable
  milestone lines (~100 evenly spaced);
- the engine log bridge forwards MinerU/BabelDOC logger lines only while an
  engine task context is active and only from engine namespaces at INFO;
- ``--trace`` / ``--trace-dir`` / ``--log-file`` parse with trace defaulting
  to off.
"""

from __future__ import annotations

import pytest

from pdf2zh.services.runtime_service import RuntimeService, TaskLogEvent


def _service_with_task(task_id: str = "t-log") -> RuntimeService:
    svc = RuntimeService()
    svc._store.create_task(task_id)
    return svc


def test_emit_log_produces_metadata_events() -> None:
    svc = _service_with_task()
    seen = []
    svc.add_event_listener(lambda evt: seen.append(evt))
    svc._emit_log("t-log", "page layout model loaded", engine="mineru", kind="engine")
    events = svc._store.get_events("t-log")
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, TaskLogEvent)
    assert evt.engine == "mineru" and evt.kind == "engine"
    assert evt.level == "info" and evt.message == "page layout model loaded"
    assert evt.timestamp > 0
    assert seen == [evt]
    assert evt.to_dict()["engine"] == "mineru"


def test_emit_log_collapses_consecutive_duplicates() -> None:
    svc = _service_with_task()
    for _ in range(5):
        svc._emit_log("t-log", "same line", engine="mineru")
    events = svc._store.get_events("t-log")
    assert len(events) == 1


def test_maybe_detail_log_samples_milestones() -> None:
    svc = _service_with_task()
    for current in range(1, 121):  # 120 pages
        svc._maybe_detail_log(
            "t-log",
            {"current": current, "total": 120, "unit": "page", "raw_stage": "Parse"},
            stage="parsing",
            engine="mineru",
        )
    messages = [e.message for e in svc._store.get_events("t-log")]
    assert "Parse: 1/120 page" in messages  # start
    assert "Parse: 120/120 page" in messages  # end
    assert "Parse: 50/120 page" in messages
    assert len(messages) <= 103  # ~100 milestones, no flood


def test_engine_bridge_forwards_only_inside_ctx() -> None:
    import logging

    from pdf2zh.services.engine_log_bridge import (
        engine_task,
        install_engine_log_bridge,
        uninstall_engine_log_bridge,
    )

    got = []
    install_engine_log_bridge(lambda t, lvl, eng, msg: got.append((t, lvl, eng, msg)))
    try:
        logging.getLogger("magic_pdf.core.model").info("ignored outside ctx")
        with engine_task("T1", "mineru"):
            logging.getLogger("magic_pdf.core.model").info("loading model")
            # service's own INFO logger must not be duplicated
            logging.getLogger("pdf2zh.services.runtime_service").info("noise")
        with engine_task("T2", "babeldoc"):
            logging.getLogger("doclayout").warning("low confidence")
    finally:
        uninstall_engine_log_bridge()
    assert ("T1", "info", "mineru", "loading model") in got
    assert not any(g[0] == "T1" and g[3] == "noise" for g in got)
    assert ("T2", "warning", "babeldoc", "low confidence") in got


def test_cli_trace_flags_default_off_and_custom_dir() -> None:
    from pdf2zh.pdf2zh import create_parser

    ns = create_parser().parse_args(["in.pdf"])
    assert getattr(ns, "trace", False) is False
    assert getattr(ns, "trace_dir", "") == ""
    assert getattr(ns, "log_file", "") == ""
    ns2 = create_parser().parse_args(
        ["in.pdf", "--trace", "--trace-dir", "/tmp/logs", "--log-file", "/tmp/app.log"]
    )
    assert ns2.trace is True
    assert ns2.trace_dir == "/tmp/logs"
    assert ns2.log_file == "/tmp/app.log"


def test_cli_ingest_backend_auto_default_and_choices() -> None:
    from pdf2zh.pdf2zh import create_parser

    ns = create_parser().parse_args(["in.pdf"])
    assert ns.ingest_backend == "auto"  # v1.1: auto 是默认摄入语义
    assert (
        create_parser()
        .parse_args(["in.pdf", "--ingest-backend", "mineru"])
        .ingest_backend
        == "mineru"
    )
    assert (
        create_parser()
        .parse_args(["in.pdf", "--ingest-backend", "marker"])
        .ingest_backend
        == "marker"
    )
    with pytest.raises(SystemExit):
        create_parser().parse_args(["in.pdf", "--ingest-backend", "bogus"])
