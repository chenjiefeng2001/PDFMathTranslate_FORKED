"""Type-safe task state management for the Gradio UI.

Replaces the bare dict (20+ fields) and 13-tuple update pattern
from Legacy gui.py with a dataclass-based state model.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Concurrency ──────────────────────────────────────────────────────────────

MAX_CONCURRENCY = 4

# ── Global Task Store ────────────────────────────────────────────────────────


@dataclass
class TaskState:
    """Type-safe task state for Gradio UI.

    Each field is updated atomically via GLOBAL_TASK_STORE dict.
    """

    task_id: str = ""
    status: str = "idle"
    progress: float = 0.0
    message: str = ""
    stage: str = ""
    file_progress: float = 0.0
    total_progress: float = 0.0
    current_file_name: str = ""
    file_list: List[str] = field(default_factory=list)
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    file_failures: List[Dict[str, str]] = field(default_factory=list)
    result_files: List[Dict[str, str]] = field(default_factory=list)
    selected_file: Optional[str] = None
    result_zip: Optional[str] = None
    preview_path: Optional[str] = None
    diagnostic_summary: Optional[str] = None
    quality_scores: Optional[Dict[str, float]] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Cancel/pause/skip signals
    cancelled: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=lambda: threading.Event())
    skip: threading.Event = field(default_factory=threading.Event)
    queue_position: int = 0

    # Hash for change detection
    last_sync_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "stage": self.stage,
            "file_progress": self.file_progress,
            "total_progress": self.total_progress,
            "current_file_name": self.current_file_name,
            "file_list": self.file_list,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "failed_files": self.failed_files,
            "file_failures": self.file_failures,
            "result_files": self.result_files,
            "selected_file": self.selected_file,
            "result_zip": self.result_zip,
            "preview_path": self.preview_path,
            "diagnostic_summary": self.diagnostic_summary,
            "quality_scores": self.quality_scores,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# Thread-safe global store
_lock = threading.Lock()
_GLOBAL_TASK_STORE: Dict[str, TaskState] = {}
_GLOBAL_TASK_QUEUE: List[str] = []


class GlobalTaskStore:
    """Thread-safe accessor for the global task store."""

    @staticmethod
    def get(task_id: str) -> Optional[TaskState]:
        with _lock:
            return _GLOBAL_TASK_STORE.get(task_id)

    @staticmethod
    def set(task_id: str, state: TaskState) -> None:
        with _lock:
            _GLOBAL_TASK_STORE[task_id] = state

    @staticmethod
    def remove(task_id: str) -> None:
        with _lock:
            _GLOBAL_TASK_STORE.pop(task_id, None)
            if task_id in _GLOBAL_TASK_QUEUE:
                _GLOBAL_TASK_QUEUE.remove(task_id)

    @staticmethod
    def list_tasks() -> List[str]:
        with _lock:
            return list(_GLOBAL_TASK_STORE.keys())

    @staticmethod
    def update(task_id: str, **kwargs: Any) -> Optional[TaskState]:
        with _lock:
            state = _GLOBAL_TASK_STORE.get(task_id)
            if state is None:
                return None
            for k, v in kwargs.items():
                if hasattr(state, k):
                    setattr(state, k, v)
            state.updated_at = time.time()
            return state

    @staticmethod
    def queue_push(task_id: str) -> None:
        with _lock:
            if task_id not in _GLOBAL_TASK_QUEUE:
                _GLOBAL_TASK_QUEUE.append(task_id)

    @staticmethod
    def queue_remove(task_id: str) -> None:
        with _lock:
            if task_id in _GLOBAL_TASK_QUEUE:
                _GLOBAL_TASK_QUEUE.remove(task_id)

    @staticmethod
    def queue_position(task_id: str) -> int:
        with _lock:
            try:
                return _GLOBAL_TASK_QUEUE.index(task_id)
            except ValueError:
                return -1

    @staticmethod
    def queue_clear() -> None:
        with _lock:
            _GLOBAL_TASK_QUEUE.clear()


GLOBAL_TASK_STORE = GlobalTaskStore()
