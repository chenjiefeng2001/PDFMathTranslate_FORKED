"""Thread-aware logging for Gradio UI progress bars.

Replaces _ThreadAwareLogHandler and _ThreadAwareStderr from Legacy gui.py.
Isolates log/progress output per thread to prevent cross-talk between
concurrent translation tasks.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections import deque
from typing import IO, Any, Deque, List, Optional

#: 详细日志 API 的全局环形缓冲上限（条）。所有线程的日志都汇入这里，
#: 前端 /gui/logs 接口与 _collect_logs 读取的正是这份日志（线程队列仅保留
#: legacy "Progress:" 语义，供旧式 per-thread 消费）。
_RING_MAX = 2000


class ThreadAwareLogHandler(logging.Handler):
    """Log handler that routes progress messages to per-thread queues.

    Messages matching "Progress:" are captured per thread ID so that
    the UI can display per-task progress without cross-contamination.
    All records are additionally appended to a global bounded ring buffer
    (``recent_lines``) so the detailed-log API / log panel can surface the
    real pipeline output instead of a placeholder.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._queues: dict[int, queue.Queue[str]] = {}
        self._ring: Deque[str] = deque(maxlen=_RING_MAX)
        self._lock = threading.Lock()

    def register_thread(self, thread_id: Optional[int] = None) -> int:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            if tid not in self._queues:
                self._queues[tid] = queue.Queue()
        return tid

    def get_queue(self, thread_id: Optional[int] = None) -> Optional[queue.Queue[str]]:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            q = self._queues.get(tid)
            if q is None:
                self._queues[tid] = queue.Queue()
                q = self._queues[tid]
            return q

    def unregister_thread(self, thread_id: Optional[int] = None) -> None:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            self._queues.pop(tid, None)

    def recent_lines(self, max_lines: int = 200) -> List[str]:
        """Most recent log lines from the global ring buffer (newest last)."""
        with self._lock:
            lines = list(self._ring)
        return lines[-max_lines:]

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        # Every record lands in the global ring (detailed log API / log panel).
        with self._lock:
            self._ring.append(msg)
        # Only route "Progress:" messages per thread
        if "Progress:" in msg:
            tid = threading.current_thread().ident or 0
            with self._lock:
                q = self._queues.get(tid)
                if q is not None:
                    q.put(msg)
                    return
        # All other messages go to fallback handler
        fallback = logging.StreamHandler(sys.stdout)
        fallback.setFormatter(self.formatter)
        fallback.emit(record)


class ThreadAwareStderr:
    """Intercepts stderr output (e.g. tqdm progress bars) per thread.

    tqdm writes progress bar updates to stderr. This class captures
    those writes and routes them to per-thread queues so that the
    Gradio UI can render per-task progress bars independently.
    """

    def __init__(self, original_stderr: IO[str]) -> None:
        self._original = original_stderr
        self._queues: dict[int, queue.Queue[str]] = {}
        self._lock = threading.Lock()

    def register_thread(self, thread_id: Optional[int] = None) -> int:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            if tid not in self._queues:
                self._queues[tid] = queue.Queue()
        return tid

    def unregister_thread(self, thread_id: Optional[int] = None) -> None:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            self._queues.pop(tid, None)

    def get_queue(self, thread_id: Optional[int] = None) -> Optional[queue.Queue[str]]:
        tid = thread_id or threading.current_thread().ident or 0
        with self._lock:
            q = self._queues.get(tid)
            if q is None:
                self._queues[tid] = queue.Queue()
                q = self._queues[tid]
            return q

    def write(self, text: str) -> None:
        tid = threading.current_thread().ident or 0
        # Check if this thread's tqdm output should be captured
        with self._lock:
            q = self._queues.get(tid)
        if q is not None and ("%|" in text or text.strip()):
            q.put(text)
        else:
            self._original.write(text)

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


# Singleton instances
_thread_aware_handler: Optional[ThreadAwareLogHandler] = None
_thread_aware_stderr: Optional[ThreadAwareStderr] = None


def get_handler() -> ThreadAwareLogHandler:
    global _thread_aware_handler
    if _thread_aware_handler is None:
        _thread_aware_handler = ThreadAwareLogHandler()
        _thread_aware_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        root = logging.getLogger()
        # The detailed-log ring only fills if records actually reach the root
        # logger: Python's default root level is WARNING, so every INFO record
        # (pipeline stages, "[task=...] ...") would be dropped upstream and the
        # log panel / /gui/logs would stay empty. Keep the level at INFO unless
        # the application configured something more verbose already.
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        if _thread_aware_handler not in root.handlers:
            root.addHandler(_thread_aware_handler)
    return _thread_aware_handler


def get_stderr_capture() -> ThreadAwareStderr:
    global _thread_aware_stderr
    if _thread_aware_stderr is None:
        _thread_aware_stderr = ThreadAwareStderr(sys.stderr)
    return _thread_aware_stderr
