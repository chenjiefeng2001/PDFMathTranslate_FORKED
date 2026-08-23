"""细粒度进度统计（P0）回归测试。

对应 ``doc/granular_progress_feasibility_report.md`` P0：
- BabelDOC 事件流的 stage_current/stage_total 整理为结构化 detail；
- TaskProgressEvent.detail 全链贯通（事件 + store 快照）；
- 协议向后兼容（to_dict 含 detail，缺省 None）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.babeldoc_adapter import (
    _babeldoc_stage_unit,
    _progress_detail_from_event,
)
from pdf2zh.services.runtime_service import RuntimeService, TaskStage


# ── 单位映射与事件整理 ───────────────────────────────────────────────────────


def test_stage_unit_mapping():
    assert _babeldoc_stage_unit("Parse Page Layout") == "page"
    assert _babeldoc_stage_unit("Detect Scanned File") == "page"
    assert _babeldoc_stage_unit("Parse PDF and Create Intermediate Representation") == "page"
    assert _babeldoc_stage_unit("Translate Paragraphs") == "paragraph"
    assert _babeldoc_stage_unit("Extract Terms") == "term"


def test_stage_unit_unknown_is_empty():
    assert _babeldoc_stage_unit("Typesetting") == ""
    assert _babeldoc_stage_unit("") == ""


def test_detail_from_layout_event():
    event = {
        "type": "progress_update",
        "stage": "Parse Page Layout",
        "stage_progress": 42.0,
        "stage_current": 5,
        "stage_total": 12,
        "overall_progress": 30.0,
    }
    assert _progress_detail_from_event(event) == {
        "engine": "babeldoc",
        "raw_stage": "Parse Page Layout",
        "unit": "page",
        "current": 5,
        "total": 12,
    }


def test_detail_from_paragraph_event():
    event = {
        "type": "progress_update",
        "stage": "Translate Paragraphs",
        "stage_current": 80,
        "stage_total": 100,
        "overall_progress": 60.0,
    }
    detail = _progress_detail_from_event(event)
    assert detail is not None
    assert detail["unit"] == "paragraph"
    assert detail["current"] == 80


def test_detail_missing_counts_returns_none():
    # 旧版本/异常事件没有计数 -> None（调用方保持现状行为）
    assert _progress_detail_from_event(
        {"stage": "Typesetting", "overall_progress": 90.0}
    ) is None


def test_detail_garbage_counts_returns_none():
    assert _progress_detail_from_event(
        {"stage": "X", "stage_current": "abc", "stage_total": None}
    ) is None


# ── 服务层贯通：事件 + store 快照 ────────────────────────────────────────────


def _service(tid: str) -> RuntimeService:
    svc = RuntimeService()
    svc._sweeper = None
    svc._store.create_task(tid)
    return svc


def test_emit_smooth_carries_detail_into_event_and_snapshot():
    svc = _service("t_gp1")
    detail = {
        "engine": "babeldoc", "raw_stage": "Parse Page Layout",
        "unit": "page", "current": 3, "total": 10,
    }
    svc._emit_smooth("t_gp1", TaskStage.PARSING.value, 20.0, "parsing", detail=detail)

    events = svc._store.get_events("t_gp1")
    assert events and events[-1].detail == detail

    state = svc.get_task_state("t_gp1")
    assert state.stage_detail == detail


def test_emit_without_detail_keeps_snapshot_untouched():
    svc = _service("t_gp2")
    first = {"engine": "babeldoc", "raw_stage": "Parse Page Layout",
             "unit": "page", "current": 5, "total": 10}
    svc._emit_smooth("t_gp2", TaskStage.ANALYZING.value, 25.0, "a", detail=first)
    # 不带 detail 的后续发射不覆盖快照（保持最后一次已知细节）
    svc._emit_smooth("t_gp2", TaskStage.ANALYZING.value, 40.0, "b")

    state = svc.get_task_state("t_gp2")
    assert state.stage_detail == first


def test_emit_smooth_slot_path_carries_detail():
    # 并发批处理槽位路径同样要携带 detail
    from pdf2zh.services.runtime_service import _BatchContext

    svc = _service("t_gp3")
    ctx = _BatchContext(total_files=2)
    with svc._batch_ctx_lock:
        svc._batch_ctx["t_gp3"] = ctx
    detail = {"engine": "babeldoc", "raw_stage": "Parse Page Layout",
              "unit": "page", "current": 7, "total": 9}
    svc._slot_begin("t_gp3", "/tmp/a.pdf", ctx)
    try:
        svc._emit_smooth(
            "t_gp3", TaskStage.PARSING.value, 50.0, "p", detail=detail,
        )
    finally:
        svc._slot_end()

    state = svc.get_task_state("t_gp3")
    assert state.stage_detail == detail
    events = svc._store.get_events("t_gp3")
    assert events[-1].detail == detail


def test_to_dict_includes_detail_for_protocol_compat():
    from pdf2zh.services.runtime_service import TaskProgressEvent

    ev = TaskProgressEvent(task_id="t", stage="parsing", progress=10.0)
    d = ev.to_dict()
    assert "detail" in d and d["detail"] is None  # 旧前端忽略未知键，兼容

    ev2 = TaskProgressEvent(
        task_id="t", stage="analyzing", progress=20.0,
        detail={"engine": "babeldoc", "current": 1, "total": 9},
    )
    assert ev2.to_dict()["detail"]["current"] == 1
