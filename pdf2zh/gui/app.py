"""Main Gradio Web UI entry point for pdf2zh.

Usage:
    python -m pdf2zh.gui.app
"""

from __future__ import annotations

# --- 优先加载 PyTorch 解决 Windows 环境下 DLL 顺序冲突（副作用导入，勿删）---
try:
    import torch  # noqa: F401
except Exception:
    pass
# -----------------------------------------------------

import os
import logging
from typing import Any, Callable, Dict, List, Tuple

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
    build_healing_markdown,
)
from pdf2zh.gui.styles import (
    UI_CSS,
    SESSION_JS,
    TOGGLE_THEME_JS,
    build_status_badge_html,
)
from pdf2zh.gui.i18n import B, stage_text
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
    NoticeEmitted,
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
from pdf2zh.gui.notifier import EVENT_NOTIFIER

logger = logging.getLogger(__name__)

#: Statuses considered "actively running" (guards cancel/pause/resume/skip).
_RUNNING_STATUSES: Tuple[str, ...] = (
    "pending",
    "parsing",
    "normalizing",
    "analyzing",
    "planning",
    "translating",
    "layouting",
    "rendering",
    "evaluating",
    "repairing",
)

#: Last known task id (needed so a declined cancel dialog can restore the
#: state value instead of clobbering it with a sentinel).
_last_task_id: str = ""

#: Browser-side confirmation for the destructive Cancel action. Returns a
#: ``__skip__`` sentinel when the user declines so the backend no-ops.
CANCEL_CONFIRM_JS = (
    "(tid) => {"
    " if (!tid) return tid;"
    " if (window.confirm('确定停止当前翻译任务？/ Cancel the current task?')) { return tid; }"
    " return '__skip__';"
    "}"
)

#: Branding block rendered in the App Shell header.
BRAND_HTML = (
    "<div class='app-brand'>"
    "<span class='brand-logo'>PDF</span>"
    f"<div><h1 class='brand-title'>{B('brand_title')}</h1>"
    f"<p class='brand-subtitle'>{B('brand_subtitle')}</p></div>"
    "</div>"
)


def _sanitize_html(text: str) -> str:
    """Sanitize text for safe HTML embedding. Removes surrogate characters."""
    cleaned = "".join(
        c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in text
    )
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def on_translate(
    client_id: str,
    file_type: str,
    file_input: Any,
    link_input: str,
    service: str,
    lang_from: str,
    lang_to: str,
    page_range: str,
    page_input: Any,
    threads: int,
    skip_subset_fonts: bool,
    ignore_cache: bool,
    vfont: str,
    vchar: str,
    mode_choice: str,
    recaptcha_response: str,
    fl_state: List[str],
    env0: str,
    env1: str,
    env2: str,
    prompt_env: str,
    backend: str,
    ocr_mode: str,
    parse_engine: str,
    magicpdf_ocr: str,
    glossary_files: Any,
    current_task_id: str,
    last_inputs: Any = None,
) -> tuple:
    """Handle translate button with double-click prevention.

    The full input set is snapshotted into ``last_inputs`` (a ``gr.State``)
    so the Retry button can resubmit the same request after a failure.
    Returns ``(task_id, translate_btn_update, last_inputs)``.
    """
    # Resolve effective client_id (supports both Gradio State and JS-side global)
    import __main__ as _main_mod

    effective_cid = (
        client_id if client_id else getattr(_main_mod, "__pdf2zh_client_id", "")
    )
    if not effective_cid:
        effective_cid = "client_default"
    if current_task_id:
        svc = get_runtime_service()
        ts = svc.get_task_state(current_task_id)
        if ts and ts.status in _RUNNING_STATUSES:
            return current_task_id, gr.update(), last_inputs
    try:
        task_id = submit_translation_task(
            client_id=effective_cid,
            file_type=file_type,
            file_input=file_input,
            link_input=link_input,
            service=service,
            lang_from=lang_from,
            lang_to=lang_to,
            page_range=page_range,
            page_input=page_input,
            threads=threads,
            skip_subset_fonts=skip_subset_fonts,
            ignore_cache=ignore_cache,
            vfont=vfont,
            vchar=vchar,
            mode_choice=mode_choice,
            recaptcha_response=recaptcha_response,
            fl_state=fl_state,
            env0=env0,
            env1=env1,
            env2=env2,
            prompt_env=prompt_env,
            backend=backend,
            ocr_mode=ocr_mode,
            parse_engine=parse_engine,
            magicpdf_ocr=magicpdf_ocr,
            glossary_files=glossary_files,
        )
    except Exception as exc:
        logger.error("Failed to submit task: %s", exc)
        return "", gr.update(), last_inputs
    global _last_task_id
    _last_task_id = task_id
    # 复位 Ctrl+C 旗标：新任务不应被上一次 Ctrl+C 的取消请求立即短路。
    from pdf2zh.parallel.interrupt import reset_interrupt_flag

    reset_interrupt_flag()
    saved = (
        client_id,
        file_type,
        file_input,
        link_input,
        service,
        lang_from,
        lang_to,
        page_range,
        page_input,
        threads,
        skip_subset_fonts,
        ignore_cache,
        vfont,
        vchar,
        mode_choice,
        recaptcha_response,
        fl_state,
        env0,
        env1,
        env2,
        prompt_env,
        backend,
        ocr_mode,
        parse_engine,
        magicpdf_ocr,
        glossary_files,
    )
    return task_id, gr.update(interactive=False), saved


def on_retry(last_inputs: Any) -> tuple:
    """Resubmit the last translation request after a failure."""
    # 26 元素快照为当前版本；<26 元素来自旧版会话（缺少 parse_engine /
    # magicpdf_ocr / glossary_files 等），视为无效。
    if not isinstance(last_inputs, tuple) or len(last_inputs) < 26:
        return "", gr.update(visible=False)
    result = on_translate(*last_inputs, "", None)
    return result[0], gr.update(visible=True, interactive=False)


def on_cancel(current_task_id: str) -> str:
    """Cancel the running task and announce it on the EventBus.

    Handles the ``__skip__`` sentinel produced when the user declines the
    browser confirm dialog (state value is restored, nothing is cancelled).
    """
    global _last_task_id
    if current_task_id == "__skip__":
        return _last_task_id
    tid = current_task_id or _last_task_id
    if not tid:
        return ""
    _last_task_id = tid
    get_runtime_service().cancel_task(tid)
    EVENT_BUS.publish(TaskCancelled(task_id=tid, message="Cancelled by user"))
    return tid


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
            [
                {"name": r["name"], "path": r["path"]}
                for r in (getattr(ts, "result_files", None) or [])
            ],
            ensure_ascii=False,
        )
        preview = getattr(ts, "preview_path", None) or ""
        safe_tid = json.dumps(task_id)
        safe_preview = json.dumps(preview)
        safe_results = json.dumps(results_json)
        script = "<script>"
        script += 'localStorage.setItem("pdf2zh_last_task_id", ' + safe_tid + ");"
        script += (
            'localStorage.setItem("pdf2zh_last_preview_path", ' + safe_preview + ");"
        )
        script += 'localStorage.setItem("pdf2zh_last_results", ' + safe_results + ");"
        script += "window.__pdf2zh_last_task_id = " + safe_tid + ";"
        script += "window.__pdf2zh_last_preview = " + safe_preview + ";"
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


def _collect_logs(max_lines: int = 50, task_id: str = "") -> str:
    """Collect recent log lines from the global ring buffer (sanitized).

    The detailed-log channel is the ``ThreadAwareLogHandler`` ring: every
    record (from any thread) lands there, so the panel shows the real
    pipeline output. When ``task_id`` is known, lines carrying a
    ``task=<id>`` marker are preferred (they belong to the active task);
    otherwise the newest global lines are returned.
    """
    try:
        handler = get_handler()
        lines = handler.recent_lines(max_lines=max(100, max_lines * 4))
        if not lines:
            return ""
        if task_id:
            tagged = [ln for ln in lines if f"task={task_id}" in ln]
            if len(tagged) >= 2:
                lines = tagged
        return "\n".join(_sanitize_html(line) for line in lines[-max_lines:])
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Event-driven delta sync layer (pdf2zh-next Stage 2/3)
#
# Architecture:  Worker -> EventBus -> EventNotifier --SSE--> EventSource
#                                                   |
#                                                   v (wake signal)
#                            hidden "sync-trigger" -> drain_events (delta sync)
#
#   * ``sync_status``  -- FULL re-render (page load / task switch).
#   * ``drain_events`` -- DELTA re-render: consumes only NEW bus events and
#     re-renders only the affected components; untouched components return a
#     no-op ``gr.update()`` so Gradio ships a minimal patch to the browser.
#
# The browser is woken by server push (SSE, Stage 3) instead of a polling
# Timer; if the stream drops, the browser-side fallback in SESSION_JS polls
# at low frequency until EventSource reconnects. The backend never polls.
# ═══════════════════════════════════════════════════════════════════════════

#: Names of the 20 dynamic components, matched against ``sync_outputs`` order.
_SYNC_COMPONENTS: Tuple[str, ...] = (
    "progress_bar",
    "status_markdown",
    "translate_btn",
    "cancel_btn",
    "pause_btn",
    "resume_btn",
    "skip_btn",
    "retry_btn",
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
        return tuple(self._updates.get(name, gr.update()) for name in _SYNC_COMPONENTS)


# ── Event -> component renderers (pure functions of the event) ──────────────


def _render_task_started(acc: _DeltaAccumulator, ev: "TaskStarted") -> None:
    acc.set("task_id", ev.task_id)
    acc.set("translate_btn", gr.update(interactive=False))
    acc.set("cancel_btn", gr.update(interactive=True))
    acc.set("pause_btn", gr.update(interactive=True))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set("skip_btn", gr.update(interactive=True))
    acc.set("retry_btn", gr.update(visible=False))
    acc.set("status_badge", gr.update(value=build_status_badge_html("running")))
    svc = get_runtime_service()
    _render_overview(acc, svc, ev.task_id)
    _render_logs(acc, ev.task_id)


def _render_logs(acc: _DeltaAccumulator, task_id: str) -> None:
    """Refresh the detailed-log panel from the global ring buffer."""
    logs = _collect_logs(task_id=task_id)
    if logs:
        acc.set("log_output", gr.update(value=f"<pre class='log-output'>{logs}</pre>"))


def _render_overview(acc: _DeltaAccumulator, svc, tid: str) -> None:
    """Refresh the document-overview panel from the current task state."""
    ts = svc.get_task_state(tid) if tid else None
    if ts is None:
        return
    acc.set("node_overview", gr.update(value=_build_overview_markdown(ts)))


def _build_overview_markdown(ts) -> str:
    """Render the document-overview markdown from a task state."""
    ov = f"**{B('label_document')}**: {ts.current_file_name or B('label_n_a')}"
    if ts.file_list:
        ov += f" | **{B('label_files')}**: {len(ts.file_list)}"
    no = ts.node_overview if isinstance(ts.node_overview, dict) else None
    if no:
        parts = []
        if no.get("pages"):
            parts.append(f"{B('diag_node_heading')} {no.get('pages')}")
        for key, label in (
            ("paragraphs", "diag_paragraphs"),
            ("headings", "diag_headings"),
            ("figures", "diag_figures"),
            ("formulas", "diag_formulas"),
        ):
            if no.get(key):
                parts.append(f"{B(label)} {no.get(key)}")
        if parts:
            ov += f"\n\n**{B('diag_graph')}**: {' | '.join(parts)}"
    if ts.message:
        safe_msg = _sanitize_html(ts.message)
        ov += f"\n\n**{B('label_message')}**: {safe_msg}"
    return ov


def _render_stage_changed(acc: _DeltaAccumulator, ev: "TaskStageChanged") -> None:
    acc.set("stepbar", gr.update(value=build_stepbar_html(ev.stage, ev.progress)))
    acc.set("status_badge", gr.update(value=build_status_badge_html(ev.stage)))
    st = f"**{B('label_status')}**: {stage_text(ev.stage)}"
    if ev.progress:
        st += f" | **{B('label_progress')}**: {ev.progress:.1f}%"
    acc.set("status_markdown", gr.update(value=st))
    _render_logs(acc, ev.task_id)
    if ev.stage in ("completed", "cancelled", "failed"):
        acc.set("translate_btn", gr.update(interactive=True))
        acc.set("cancel_btn", gr.update(interactive=False))
        acc.set("pause_btn", gr.update(interactive=False))
        acc.set("resume_btn", gr.update(interactive=False))
        acc.set("skip_btn", gr.update(interactive=False))
        if ev.stage == "failed":
            acc.set("retry_btn", gr.update(visible=True, interactive=True))
        else:
            acc.set("retry_btn", gr.update(visible=False))


def _render_progress_changed(acc: _DeltaAccumulator, ev: "TaskProgressChanged") -> None:
    # Only the progress bar is re-rendered here. The status badge is tied to
    # the *stage* (see _render_stage_changed); re-setting it on every progress
    # event would replace its DOM mid-animation (pulse-dot), causing visible
    # flicker at high event rates.
    acc.set(
        "progress_bar",
        gr.update(
            value=build_progress_bar_html(
                ev.stage or "running",
                ev.progress,
                ev.message or "",
                getattr(ev, "eta", 0.0),
            )
        ),
    )
    # Keep the status text in the same labelled format as the full render so
    # live progress does not drift from the page-load/refresh snapshot.
    acc.set(
        "status_markdown",
        gr.update(
            value=(
                f"**{B('label_status')}**: {stage_text(ev.stage or 'running')} | "
                f"**{B('label_progress')}**: {ev.progress:.1f}%"
            )
            + _active_notice_markdown(ev.task_id)
        ),
    )
    _render_logs(acc, ev.task_id)


def _render_message_changed(acc: _DeltaAccumulator, ev: "TaskMessageChanged") -> None:
    # Keep the labelled status/progress prefix so the live status text matches
    # the full re-render (a bare message would silently drop the 状态/进度 label).
    svc = get_runtime_service()
    ts = svc.get_task_state(ev.task_id) if ev.task_id else None
    st = (
        f"**{B('label_status')}**: "
        f"{stage_text(ts.status) if ts is not None and ts.status else B('status_running')}"
    )
    if ts is not None and ts.progress:
        st += f" | **{B('label_progress')}**: {ts.progress:.1f}%"
    if ev.message:
        st += f"\n\n{ev.message}"
    st += _active_notice_markdown(ev.task_id)
    acc.set("status_markdown", gr.update(value=st))
    _render_logs(acc, ev.task_id)


#: Per-task active notices (task_id -> markdown line). Set by the notice
#: renderer; appended to the status text by the progress/message renderers so
#: a degradation notice survives continued progress updates.
_ACTIVE_NOTICES: Dict[str, str] = {}


def _active_notice_markdown(task_id: str) -> str:
    """Return the markdown suffix for an active notice ('' if none)."""
    line = _ACTIVE_NOTICES.get(task_id)
    return f"\n\n{line}" if line else ""


def _render_notice_emitted(acc: _DeltaAccumulator, ev: "NoticeEmitted") -> None:
    """Surface a structured runtime notice in the status area (not the bar).

    Notices are process-health facts (backend degradation, fallback, cache
    migration, ...) and are deliberately rendered outside the progress
    channel -- exactly the message that was previously dropped by the
    monotonic progress clamp (0% + message under the last progress value).
    """
    if not ev.task_id:
        return
    icon = {"error": "🚫", "warning": "⚠️"}.get(ev.severity, "ℹ️")
    line = f"{icon} **{ev.title}**"
    if ev.detail:
        line += f" — {ev.detail}"
    if ev.tip:
        line += f"（{ev.tip}）"
    _ACTIVE_NOTICES[ev.task_id] = line
    stage = ""
    svc = get_runtime_service()
    ts = svc.get_task_state(ev.task_id) if ev.task_id else None
    if ts is not None:
        stage = ts.status or ""
    acc.set(
        "status_badge",
        gr.update(value=build_status_badge_html(stage or "running", ev.title[:24])),
    )
    acc.set(
        "status_markdown",
        gr.update(value=line + _active_notice_markdown(ev.task_id)),
    )
    _render_logs(acc, ev.task_id)


def _render_paused(acc: _DeltaAccumulator, ev: "TaskPaused") -> None:
    acc.set("pause_btn", gr.update(interactive=False))
    acc.set("resume_btn", gr.update(interactive=True))
    acc.set(
        "status_markdown",
        gr.update(value=f"**{B('label_status')}**: {B('status_paused')}"),
    )


def _render_resumed(acc: _DeltaAccumulator, ev: "TaskResumed") -> None:
    acc.set("pause_btn", gr.update(interactive=True))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set(
        "status_markdown",
        gr.update(value=f"**{B('label_status')}**: {B('status_running')}"),
    )


def _render_skipped(acc: _DeltaAccumulator, ev: "TaskSkipped") -> None:
    acc.set(
        "status_markdown",
        gr.update(value=f"**{B('label_status')}**: {B('status_skipping')}"),
    )


def _render_terminal(
    acc: _DeltaAccumulator, status: str, message: str = "", task_id: str = ""
) -> None:
    """Common terminal-state rendering shared by cancelled/failed/finished."""
    acc.set("translate_btn", gr.update(interactive=True))
    acc.set("cancel_btn", gr.update(interactive=False))
    acc.set("pause_btn", gr.update(interactive=False))
    acc.set("resume_btn", gr.update(interactive=False))
    acc.set("skip_btn", gr.update(interactive=False))
    acc.set(
        "retry_btn",
        gr.update(visible=status == "failed", interactive=status == "failed"),
    )
    acc.set("status_badge", gr.update(value=build_status_badge_html(status, message)))
    if task_id:
        svc = get_runtime_service()
        _render_overview(acc, svc, task_id)
        _render_logs(acc, task_id)
    if status == "completed":
        acc.set("stepbar", gr.update(value=build_stepbar_html("completed", 100.0)))
        acc.set(
            "status_markdown",
            gr.update(
                value=(
                    f"**{B('label_status')}**: {B('status_completed')} | "
                    f"**{B('label_progress')}**: 100.0%"
                )
            ),
        )
    elif status == "failed":
        acc.set(
            "progress_bar",
            gr.update(value=build_progress_bar_html("failed", 100.0, message)),
        )
        hint = message or f"{B('label_error')}: -"
        acc.set(
            "status_markdown",
            gr.update(
                value=(
                    f"**{B('label_status')}**: {B('status_failed')}\n\n"
                    f"**{B('label_error')}**: {hint}\n\n{B('retry_hint')}"
                )
            ),
        )
    else:
        acc.set(
            "progress_bar",
            gr.update(value=build_progress_bar_html("cancelled", 0.0, message)),
        )
        acc.set(
            "status_markdown",
            gr.update(
                value=message or f"**{B('label_status')}**: {B('status_cancelled')}"
            ),
        )


def _render_cancelled(acc: _DeltaAccumulator, ev: "TaskCancelled") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "cancelled", ev.message, ev.task_id)


def _render_failed(acc: _DeltaAccumulator, ev: "TaskFailed") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "failed", ev.message, ev.task_id)


def _render_finished(acc: _DeltaAccumulator, ev: "TaskFinished") -> None:
    acc.set("task_id", ev.task_id)
    _render_terminal(acc, "completed", "Complete", ev.task_id)


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
                + f'" type="application/pdf" title="{B("preview_title")}"></iframe></div>'
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
            value=build_healing_markdown(
                diagnostic_report=ev.diagnostic_report,
                heal_status=ev.heal_status,
                repair_records=ev.repair_records,
                confidence_stats=ev.confidence_stats,
                diagnostic_summary=ev.diagnostic_summary,
            )
        ),
    )
    if ev.node_overview:
        acc.set(
            "node_overview",
            gr.update(
                value=build_diagnostic_markdown(
                    node_overview=dict(ev.node_overview),
                    diagnostic_summary=ev.diagnostic_summary,
                )
            ),
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
    "NoticeEmitted": _render_notice_emitted,
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
    """Resolve the active task id, falling back to the global store.

    The runtime service is the single source of truth: submits create task
    entries there but *not* in ``GLOBAL_TASK_STORE``, so a page refresh /
    new session must recover the latest task from the service store, otherwise
    the UI resets to idle while the backend task keeps running (desync).
    """
    svc = get_runtime_service()
    tid = current_task_id or ""

    def _latest() -> str:
        ids = GLOBAL_TASK_STORE.list_tasks()
        if not ids:
            ids = svc.list_task_ids()
        return ids[-1] if ids else ""

    if not tid:
        tid = _latest()
    else:
        ts = svc.get_task_state(tid)
        if ts is None:
            tid = _latest()
    return tid


def _fill_full_state(acc: _DeltaAccumulator, svc, tid: str) -> None:
    """Render the entire current task state into the accumulator."""
    ts = svc.get_task_state(tid)
    if ts is None:
        return

    _clean_stale_in_flight()
    running = ts.status in _RUNNING_STATUSES
    done = ts.status == "completed"
    pct = ts.progress

    bar = build_progress_bar_html(
        ts.stage or ts.status or "running",
        pct,
        ts.message or "",
        getattr(ts, "eta", 0.0),
    )
    st = (
        f"**{B('label_status')}**: {stage_text(ts.status)} | "
        f"**{B('label_progress')}**: {pct:.1f}%"
    )
    if ts.error_message:
        st += f"\n\n**{B('label_error')}**: {ts.error_message}"
    btn_upd = gr.update(interactive=not running)

    stepbar = build_stepbar_html(ts.status, pct)
    badge = build_status_badge_html(ts.status, ts.message or "")

    qs = (ts.quality_scores or {}) if isinstance(ts.quality_scores, dict) else {}
    ds = ts.diagnostic_summary or ""
    qs_md = build_diagnostic_markdown(quality_scores=qs)
    diag_md = build_healing_markdown(
        diagnostic_report=ts.diagnostic_report,
        heal_status=ts.heal_status,
        repair_records=ts.repair_records,
        confidence_stats=ts.confidence_stats,
        diagnostic_summary=ds,
    )
    ov = _build_overview_markdown(ts)

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

    ph = f"<div class='preview-empty'>{B('preview_empty')}</div>"
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
                + f'" type="application/pdf" title="{B("preview_title")}"></iframe></div>'
            )

    if done:
        _persist_state_to_storage(tid, ts)

    lh = f"<pre class='log-output'>{B('progress_log_idle')}</pre>"
    logs = _collect_logs(task_id=tid)
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
        retry_btn=gr.update(
            visible=done and ts.status == "failed",
            interactive=done and ts.status == "failed",
        ),
        node_overview=gr.update(value=ov),
        quality_scores=gr.update(value=qs_md),
        diagnostic_status=gr.update(value=diag_md),
        result_selector=gr.update(choices=choices, value=sval, visible=bool(choices)),
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
    """Event-driven delta sync (SSE wake transport).

    ``consumed`` is a ``(task_id, last_sequence)`` pair held in a ``gr.State``.
    Each wake pulls only NEW events from the bus and re-renders only the
    components those events affect; untouched components stay ``gr.update()``
    no-ops. Returns a 2-tuple ``(updates_tuple, new_consumed_cursor)`` where
    ``updates_tuple`` is the 20-component sync contract (matched against
    ``_SYNC_COMPONENTS`` / ``sync_outputs``) and ``new_consumed_cursor`` is the
    ``(task_id, last_sequence)`` cursor to store back into ``gr.State``.

    The Gradio dependency binding cannot consume a nested tuple directly, so
    the transport layer flattens the result via ``_drain_events_flat``.
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
    structured ``(updates_20, cursor)`` pair is flattened into the
    ``[*sync_outputs, event_seq_state]`` tuple the sync dependency expects.
    """
    updates, new_consumed = drain_events(current_task_id, consumed)
    return (*updates, new_consumed)


def on_select_file(tid: str, val: str) -> None:
    """Record the output-file selection for the active task.

    The runtime store is the source of truth for downloads and the full
    re-render; GLOBAL_TASK_STORE is only a legacy mirror.
    """
    if tid:
        svc = get_runtime_service()
        svc.update_task_state(tid, selected_file=val)
        GLOBAL_TASK_STORE.update(tid, selected_file=val)


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
        gr.update(value=f"**{B('label_status')}**: {B('status_ready')}"),
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(visible=False),
        gr.update(value=f"*{B('waiting_task')}*"),
        gr.update(value=f"*{B('idle_quality')}*"),
        gr.update(value=f"*{B('idle_diag')}*"),
        gr.update(choices=[], value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value=f"<pre class='log-output'>{B('progress_log_idle')}</pre>"),
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
        css=UI_CSS,
        title="PDFMathTranslate",
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
                B("theme_dark_label"),
                elem_id="theme-toggle-btn",
                elem_classes="theme-toggle-btn",
            )
            recover_gpu_btn = gr.Button(
                B("header_recover_gpu"),
                elem_id="recover-gpu-btn",
                elem_classes="theme-toggle-btn",
            )
            recover_gpu_status = gr.Markdown(
                value="",
                visible=False,
                elem_id="recover-gpu-status",
            )

        def _on_recover_gpu():
            """恢复版面分析 GPU 后端：清降级标记，下次任务初始化重新尝试 GPU。"""
            from pdf2zh.doclayout import set_backend

            try:
                set_backend("auto")
                _hint = B("recover_gpu_ok")
            except Exception as e:
                _hint = f"⚠️ {B('recover_gpu_fail')}: {str(e)[:100]}"
            return gr.update(visible=True, value=_hint)

        recover_gpu_btn.click(
            fn=_on_recover_gpu,
            inputs=None,
            outputs=[recover_gpu_status],
            queue=False,
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
        with gr.Row(elem_classes="app-main-row"):
            with gr.Column(scale=7, elem_classes="app-col-main"):
                uc = create_upload_panel()
                pc = create_progress_panel()
                prc = create_preview_panel()

            with gr.Column(scale=3, elem_classes="app-col-side"):
                cc = create_config_panel()
                dc = create_diagnostic_panel()

        t_inputs = [
            gr.State(""),
            gr.State("file"),
            uc["file_input"],
            uc["link_input"],
            cc["service"],
            cc["lang_from"],
            cc["lang_to"],
            cc["page_range"],
            gr.State(None),
            cc["threads"],
            cc["skip_subset_fonts"],
            cc["ignore_cache"],
            cc["vfont"],
            cc["vchar"],
            cc["mode_choice"],
            gr.State(""),
            file_state,
            cc["env0"],
            cc["env1"],
            cc["env2"],
            cc["prompt_env"],
            cc["backend"],
            cc["ocr_mode"],
            cc["parse_engine"],
            cc["magicpdf_ocr"],
            cc["glossary_files"],
            task_id_state,
        ]
        # Snapshot of the last submitted request (powers the Retry button).
        last_inputs_state = gr.State(None)

        pc["translate_btn"].click(
            fn=on_translate,
            inputs=[*t_inputs, last_inputs_state],
            outputs=[task_id_state, pc["translate_btn"], last_inputs_state],
        )

        # S3: 控制/下载类操作全部 queue=False 直连（不占 Gradio 队列并发槽）。
        # 翻译任务占满 default_concurrency_limit=2 时，取消/暂停/下载仍即时
        # 生效，不会因排队被 max_size 丢弃或无限等待 —— 任务卡死时 UI 可救。
        pc["retry_btn"].click(
            fn=on_retry,
            inputs=[last_inputs_state],
            outputs=[task_id_state, pc["retry_btn"]],
            queue=False,
        )

        pc["cancel_btn"].click(
            fn=on_cancel,
            js=CANCEL_CONFIRM_JS,
            inputs=[task_id_state],
            outputs=[task_id_state],
            queue=False,
        )
        pc["pause_btn"].click(
            fn=on_pause,
            inputs=[task_id_state],
            outputs=[task_id_state],
            queue=False,
        )
        pc["resume_btn"].click(
            fn=on_resume,
            inputs=[task_id_state],
            outputs=[task_id_state],
            queue=False,
        )
        pc["skip_btn"].click(
            fn=on_skip,
            inputs=[task_id_state],
            outputs=[task_id_state],
            queue=False,
        )

        prc["download_btn"].click(
            fn=on_download_single,
            inputs=[task_id_state],
            outputs=[prc["download_single"]],
            queue=False,
        )
        prc["download_all_btn"].click(
            fn=on_download_all,
            inputs=[task_id_state],
            outputs=[prc["download_zip"]],
            queue=False,
        )

        def _on_select(tid: str, val: str) -> None:
            on_select_file(tid, val)

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
            pc["retry_btn"],
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

        # Start the Worker -> EventBus -> SSE fan-out bridge once (idempotent).
        # The worker publishes typed domain events; the browser is woken by
        # server push and runs one delta sync per wake (no polling Timer).
        EVENT_BRIDGE.start()
        EVENT_NOTIFIER.start()

        # (task_id, last_sequence) delta cursor consumed by drain_events.
        event_seq_state = gr.State(("", 0))

        # Hidden wake trigger: the browser clicks this (via EventSource
        # message) once per published event; each click pulls only NEW
        # events from the bus (delta update).
        gr.Button("", visible=False, elem_id="sync-trigger").click(
            fn=_drain_events_flat,
            inputs=[task_id_state, event_seq_state],
            outputs=[*sync_outputs, event_seq_state],
            queue=False,
        )

        def _on_page_load():
            tid = _resolve_current_task_id("")
            if tid:
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
            logger.warning(
                "Could not register /pdf-preview/ route: no FastAPI app available"
            )
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


def _register_events_route(gui: "gr.Blocks") -> None:
    """Register the /gui/events SSE stream on the LIVE FastAPI app.

    Must be called AFTER gui.launch() for the same reason as
    ``_register_preview_route``: Gradio 5 rebuilds the FastAPI app inside
    launch(), dropping any routes registered earlier.
    """
    try:
        app = gui.app
        if app is None or not hasattr(app, "add_api_route"):
            logger.warning(
                "Could not register /gui/events route: no FastAPI app available"
            )
            return
        app.add_api_route(
            "/gui/events",
            endpoint=EVENT_NOTIFIER.sse_stream,
            methods=["GET"],
        )
        logger.info("Registered /gui/events SSE stream")
    except Exception as route_err:
        logger.warning("Could not register /gui/events route: %s", route_err)


def _register_logs_route(gui: "gr.Blocks") -> None:
    """Register the /gui/logs detailed-log API on the LIVE FastAPI app.

    Returns the most recent log lines from the global ring buffer as JSON
    (``{"lines": [...], "total": N}``) so any client can tail the detailed
    logs without SSE. Registered after launch like the other routes.
    """
    try:
        app = gui.app
        if app is None or not hasattr(app, "add_api_route"):
            logger.warning(
                "Could not register /gui/logs route: no FastAPI app available"
            )
            return

        def logs_endpoint(max_lines: int = 200):
            from starlette.responses import JSONResponse

            lines = get_handler().recent_lines(
                max_lines=max(1, min(int(max_lines), 1000))
            )
            return JSONResponse({"lines": lines, "total": len(lines)})

        app.add_api_route(
            "/gui/logs",
            endpoint=logs_endpoint,
            methods=["GET"],
        )
        logger.info("Registered /gui/logs detailed-log API")
    except Exception as route_err:
        logger.warning("Could not register /gui/logs route: %s", route_err)


def main() -> None:
    from pdf2zh.parallel.interrupt import install_interrupt_guard

    # Ctrl+C 语义（cancel_only=True）：第一次 Ctrl+C 只取消当前翻译任务、GUI 保持运行
    # 进入空闲（可看预览/重新提交/下载已完成任务）；第二次 Ctrl+C 才关闭应用。后台
    # 翻译线程经 coordinator 轮询/池崩短路感知旗标并落 CANCELLED，绝不进入整文档串行兜底。
    install_interrupt_guard(cancel_only=True)

    # Unified entry through entry.setup_gui() (same as CLI interactive).
    # Direct gui.launch() here hits two fixed boot bugs:
    #   1) Clash/VPN system proxies hijack the loopback startup-events handshake,
    #      turning http://localhost:7860/gradio_api/startup-events into an empty
    #      502 (entry._sanitize_loopback_proxy sets NO_PROXY before launch);
    #   2) gradio 5.20-5.35 has a transient handshake-failure race
    #      (entry._launch tolerates it after probing the live port), and a stale
    #      instance holding the port slides to a free one.
    # entry also registers /pdf-preview /gui/events /gui/logs and opens the browser.
    from pdf2zh.gui.entry import setup_gui

    setup_gui()


if __name__ == "__main__":
    from pdf2zh.pdf2zh import spawn_child_yields_to

    if spawn_child_yields_to():
        raise SystemExit(0)
    main()
