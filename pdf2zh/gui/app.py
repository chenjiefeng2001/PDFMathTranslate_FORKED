"""Main Gradio Web UI entry point for pdf2zh.

Usage:
    python -m pdf2zh.gui.app
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import gradio as gr

from pdf2zh.gui.components.upload_panel import create_upload_panel
from pdf2zh.gui.components.config_panel import create_config_panel
from pdf2zh.gui.components.progress_panel import (
    create_progress_panel,
    build_stepbar_html,
    build_progress_bar_html,
)
from pdf2zh.gui.components.preview_panel import create_preview_panel
from pdf2zh.gui.components.diagnostic_panel import (
    create_diagnostic_panel,
    build_diagnostic_markdown,
)
from pdf2zh.gui.styles import (
    UI_CSS,
    SESSION_JS,
    TOGGLE_THEME_JS,
    build_status_badge_html,
)
from pdf2zh.gui.state import GLOBAL_TASK_STORE
from pdf2zh.gui.logger import get_handler
from pdf2zh.gui.worker import (
    get_runtime_service,
    submit_translation_task,
    _clean_stale_in_flight,
)
from pdf2zh.gui.events import (
    EVENT_BUS,
    DiagnosticsUpdated,
    FileGenerated,
    PreviewReady,
    TaskCancelled,
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
from pdf2zh.gui.event_bridge import EVENT_BRIDGE

logger = logging.getLogger(__name__)

#: Branding block rendered in the App Shell header.
BRAND_HTML = (
    "<div class='app-brand'>"
    "<span class='brand-logo'>PDF</span>"
    "<div><h1 class='brand-title'>PDFMathTranslate</h1>"
    "<p class='brand-subtitle'>Document Intelligence Runtime</p></div>"
    "</div>"
)


def _sanitize_html(text: str) -> str:
    """Sanitize text for safe HTML embedding. Removes surrogate characters."""
    cleaned = "".join(c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in text)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def on_translate(
    client_id: str, file_type: str, file_input: Any, link_input: str,
    service: str, lang_from: str, lang_to: str, page_range: str,
    page_input: Any, threads: int, skip_subset_fonts: bool,
    ignore_cache: bool, vfont: str, vchar: str, mode_choice: str,
    recaptcha_response: str, fl_state: List[str],
    env0: str, env1: str, env2: str, prompt_env: str,
    current_task_id: str,
) -> tuple:
    """Handle translate button with double-click prevention."""
    # Resolve effective client_id (supports both Gradio State and JS-side global)
    import __main__ as _main_mod
    effective_cid = client_id if client_id else getattr(_main_mod, "__pdf2zh_client_id", "")
    if not effective_cid:
        effective_cid = "client_default"
    if current_task_id:
        svc = get_runtime_service()
        ts = svc.get_task_state(current_task_id)
        if ts and ts.status in (
            "pending", "parsing", "normalizing", "analyzing",
            "planning", "translating", "layouting", "rendering",
            "evaluating", "repairing",
        ):
            return current_task_id, gr.update()
    try:
        task_id = submit_translation_task(
            client_id=effective_cid, file_type=file_type,
            file_input=file_input, link_input=link_input,
            service=service, lang_from=lang_from, lang_to=lang_to,
            page_range=page_range, page_input=page_input,
            threads=threads, skip_subset_fonts=skip_subset_fonts,
            ignore_cache=ignore_cache, vfont=vfont, vchar=vchar,
            mode_choice=mode_choice,
            recaptcha_response=recaptcha_response,
            fl_state=fl_state, env0=env0, env1=env1, env2=env2,
            prompt_env=prompt_env,
        )
    except Exception as exc:
        logger.error("Failed to submit task: %s", exc)
        return "", gr.update()
    return task_id, gr.update(interactive=False)


def on_cancel(current_task_id: str) -> str:
    """Cancel the running task and announce it on the EventBus."""
    if current_task_id:
        get_runtime_service().cancel_task(current_task_id)
        EVENT_BUS.publish(
            TaskCancelled(task_id=current_task_id, message="Cancelled by user")
        )
    return current_task_id


def on_pause(current_task_id: str) -> str:
    """Pause the running task and announce it on the EventBus."""
    if current_task_id:
        get_runtime_service().pause_task(current_task_id)
        EVENT_BUS.publish(TaskPaused(task_id=current_task_id))
    return current_task_id


def on_resume(current_task_id: str) -> str:
    """Resume a paused task and announce it on the EventBus."""
    if current_task_id:
        get_runtime_service().resume_task(current_task_id)
        EVENT_BUS.publish(TaskResumed(task_id=current_task_id))
    return current_task_id


def on_skip(current_task_id: str) -> str:
    """Skip the current file and announce it on the EventBus."""
    if current_task_id:
        get_runtime_service().skip_task(current_task_id)
        EVENT_BUS.publish(TaskSkipped(task_id=current_task_id))
    return current_task_id



def _persist_state_to_storage(task_id: str, ts) -> str:
    """Persist current task state to localStorage for cross-session recovery."""
    import json
    try:
        results_json = json.dumps(
            [{"name": r["name"], "path": r["path"]}
             for r in (getattr(ts, "result_files", None) or [])],
            ensure_ascii=False,
        )
        preview = getattr(ts, "preview_path", None) or ""
        safe_tid = json.dumps(task_id)
        safe_preview = json.dumps(preview)
        safe_results = json.dumps(results_json)
        script = "<script>"
        script += 'localStorage.setItem("pdf2zh_last_task_id", ' + safe_tid + ');'
        script += 'localStorage.setItem("pdf2zh_last_preview_path", ' + safe_preview + ');'
        script += 'localStorage.setItem("pdf2zh_last_results", ' + safe_results + ');'
        script += 'window.__pdf2zh_last_task_id = ' + safe_tid + ';'
        script += 'window.__pdf2zh_last_preview = ' + safe_preview + ';'
        script += "</script>"
        return script
    except Exception:
        return ""


def on_download_single(current_task_id: str):
    if not current_task_id:
        return None
    ts = get_runtime_service().get_task_state(current_task_id)
    if ts is None or not ts.result_files:
        return None
    sel = getattr(ts, "selected_file", None)
    for rf in ts.result_files:
        if rf["name"] == sel and os.path.exists(rf["path"]):
            return rf["path"]
    if ts.result_files and os.path.exists(ts.result_files[0]["path"]):
        return ts.result_files[0]["path"]
    return None


def on_download_all(current_task_id: str):
    if not current_task_id:
        return None
    ts = get_runtime_service().get_task_state(current_task_id)
    if ts is None:
        return None
    zp = getattr(ts, "result_zip", None)
    if zp and os.path.exists(zp):
        return zp
    return None


def _collect_logs(max_lines: int = 50) -> str:
    try:
        handler = get_handler()
        handler.register_thread()
        q = handler.get_queue()
        if q is None:
            return ""
        lines = []
        while not q.empty() and len(lines) < max_lines:
            try:
                lines.append(q.get_nowait())
            except Exception:
                break
        if lines:
            return "\n".join(
                _sanitize_html(line)
                for line in lines[-max_lines:]
            )
        return ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Event-driven delta sync layer (pdf2zh-next Stage 2)
#
# Architecture:  Worker -> EventBus -> TaskStore -> (Timer transport) -> UI
#
#   * ``sync_status``  -- FULL re-render (page load / task switch).
#   * ``drain_events`` -- DELTA re-render: consumes only NEW bus events and
#     re-renders only the affected components; untouched components return a
#     no-op ``gr.update()`` so Gradio ships a minimal patch to the browser.
#
# The Timer no longer drives business logic -- it is just a transport. When a
# WebSocket/SSE transport arrives (Stage 3) this layer stays unchanged.
# ═══════════════════════════════════════════════════════════════════════════

#: Names of the 19 dynamic components, matched against ``sync_outputs`` order.
_SYNC_COMPONENTS: Tuple[str, ...] = (
    "progress_bar",
    "status_markdown",
    "translate_btn",
    "cancel_btn",
    "pause_btn",
    "resume_btn",
    "skip_btn",
    "node_overview",
    "quality_scores",
    "diagnostic_status",
    "result_selector",
    "download_single",
    "download_zip",
    "log_output",
    "pdf_preview",
    "task_id",
    "stepbar",
    "header_badge",
    "status_badge",
)


class _DeltaAccumulator:
    """Collect targeted component updates and emit the 19-tuple sync contract.

    Components not touched by the collected events stay as ``gr.update()``
    (a no-op), so Gradio ships only the true deltas to the browser.
    """

    def __init__(self) -> None:
        self._updates: Dict[str, Any] = {}

    def set(self, name: str, value: Any) -> None:
        self._updates[name] = value

    def update(self, **kwargs: Any) -> None:
        self._updates.update(kwargs)

    def touched(self) -> List[str]:
        return list(self._updates.keys())

    def as_tuple(self) -> tuple:
        return tuple(
            self._updates.get(name, gr.update()) for name in _SYNC_COMPONENTS
        )


# ── Event -> component renderers (pure functions of the event) ──────────────


def _render_task_started(acc: _DeltaAccumulator, ev: "TaskStarted") -> None:
    acc.set("task_id", ev.task_id)
    acc.set("translate_btn", gr.update(interactive=False))
    acc.set("cancel_btn", gr.update(interactive=True))
    acc.set("pause_btn", gr.update(interactive=True))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set("skip_btn", gr.update(interactive=True))
    acc.set("status_badge", gr.update(value=build_status_badge_html("running")))


def _render_stage_changed(acc: _DeltaAccumulator, ev: "TaskStageChanged") -> None:
    acc.set("stepbar", gr.update(value=build_stepbar_html(ev.stage, ev.progress)))
    acc.set("status_badge", gr.update(value=build_status_badge_html(ev.stage)))
    st = f"**Status**: `{_clean_surrogates(ev.stage)}`"
    if ev.progress:
        st += f" | **Progress**: {ev.progress:.1f}%"
    acc.set("status_markdown", gr.update(value=st))
    if ev.stage in ("completed", "cancelled", "failed"):
        acc.set("translate_btn", gr.update(interactive=True))
        acc.set("cancel_btn", gr.update(interactive=False))
        acc.set("pause_btn", gr.update(interactive=False))
        acc.set("resume_btn", gr.update(interactive=False))
        acc.set("skip_btn", gr.update(interactive=False))


def _render_progress_changed(
    acc: _DeltaAccumulator, ev: "TaskProgressChanged"
) -> None:
    acc.set(
        "progress_bar",
        gr.update(
            value=build_progress_bar_html(
                ev.stage or "running", ev.progress, ev.message or ""
            )
        ),
    )
    if ev.stage:
        acc.set(
            "status_badge",
            gr.update(value=build_status_badge_html(ev.stage, ev.message or "")),
        )


def _render_message_changed(
    acc: _DeltaAccumulator, ev: "TaskMessageChanged"
) -> None:
    acc.set("status_markdown", gr.update(value=ev.message))
    logs = _collect_logs()
    if logs:
        acc.set("log_output", gr.update(value=f"<pre class='log-output'>{logs}</pre>"))


def _render_paused(acc: _DeltaAccumulator, ev: "TaskPaused") -> None:
    acc.set("pause_btn", gr.update(interactive=False))
    acc.set("resume_btn", gr.update(interactive=True))
    acc.set("status_markdown", gr.update(value="**Status**: Paused ⏸"))


def _render_resumed(acc: _DeltaAccumulator, ev: "TaskResumed") -> None:
    acc.set("pause_btn", gr.update(interactive=True))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set("status_markdown", gr.update(value="**Status**: Running ▶️"))


def _render_skipped(acc: _DeltaAccumulator, ev: "TaskSkipped") -> None:
    acc.set("status_markdown", gr.update(value="**Status**: Skipping current file..."))


def _render_terminal(
    acc: _DeltaAccumulator, status: str, message: str = ""
) -> None:
    """Common terminal-state rendering shared by cancelled/failed/finished."""
    acc.set("translate_btn", gr.update(interactive=True))
    acc.set("cancel_btn", gr.update(interactive=False))
    acc.set("pause_btn", gr.update(interactive=False))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set("skip_btn", gr.update(interactive=False))
    acc.set("status_badge", gr.update(value=build_status_badge_html(status, message)))
    if status == "completed":
        acc.set("stepbar", gr.update(value=build_stepbar_html("completed", 100.0)))
        acc.set(
            "status_markdown",
            gr.update(value="**Status**: `completed` | **Progress**: 100.0%"),
        )
    elif status == "failed":
        acc.set(
            "progress_bar",
            gr.update(value=build_progress_bar_html("failed", 100.0, message)),
        )
        acc.set("status_markdown", gr.update(value=message or "**Status**: Failed"))
    else:
        acc.set(
            "progress_bar",
            gr.update(value=build_progress_bar_html("cancelled", 0.0, message)),
        )
        acc.set("status_markdown", gr.update(value=message or "**Status**: Cancelled"))


def _render_cancelled(acc: _DeltaAccumulator, ev: "TaskCancelled") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "cancelled", ev.message)


def _render_failed(acc: _DeltaAccumulator, ev: "TaskFailed") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "failed", ev.message)


def _render_finished(acc: _DeltaAccumulator, ev: "TaskFinished") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "completed", "Complete")


def _render_preview_ready(acc: _DeltaAccumulator, ev: "PreviewReady") -> None:
    if not ev.preview_path or not os.path.exists(ev.preview_path):
        return
    import urllib.parse as _pp

    encoded = _pp.quote(ev.preview_path)
    acc.set(
        "pdf_preview",
        gr.update(
            value=(
                '<div class="pdf-iframe-container"><iframe src="/pdf-preview/'
                + encoded
                + '" type="application/pdf"></iframe></div>'
            )
        ),
    )


def _render_file_generated(acc: _DeltaAccumulator, ev: "FileGenerated") -> None:
    if not ev.files:
        return
    choices = [r.get("name", "") for r in ev.files]
    sval = choices[0] if choices else None
    acc.set("result_selector", gr.update(choices=choices, value=sval, visible=True))
    single_path = None
    for rf in ev.files:
        p = rf.get("path", "")
        if p and os.path.exists(p):
            single_path = p
            break
    acc.set("download_single", gr.update(value=single_path, visible=bool(single_path)))
    zip_ok = bool(ev.zip_path) and os.path.exists(ev.zip_path)
    acc.set(
        "download_zip",
        gr.update(value=ev.zip_path if zip_ok else None, visible=zip_ok),
    )


def _render_diagnostics_updated(
    acc: _DeltaAccumulator, ev: "DiagnosticsUpdated"
) -> None:
    qs = ev.quality_scores or {}
    acc.set(
        "quality_scores", gr.update(value=build_diagnostic_markdown(quality_scores=qs))
    )
    acc.set(
        "diagnostic_status",
        gr.update(
            value=build_diagnostic_markdown(diagnostic_summary=ev.diagnostic_summary)
        ),
    )
    if ev.diagnostic_summary or qs:
        acc.set(
            "node_overview",
            gr.update(value=build_diagnostic_markdown(node_overview={"pages": 1})),
        )


#: Event type -> renderer dispatch table (the UI's event handlers).
_EVENT_RENDERERS: Dict[str, Callable[[_DeltaAccumulator, Any], None]] = {
    "TaskStarted": _render_task_started,
    "TaskStageChanged": _render_stage_changed,
    "TaskProgressChanged": _render_progress_changed,
    "TaskMessageChanged": _render_message_changed,
    "TaskPaused": _render_paused,
    "TaskResumed": _render_resumed,
    "TaskSkipped": _render_skipped,
    "TaskCancelled": _render_cancelled,
    "TaskFailed": _render_failed,
    "TaskFinished": _render_finished,
    "PreviewReady": _render_preview_ready,
    "FileGenerated": _render_file_generated,
    "DiagnosticsUpdated": _render_diagnostics_updated,
}


def _apply_events(acc: _DeltaAccumulator, events: List[Any]) -> None:
    """Dispatch events onto the accumulator (ordered, fault-tolerant)."""
    for ev in events:
        renderer = _EVENT_RENDERERS.get(ev.event_type)
        if renderer is None:
            continue
        try:
            renderer(acc, ev)
        except Exception:
            logger.exception("Failed to render event %s", ev.event_type)


def _resolve_current_task_id(current_task_id: str) -> str:
    """Resolve the active task id, falling back to the global store."""
    svc = get_runtime_service()
    tid = current_task_id or ""
    if not tid:
        tasks = GLOBAL_TASK_STORE.list_tasks()
        if tasks:
            tid = tasks[-1]
    else:
        ts = svc.get_task_state(tid)
        if ts is None:
            tasks = GLOBAL_TASK_STORE.list_tasks()
            tid = tasks[-1] if tasks else ""
    return tid



def _fill_full_state(acc: _DeltaAccumulator, svc, tid: str) -> None:
    """Render the entire current task state into the accumulator."""
    ts = svc.get_task_state(tid)
    if ts is None:
        return

    _clean_stale_in_flight()
    running = ts.status in (
        "pending", "parsing", "normalizing", "analyzing",
        "planning", "translating", "layouting", "rendering",
        "evaluating", "repairing",
    )
    done = ts.status == "completed"
    pct = ts.progress

    bar = build_progress_bar_html(
        ts.stage or ts.status or "running", pct, ts.message or ""
    )
    st = f"**Status**: `{_clean_surrogates(ts.status)}` | **Progress**: {pct:.1f}%"
    if ts.error_message:
        st += f"\n\nError: {ts.error_message}"
    btn_upd = gr.update(interactive=not running)

    stepbar = build_stepbar_html(ts.status, pct)
    badge = build_status_badge_html(ts.status, ts.message or "")

    qs = (ts.quality_scores or {}) if isinstance(ts.quality_scores, dict) else {}
    ds = ts.diagnostic_summary or ""
    node_ov = {}
    if ts.current_file_name:
        node_ov["pages"] = 1
    qs_md = build_diagnostic_markdown(quality_scores=qs)
    diag_md = build_diagnostic_markdown(diagnostic_summary=ds, node_overview=node_ov)
    ov = f"**Document**: {ts.current_file_name or 'N/A'}"
    if ts.file_list:
        ov += f" | **Nodes**: {len(ts.file_list)}"
    if ts.message:
        safe_msg = _sanitize_html(ts.message)
        ov += f"\n\n**Message**: {safe_msg}"

    choices = []
    sval = None
    if ts.result_files:
        choices = [r["name"] for r in ts.result_files]
        sval = ts.selected_file or choices[0]

    download_single_val = None
    download_zip_val = None
    if done and ts.result_files:
        selected_path = None
        for rf in ts.result_files:
            if rf["name"] == sval and os.path.exists(rf.get("path", "")):
                selected_path = rf["path"]
                break
        if not selected_path:
            for rf in ts.result_files:
                if os.path.exists(rf.get("path", "")):
                    selected_path = rf["path"]
                    break
        download_single_val = selected_path
    if done and ts.result_zip and os.path.exists(ts.result_zip):
        download_zip_val = ts.result_zip

    ph = "<div class='preview-empty'>等待翻译完成后显示预览</div>"
    if done:
        preview_path = None
        # honour the output-mode selector first so the preview follows the
        # dual/mono toggle, then fall back to the worker's preview path.
        if download_single_val and os.path.exists(download_single_val):
            preview_path = download_single_val
        elif ts.preview_path and os.path.exists(ts.preview_path):
            preview_path = ts.preview_path
        if preview_path is None and ts.result_files:
            for rf in ts.result_files:
                p = rf.get("path", "")
                if p and os.path.exists(p):
                    preview_path = p
                    break
        if preview_path:
            import urllib.parse as _pp

            encoded = _pp.quote(preview_path)
            ph = (
                '<div class="pdf-iframe-container"><iframe src="/pdf-preview/'
                + encoded
                + '" type="application/pdf"></iframe></div>'
            )

    if done:
        _persist_state_to_storage(tid, ts)

    lh = "<pre class='log-output'>[System ready]</pre>"
    logs = _collect_logs()
    if logs:
        lh = f"<pre class='log-output'>{logs}</pre>"

    acc.update(
        progress_bar=gr.update(value=bar),
        status_markdown=gr.update(value=st),
        translate_btn=btn_upd,
        cancel_btn=gr.update(interactive=running),
        pause_btn=gr.update(interactive=running),
        resume_btn=gr.update(interactive=running),
        skip_btn=gr.update(interactive=running),
        node_overview=gr.update(value=ov),
        quality_scores=gr.update(value=qs_md),
        diagnostic_status=gr.update(value=diag_md),
        result_selector=gr.update(
            choices=choices, value=sval, visible=bool(choices)
        ),
        download_single=gr.update(
            value=download_single_val, visible=done and bool(download_single_val)
        ),
        download_zip=gr.update(
            value=download_zip_val, visible=done and bool(download_zip_val)
        ),
        log_output=gr.update(value=lh),
        pdf_preview=gr.update(value=ph),
        task_id=tid,
        stepbar=gr.update(value=stepbar),
        header_badge=gr.update(value=badge),
        status_badge=gr.update(value=badge),
    )



def sync_status(current_task_id: str) -> tuple:
    """Full re-render of all dynamic UI components.

    Used for page load and task switches. Returns a 19-tuple matched against
    ``sync_outputs`` in ``create_gui`` (see ``_SYNC_COMPONENTS``).
    """
    svc = get_runtime_service()
    tid = _resolve_current_task_id(current_task_id)
    if not tid:
        return _idle_updates()

    acc = _DeltaAccumulator()
    _fill_full_state(acc, svc, tid)
    if not acc.touched():
        return _idle_updates()
    return acc.as_tuple()


def _parse_consumed(consumed: Any) -> Tuple[str, int]:
    """Unpack the ``(task_id, last_sequence)`` delta cursor held in gr.State."""
    if isinstance(consumed, (tuple, list)) and len(consumed) == 2:
        return str(consumed[0] or ""), int(consumed[1] or 0)
    return "", 0


def drain_events(current_task_id: str, consumed: Any) -> tuple:
    """Event-driven delta sync (Gradio Timer transport).

    ``consumed`` is a ``(task_id, last_sequence)`` pair held in a ``gr.State``.
    Each tick pulls only NEW events from the bus and re-renders only the
    components those events affect; untouched components stay ``gr.update()``
    no-ops. Returns a 2-tuple ``(updates_tuple, new_consumed_cursor)`` where
    ``updates_tuple`` is the 19-component sync contract (matched against
    ``_SYNC_COMPONENTS`` / ``sync_outputs``) and ``new_consumed_cursor`` is the
    ``(task_id, last_sequence)`` cursor to store back into ``gr.State``.

    The Gradio Timer binding cannot consume a nested tuple directly, so the
    transport layer flattens the result via ``_drain_events_flat``.
    """
    svc = get_runtime_service()
    tid = _resolve_current_task_id(current_task_id)
    prev_tid, last_seq = _parse_consumed(consumed)

    if tid and tid != prev_tid:
        # Task switch -> full state render, then replay the task's events so
        # busy/terminal flags (buttons, badges) come from the event log.
        acc = _DeltaAccumulator()
        _fill_full_state(acc, svc, tid)
        events = EVENT_BUS.events_since(tid, 0)
        _apply_events(acc, events)
        _persist_if_terminal(svc, tid, events)
        new_seq = events[-1].sequence if events else EVENT_BUS.last_sequence(tid)
        return acc.as_tuple(), (tid, new_seq)

    if not tid:
        return _DeltaAccumulator().as_tuple(), ("", 0)

    events = EVENT_BUS.events_since(tid, last_seq)
    if not events:
        return _DeltaAccumulator().as_tuple(), (tid, last_seq)

    acc = _DeltaAccumulator()
    _apply_events(acc, events)
    _persist_if_terminal(svc, tid, events)
    new_seq = events[-1].sequence
    return acc.as_tuple(), (tid, new_seq)


def _drain_events_flat(current_task_id: str, consumed: Any) -> tuple:
    """Gradio transport adapter: flatten ``drain_events`` into 20 output slots.

    Gradio binds one function return value per output component, so the
    structured ``(updates_19, cursor)`` pair is flattened into the
    ``[*sync_outputs, event_seq_state]`` tuple the Timer tick expects.
    """
    updates, new_consumed = drain_events(current_task_id, consumed)
    return (*updates, new_consumed)


def _persist_if_terminal(svc, tid: str, events: List[Any]) -> None:
    """Persist the localStorage snapshot once the task reaches a done state."""
    for ev in reversed(events):
        if isinstance(ev, (TaskFinished, TaskCancelled, TaskFailed)):
            ts = svc.get_task_state(tid)
            if ts is not None:
                _persist_state_to_storage(tid, ts)
            break


def _idle_updates() -> tuple:
    return (
        gr.update(value=build_progress_bar_html("", 0.0, "")),
        gr.update(value="**Status**: Ready"),
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(value="*Waiting for translation task...*"),
        gr.update(value="*Quality scores will appear after translation*"),
        gr.update(value="*No diagnostic analysis yet*"),
        gr.update(choices=[], value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value="<pre class='log-output'>[System ready]</pre>"),
        gr.update(value=None),
        "",
        gr.update(value=build_stepbar_html("", 0.0)),
        gr.update(value=build_status_badge_html("idle")),
        gr.update(value=build_status_badge_html("idle")),
    )


def _clean_surrogates(text: str) -> str:
    """Remove surrogate characters that crash orjson serialization in Gradio."""
    if not isinstance(text, str):
        return str(text) if text is not None else ""
    return "".join(c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in text)


def create_gui() -> gr.Blocks:
    """Create the main Gradio Block UI."""
    get_handler()

    with gr.Blocks(
        css=UI_CSS, title="PDFMathTranslate",
        theme=gr.themes.Base(primary_hue="blue", neutral_hue="slate"),
        head=SESSION_JS,
    ) as gui:

        # ---- App Shell Header (brand + global status badge + theme toggle) ----
        with gr.Row(elem_classes="app-header"):
            gr.HTML(BRAND_HTML, elem_classes="brand-block")
            header_badge = gr.HTML(
                value=build_status_badge_html("idle"),
                elem_classes="badge-block",
            )
            theme_toggle = gr.Button(
                "🌙 深色模式 / Dark",
                elem_classes="theme-toggle-btn",
            )

        # Theme hot-swap runs purely in the browser (localStorage persisted).
        theme_toggle.click(
            fn=None,
            js=TOGGLE_THEME_JS,
            inputs=None,
            outputs=None,
            queue=False,
        )

        file_state = gr.State([])
        task_id_state = gr.State("")

        # ---- StepBar pipeline rail (4 stages) ----
        stepbar_html = gr.HTML(
            value=build_stepbar_html("", 0.0),
            elem_classes="stepbar-wrap",
        )

        # ---- Main two-column layout ----
        #   Left (scale 7): upload -> progress -> preview  (pipeline)
        #   Right (scale 3): config + diagnostics            (side rail)
        with gr.Row():
            with gr.Column(scale=7):
                uc = create_upload_panel()
                pc = create_progress_panel()
                prc = create_preview_panel()

            with gr.Column(scale=3):
                cc = create_config_panel()
                dc = create_diagnostic_panel()

        t_inputs = [
            gr.State(""), gr.State("file"), uc["file_input"],
            uc["link_input"], cc["service"], cc["lang_from"],
            cc["lang_to"], cc["page_range"], gr.State(None),
            cc["threads"], cc["skip_subset_fonts"],
            cc["ignore_cache"], cc["vfont"], cc["vchar"],
            cc["mode_choice"], gr.State(""), file_state,
            cc["env0"], cc["env1"], cc["env2"], cc["prompt_env"],
            task_id_state,
        ]

        pc["translate_btn"].click(
            fn=on_translate,
            inputs=t_inputs,
            outputs=[task_id_state, pc["translate_btn"]],
        )

        pc["cancel_btn"].click(
            fn=on_cancel,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["pause_btn"].click(
            fn=on_pause,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["resume_btn"].click(
            fn=on_resume,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["skip_btn"].click(
            fn=on_skip,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )

        prc["download_btn"].click(
            fn=on_download_single,
            inputs=[task_id_state],
            outputs=[prc["download_single"]],
        )
        prc["download_all_btn"].click(
            fn=on_download_all,
            inputs=[task_id_state],
            outputs=[prc["download_zip"]],
        )

        def _on_select(tid: str, val: str) -> None:
            if tid:
                GLOBAL_TASK_STORE.update(tid, selected_file=val)

        prc["result_selector"].change(
            fn=_on_select,
            inputs=[task_id_state, prc["result_selector"]],
            outputs=[],
        )



        sync_outputs = [
            pc["progress_bar"],
            pc["status_markdown"],
            pc["translate_btn"],
            pc["cancel_btn"],
            pc["pause_btn"],
            pc["resume_btn"],
            pc["skip_btn"],
            dc["node_overview"],
            dc["quality_scores"],
            dc["diagnostic_status"],
            prc["result_selector"],
            prc["download_single"],
            prc["download_zip"],
            pc["log_output"],
            prc["pdf_preview"],
            task_id_state,
            stepbar_html,
            header_badge,
            pc["status_badge"],
        ]

        # Start the Worker -> EventBus bridge once (idempotent). The worker
        # publishes typed domain events; the Timer below is only a transport
        # that pulls NEW events each tick (delta update).
        EVENT_BRIDGE.start()

        # (task_id, last_sequence) delta cursor consumed by drain_events.
        event_seq_state = gr.State(("", 0))

        gr.Timer(value=1.5, active=True).tick(
            fn=_drain_events_flat,
            inputs=[task_id_state, event_seq_state],
            outputs=[*sync_outputs, event_seq_state],
        )

        def _on_page_load():
            svc = get_runtime_service()
            tasks = GLOBAL_TASK_STORE.list_tasks()
            if tasks:
                tid = tasks[-1]
                full = sync_status(tid)
                consumed = (tid, EVENT_BUS.last_sequence(tid))
                return (*full, consumed)
            return (*_idle_updates(), ("", 0))

        gui.load(
            fn=_on_page_load,
            inputs=None,
            outputs=[*sync_outputs, event_seq_state],
        )

    return gui


def _register_preview_route(gui: "gr.Blocks") -> None:
    """Register the /pdf-preview/ route on the LIVE FastAPI app.

    IMPORTANT: Gradio 5's Blocks.launch() recreates the FastAPI app
    (self.server_app = self.app = App.create_app(...)), so any route
    registered *before* launch is silently dropped and the preview iframe
    receives a {"detail": "Not Found"} 404 response. This helper must
    therefore be called AFTER gui.launch() has started.
    """
    try:
        from starlette.responses import FileResponse, Response

        app = gui.app
        if app is None or not hasattr(app, "get"):
            logger.warning("Could not register /pdf-preview/ route: no FastAPI app available")
            return

        @app.get("/pdf-preview/{path:path}")
        async def serve_preview(path: str):
            if os.path.exists(path):
                return FileResponse(
                    path,
                    media_type="application/pdf",
                    filename=os.path.basename(path),
                    content_disposition_type="inline",
                )
            return Response("File not found", status_code=404)
    except Exception as route_err:
        logger.warning("Could not register /pdf-preview/ route: %s", route_err)


def main() -> None:
    gui = create_gui()
    gui.queue(default_concurrency_limit=2, max_size=10, status_update_rate=0.1)
    gui.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        prevent_thread_lock=True,
    )
    # Register custom route for PDF preview iframe AFTER launch (Gradio 5
    # rebuilds the FastAPI app inside launch(), dropping pre-launch routes).
    _register_preview_route(gui)
    gui.block_thread()


if __name__ == "__main__":
    main()
