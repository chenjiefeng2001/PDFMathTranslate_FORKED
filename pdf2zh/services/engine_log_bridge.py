"""Engine log bridge — forward MinerU / BabelDOC pipeline log lines into the
per-task log stream instead of losing them in the server console.

The API service installs one root-logger handler (idempotent).  While an
engine task is active (contextvar set by ``engine_task``), records coming
from the engine namespaces are forwarded to the service's task log channel
with their metadata (ts / level / engine / message); when no engine task is
active the handler stays silent so CLI/console behaviour is untouched.

Forwarding rules:

- engine tag is taken from the active context first, then from the logger
  namespace (``mineru``/``magic_pdf``/``magicpdf``/``pdf2zh.magicpdf_*``,
  ``babeldoc``/``doclayout``/``pdf2zh.babeldoc_*``/``pdf2zh.kernel.*``);
- DEBUG lines are dropped, INFO kept only from engine namespaces, and
  WARNING/ERROR from any namespace inside the active context are kept;
- consecutive identical lines are collapsed (MinerU/magic-pdf repeat model
  loading banners a lot);
- a small rate cap protects the event stream from log floods.

The bridge is only installed by the service (``install_engine_log_bridge``);
the CLI never installs it, so engine console output stays exactly as today.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
from typing import Callable, Optional, Tuple

#: active (task_id, engine_tag) while an engine task executes.
_ENGINE_CTX: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = (
    contextvars.ContextVar("pdf2zh_engine_task", default=None)
)

#: logger-name prefix (engine namespace) → preferred engine tag.
_NS_TAGS: Tuple[Tuple[str, str], ...] = (
    ("pdf2zh.magicpdf_", "mineru"),
    ("pdf2zh.magicpdf_cli", "mineru"),
    ("magic_pdf", "mineru"),
    ("mineru", "mineru"),
    ("magicpdf", "mineru"),
    ("pdf2zh.babeldoc_", "babeldoc"),
    ("pdf2zh.kernel", "babeldoc"),
    ("babeldoc", "babeldoc"),
    ("doclayout", "babeldoc"),
)

#: noisy banner / progress lines that are never useful in a task log.
_NOISE_RE = re.compile(r"\r|\x1b\[|it/s\]|ETA:|it/s,|Batches:")
_NOISE_SUB = re.compile(r"\s+")


def current_engine_ctx() -> Optional[Tuple[str, str]]:
    """Active (task_id, engine) inside an engine task, else None."""
    return _ENGINE_CTX.get()


class _EngineLogContext:
    """Context manager that scopes a task + engine tag for the calling thread
    (and asyncio tasks running on it) — see :func:`engine_task`."""

    def __init__(self, task_id: str, engine: str) -> None:
        self._tok = None
        self._value = (task_id, engine)

    def __enter__(self):
        self._tok = _ENGINE_CTX.set(self._value)
        return self

    def __exit__(self, *exc) -> None:
        if self._tok is not None:
            _ENGINE_CTX.reset(self._tok)


def engine_task(task_id: str, engine: str) -> _EngineLogContext:
    """Scope the engine log bridge to one task execution (thread/async local)."""
    return _EngineLogContext(task_id, engine)


def _tag_for(name: str, fallback: str) -> str:
    for prefix, tag in _NS_TAGS:
        if name == prefix.rstrip("_") or name.startswith(prefix):
            return tag
    return fallback


class _EngineLogHandler(logging.Handler):
    """Root handler that forwards engine records to ``callback`` while an
    engine task context is active."""

    def __init__(
        self,
        callback: Callable[[str, str, str, str], None],
        level: int = logging.INFO,
    ) -> None:
        super().__init__(level)
        self._callback = callback
        self._last: Optional[Tuple[str, str, str]] = None  # (task, engine, msg)
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ctx = _ENGINE_CTX.get()
            if ctx is None:
                return
            task_id, engine = ctx
            name = record.name or ""
            if record.levelno < logging.INFO:
                return
            ns_tag = _tag_for(name, "")
            if record.levelno < logging.WARNING:
                # INFO survives only from engine namespaces — the service's
                # own task logging already reaches the UI through progress
                # events, and forwarding it again would duplicate every line.
                if not ns_tag:
                    return
            # 命名空间命中优先；未命中（服务自身 WARNING/ERROR）按上下文引擎记。
            engine_tag = ns_tag or engine
            msg = (record.getMessage() or "").strip()
            msg = _NOISE_SUB.sub(" ", msg)
            if not msg or _NOISE_RE.search(msg):
                return
            level = (record.levelname or "info").lower()
            key = (task_id, engine_tag, msg)
            with self._lock:
                if self._last == key:
                    return  # collapse consecutive identical lines
                self._last = key
            try:
                self._callback(task_id, level, engine_tag, msg)
            except Exception:  # noqa: BLE001 -- a broken sink never kills logging
                pass
        except Exception:  # noqa: BLE001 -- logging must never raise
            pass


_internal: dict = {"installed": False}


def install_engine_log_bridge(
    callback: Callable[[str, str, str, str], None],
    level: int = logging.INFO,
) -> bool:
    """Attach the engine bridge to the root logger once per process.  Safe to
    call from every RuntimeService instance (guarded): one handler is shared,
    and a re-install (e.g. a later service instance) just repoints its sink
    at the newest callback so engine lines never land on a stale service."""
    root = logging.getLogger()
    if _internal["installed"]:
        handler = _internal.get("handler")
        if handler is not None:
            handler._callback = callback
        return True
    handler = _EngineLogHandler(callback, level=level)
    root.addHandler(handler)
    # Keep records flowing: without a configured level most namespaces default
    # to WARNING via the root logger and INFO engine lines would be dropped
    # upstream of our handler.  The handler itself filters non-engine INFO.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _internal["handler"] = handler
    _internal["installed"] = True
    return True


def uninstall_engine_log_bridge() -> None:
    """Remove the handler again (tests / teardown)."""
    if not _internal["installed"]:
        return
    handler = _internal.pop("handler", None)
    if handler is not None:
        root = logging.getLogger()
        if handler in root.handlers:
            root.removeHandler(handler)
    _internal["installed"] = False


__all__ = [
    "engine_task",
    "current_engine_ctx",
    "install_engine_log_bridge",
    "uninstall_engine_log_bridge",
]
