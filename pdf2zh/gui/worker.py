"""Background translation worker for Gradio UI.

Wraps RuntimeService for Gradio-specific task lifecycle management.
Supports cancellation, pause/resume, queue management, and double-click prevention.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TranslationRequest,
)
from pdf2zh.gui.state import GLOBAL_TASK_STORE, TaskState

logger = logging.getLogger(__name__)

# ── Double-click prevention lock ──────────────────────────────────────────────
_SUBMIT_LOCK = threading.Lock()
_IN_FLIGHT: Dict[str, str] = {}  # client_id -> task_id


def get_runtime_service() -> RuntimeService:
    """Get or create the process-wide RuntimeService singleton.

    委托给 services.runtime_singleton：保证 GUI / REST API / Flask backend
    共享同一个实例（Phase A 解耦的前提）。
    """
    from pdf2zh.services.runtime_singleton import (
        get_runtime_service as _shared_get_runtime_service,
    )

    return _shared_get_runtime_service()


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
    backend: str = "auto",
    ocr_mode: str = "auto",
    parse_engine: str = "auto",
    magicpdf_ocr: str = "auto",
    glossary_files: Any = None,
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

    # Resolve source path(s) -- multi-file upload yields a list
    source_paths = _resolve_source_paths(file_type, file_input, link_input, page_input)

    if not source_paths:
        with _SUBMIT_LOCK:
            _IN_FLIGHT.pop(client_id, None)
        logger.error("No source files provided")
        raise FileNotFoundError("No source file provided")

    # Build typed request
    extra_config = {
        "mode_choice": mode_choice,
        "ocr_mode": ocr_mode,
        "prompt": prompt_env,
    }
    envs = _parse_env_lines(env0, env1, env2)
    if envs:
        extra_config["envs"] = envs
    request = TranslationRequest(
        source_path=source_paths[0],
        files=source_paths,
        target_lang=lang_to,
        source_lang=lang_from,
        engine=service,
        page_range=page_range,
        vfont=vfont,
        vchar=vchar,
        threads=threads,
        skip_subset_fonts=skip_subset_fonts,
        ignore_cache=ignore_cache,
        extra_config=extra_config,
        backend=backend,
        parse_engine=parse_engine,
        magicpdf_ocr=_magicpdf_ocr_bool(magicpdf_ocr),
        magicpdf_ocr_mode=_magicpdf_ocr_mode(magicpdf_ocr),
        glossary_files=_resolve_glossary_paths(glossary_files),
    )

    # Submit via RuntimeService
    svc = get_runtime_service()
    task_id = svc.submit_task(request)

    # Register in-flight (replace placeholder)
    with _SUBMIT_LOCK:
        _IN_FLIGHT[client_id] = task_id

    return task_id


def _magicpdf_ocr_mode(value: Any) -> str:
    """归一化 MagicPDF OCR 三态值（auto/on/off）。

    兼容旧 GUI/调用方传入的 bool：``True`` → ``on``，``False`` → ``auto``。
    """
    if isinstance(value, bool):
        return "on" if value else "auto"
    v = str(value or "").strip().lower()
    return v if v in ("auto", "on", "off") else "auto"


def _magicpdf_ocr_bool(value: Any) -> bool:
    """把 MagicPDF OCR 三态值映射为历史 bool 字段（仅 ``on`` 为 True）。"""
    return _magicpdf_ocr_mode(value) == "on"


def _resolve_glossary_paths(glossary_files: Any) -> List[str]:
    """把 gr.Files 上传值规整为 CSV 绝对路径列表。

    Gradio 5 的 ``gr.Files`` 产出 ``list[str]``（临时副本路径）；兼容
    dict（含 ``name`` 键）与 Path 形态，空项静默剔除。
    """
    if not glossary_files:
        return []
    if isinstance(glossary_files, (str, Path)):
        glossary_files = [glossary_files]
    paths: List[str] = []
    for item in glossary_files:
        if isinstance(item, dict):
            item = item.get("name") or item.get("path")
        if not item:
            continue
        p = Path(str(item))
        if p.is_file():
            paths.append(str(p))
        else:
            logger.warning("Glossary upload not found, skipped: %s", item)
    return paths


def _parse_env_lines(*lines: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines into an engine env dict.

    Keys are lowercased (engine lookups are case-insensitive); blank lines,
    comments (``#``) and lines without ``=`` are skipped.
    """
    envs: Dict[str, str] = {}
    for line in lines:
        if not line or not isinstance(line, str):
            continue
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key:
            envs[key] = value.strip()
    return envs


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


def _coerce_path(item: Any) -> Optional[str]:
    """Extract a filesystem path from a Gradio 5 value (str / FileData / UploadFile)."""
    if item is None:
        return None
    if isinstance(item, str) and item.strip():
        return item
    for attr in ("path", "name"):
        if hasattr(item, attr):
            val = getattr(item, attr)
            if isinstance(val, str) and val.strip():
                return val
    return None


def _resolve_source_paths(
    file_type: str,
    file_input: Any,
    link_input: str,
    page_input: Any,
) -> List[str]:
    """Resolve ALL source file paths from various input formats.

    Handles:
      - Direct file upload (gr.File single file -> one path string)
      - Multi-file upload (gr.File file_count=\"multiple\" -> list of paths)
      - Gradio FileData / UploadFile objects (have .name / .path)
      - URL link download (saved to temp)
      - Page input (saved to temp)

    Returns a (possibly empty) list of paths; no ``os.path.exists`` filtering
    is applied so invalid files still surface as per-file failures downstream.
    """
    paths: List[str] = []
    if file_type == "file" and file_input is not None:
        raw_items: List[Any]
        if isinstance(file_input, (list, tuple)):
            raw_items = list(file_input)
        else:
            raw_items = [file_input]
        for item in raw_items:
            p = _coerce_path(item)
            if p:
                paths.append(p)

    # URL link fallback (only when no local files were provided).
    if not paths and link_input and link_input.startswith(("http://", "https://")):
        import tempfile
        import urllib.request

        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            urllib.request.urlretrieve(link_input, tmp.name)
            paths.append(tmp.name)
        except Exception as exc:
            logger.warning("Failed to download URL %s: %s", link_input, exc)
    return paths



def _resolve_source_path(
    file_type: str,
    file_input: Any,
    link_input: str,
    page_input: Any,
) -> Optional[str]:
    """Resolve the first source file path (legacy single-file entry point)."""
    paths = _resolve_source_paths(file_type, file_input, link_input, page_input)
    return paths[0] if paths else None
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
        total_files=svc_state.total_files,
        completed_files=svc_state.completed_files,
        failed_files=svc_state.failed_files,
        file_failures=list(getattr(svc_state, "file_failures", None) or []),
        result_files=svc_state.result_files,
        selected_file=svc_state.selected_file,
        result_zip=svc_state.result_zip,
        preview_path=svc_state.preview_path,
        diagnostic_summary=svc_state.diagnostic_summary,
        quality_scores=svc_state.quality_scores,
        eta=getattr(svc_state, "eta", 0.0),
        error_message=svc_state.error_message,
    )
