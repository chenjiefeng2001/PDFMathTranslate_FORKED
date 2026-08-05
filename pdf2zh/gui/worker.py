"""Background translation worker for Gradio UI.

Wraps RuntimeService for Gradio-specific task lifecycle management.
Supports cancellation, pause/resume, queue management, and double-click prevention.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TranslationRequest,
)
from pdf2zh.gui.state import GLOBAL_TASK_STORE, TaskState

logger = logging.getLogger(__name__)

# Singleton service instance
_runtime_service: Optional[RuntimeService] = None

# ── Double-click prevention lock ──────────────────────────────────────────────
_SUBMIT_LOCK = threading.Lock()
_IN_FLIGHT: Dict[str, str] = {}  # client_id -> task_id


def get_runtime_service() -> RuntimeService:
    """Get or create the global RuntimeService singleton."""
    global _runtime_service
    if _runtime_service is None:
        _runtime_service = RuntimeService()
    return _runtime_service


# ── Stale in-flight task cleanup ───────────────────────────────────────────


def _clean_stale_in_flight(max_age: float = 300.0) -> int:
    """Remove in-flight entries whose tasks have finished or expired.

    Returns the count of remaining in-flight entries after cleanup.
    """
    import time as _time
    now = _time.time()
    svc = get_runtime_service()
    stale = []
    with _SUBMIT_LOCK:
        for cid, tid in list(_IN_FLIGHT.items()):
            if tid == "__submitting__":
                continue
            ts = svc.get_task_state(tid)
            if ts is None:
                stale.append(cid)
            elif ts.status in ("completed", "cancelled", "failed"):
                stale.append(cid)
            elif hasattr(ts, "updated_at") and (now - ts.updated_at) > max_age:
                stale.append(cid)
        for cid in stale:
            _IN_FLIGHT.pop(cid, None)
    return len(_IN_FLIGHT)


def submit_translation_task(
    client_id: str,
    file_type: str,
    file_input: Any,
    link_input: str,
    service: str,
    lang_from: str,
    lang_to: str,
    page_range: Optional[str] = None,
    page_input: Any = None,
    threads: int = 4,
    skip_subset_fonts: bool = False,
    ignore_cache: bool = False,
    vfont: str = "",
    vchar: str = "",
    mode_choice: str = "auto",
    recaptcha_response: str = "",
    fl_state: Any = None,
    env0: str = "",
    env1: str = "",
    env2: str = "",
    prompt_env: str = "",
    callback: Optional[Callable] = None,
) -> str:
    """Submit a translation task to RuntimeService.

    Includes double-click prevention via client_id-based in-flight tracking.
    Returns the task_id string for UI tracking.
    """
    # ── Double-click prevention (atomic check+reserve) ──
    svc = get_runtime_service()
    with _SUBMIT_LOCK:
        existing = _IN_FLIGHT.get(client_id)
        if existing:
            ts = svc.get_task_state(existing)
            if ts and ts.status in (
                "pending", "parsing", "normalizing", "analyzing",
                "planning", "translating", "layouting", "rendering",
                "evaluating", "repairing",
            ):
                logger.info(
                    "Double-click guard: task %s already running for client %s",
                    existing, client_id,
                )
                return existing
        _IN_FLIGHT[client_id] = "__submitting__"

    # Resolve source path
    source_path = _resolve_source_path(file_type, file_input, link_input, page_input)

    if not source_path or not os.path.exists(source_path):
        with _SUBMIT_LOCK:
            _IN_FLIGHT.pop(client_id, None)
        logger.error("Source file not found: %s", source_path)
        raise FileNotFoundError(f"Source file not found: {source_path}")

    # Build typed request
    request = TranslationRequest(
        source_path=source_path,
        target_lang=lang_to,
        source_lang=lang_from,
        engine=service,
        page_range=page_range,
        vfont=vfont,
        vchar=vchar,
        threads=threads,
        skip_subset_fonts=skip_subset_fonts,
        ignore_cache=ignore_cache,
        extra_config={
            "mode_choice": mode_choice,
            "prompt": prompt_env,
        },
    )

    # Submit via RuntimeService
    svc = get_runtime_service()
    task_id = svc.submit_task(request)

    # Register in-flight (replace placeholder)
    with _SUBMIT_LOCK:
        _IN_FLIGHT[client_id] = task_id

    return task_id


def _try_clean_in_flight(client_id: str) -> None:
    """Remove from in-flight tracking if the task is no longer active."""
    with _SUBMIT_LOCK:
        task_id = _IN_FLIGHT.get(client_id)
        if not task_id or task_id == "__submitting__":
            return
        svc = get_runtime_service()
        ts = svc.get_task_state(task_id)
        if ts is None or ts.status in ("completed", "cancelled", "failed"):
            _IN_FLIGHT.pop(client_id, None)


def check_clean_in_flight(client_id: str) -> None:
    """Remove from in-flight tracking if the task is no longer active."""
    with _SUBMIT_LOCK:
        task_id = _IN_FLIGHT.get(client_id)
        if not task_id:
            return
        svc = get_runtime_service()
        ts = svc.get_task_state(task_id)
        if ts is None or ts.status in ("completed", "cancelled", "failed"):
            _IN_FLIGHT.pop(client_id, None)


def background_translation_worker(
    task_id: str,
    request: TranslationRequest,
    log_handler: Any = None,
    stderr_capture: Any = None,
) -> None:
    """Run translation in background thread (alternative entry point).

    This is a drop-in replacement for the Legacy worker that uses
    RuntimeService internally. Primarily for backward compatibility.
    """
    svc = get_runtime_service()
    svc.submit_task(request)


def _resolve_source_path(
    file_type: str,
    file_input: Any,
    link_input: str,
    page_input: Any,
) -> Optional[str]:
    """Resolve the source file path from various input formats.

    Handles:
      - Direct file upload (gr.File → single path string)
      - Multi-file upload (gr.File file_count="multiple" → list of paths, take first)
      - Gradio UploadFile object (has .name attribute)
      - URL link download (saved to temp)
      - Page input (saved to temp)
    """
    # If file_input is a list (gr.File with file_count="multiple"), take first
    if isinstance(file_input, (list, tuple)):
        if not file_input:
            return None
        first = file_input[0]
        # First element might be a path string or an UploadFile
        if isinstance(first, str) and os.path.exists(first):
            return first
        if hasattr(first, "name"):
            path = getattr(first, "name", "")
            if path and os.path.exists(path):
                return path
        return None

    # If file_input is a string path
    if isinstance(file_input, str) and os.path.exists(file_input):
        return file_input

    # If file_input has a .name attribute (Gradio UploadFile / single file upload)
    if hasattr(file_input, "name"):
        path = getattr(file_input, "name", "")
        if path and os.path.exists(path):
            return path

    # If link_input is provided
    if link_input and link_input.startswith(("http://", "https://")):
        import tempfile
        import urllib.request
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            urllib.request.urlretrieve(link_input, tmp.name)
            return tmp.name
        except Exception as exc:
            logger.warning("Failed to download URL %s: %s", link_input, exc)
            return None

    return None


def cancel_task(task_id: str) -> bool:
    """Cancel a running translation task."""
    svc = get_runtime_service()
    return svc.cancel_task(task_id)


def get_task_state(task_id: str) -> Optional[TaskState]:
    """Get current state of a task."""
    state = GLOBAL_TASK_STORE.get(task_id)
    if state is not None:
        return state
    # Fallback to RuntimeService
    svc_state = get_runtime_service().get_task_state(task_id)
    if svc_state is None:
        return None
    return TaskState(
        task_id=svc_state.task_id,
        status=svc_state.status,
        progress=svc_state.progress,
        message=svc_state.message,
        stage=svc_state.stage,
        file_progress=svc_state.file_progress,
        total_progress=svc_state.total_progress,
        current_file_name=svc_state.current_file_name,
        file_list=svc_state.file_list,
        result_files=svc_state.result_files,
        selected_file=svc_state.selected_file,
        result_zip=svc_state.result_zip,
        preview_path=svc_state.preview_path,
        diagnostic_summary=svc_state.diagnostic_summary,
        quality_scores=svc_state.quality_scores,
        error_message=svc_state.error_message,
    )
