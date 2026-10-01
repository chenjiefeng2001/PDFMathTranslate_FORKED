"""Best-effort, Windows-tolerant file/directory deletion helpers.

Why this exists
---------------
On Windows a file cannot be removed while any process keeps it open: the OS
rejects the delete with ``PermissionError`` (typically WinError 32 "being
used by another process" or WinError 5 "access denied").  During GUI/web
translation jobs scratch PDFs are routinely held open for a moment by
antivirus scanners, the search indexer, or an in-process preview/PDF
library, so a single eager ``os.remove``/``unlink`` in cleanup code can
(a) crash the cleanup path and fail the surrounding job, and (b) leak the
temp file when the lock outlives the attempt.

These helpers make cleanup *best effort*: they never raise, retry several
times to ride out transient locks and, when ``defer=True``, keep retrying
on a background daemon thread guarded by a file-identity check so a temp
file that becomes deletable moments later still gets cleaned up without
risking deletion of a file a later job recreated at the same path.
POSIX semantics are unchanged (open files can be unlinked there anyway).

All knobs can be tuned through environment variables (parsed per call, so
no module reload is needed to apply them in tests or at runtime):

- ``PDF2ZH_CLEANUP_RETRIES``      inline delete attempts      (default 4)
- ``PDF2ZH_CLEANUP_RETRY_DELAY``  seconds between attempts    (default 0.25)
- ``PDF2ZH_CLEANUP_DEFER_RETRIES`` background retry attempts  (default 3)
- ``PDF2ZH_CLEANUP_DEFER_DELAY``  background retry base delay (default 2.0)
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
import time

logger = logging.getLogger(__name__)

#: Windows winerror codes that mean "the file is momentarily busy/locked"
#: or "access denied" -- retryable delete failures (sharing violation 32,
#: access denied 5, etc.).
_WINDOWS_RETRY_WINERRORS = frozenset({5, 32, 33, 36, 206})

#: POSIX errno values worth retrying (permission/busy/not-empty).
_RETRY_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.EPERM})


def _env_int(name: str, default: int) -> int:
    """Parse a positive-int env var; empty/garbage/non-positive -> default."""
    try:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        val = int(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a non-negative-float env var; empty/garbage -> default."""
    try:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        val = float(raw)
        return val if val >= 0.0 else default
    except (TypeError, ValueError):
        return default


def _inline_attempts_default() -> int:
    return _env_int("PDF2ZH_CLEANUP_RETRIES", 4)


def _retry_delay_default() -> float:
    return _env_float("PDF2ZH_CLEANUP_RETRY_DELAY", 0.25)


def _defer_attempts_default() -> int:
    return _env_int("PDF2ZH_CLEANUP_DEFER_RETRIES", 3)


def _defer_delay_default() -> float:
    return _env_float("PDF2ZH_CLEANUP_DEFER_DELAY", 2.0)


def _is_retryable(exc: BaseException) -> bool:
    """Would retrying this deletion error plausibly help?"""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return False  # gone already / path is not what we thought
    if os.name == "nt":
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            return winerror in _WINDOWS_RETRY_WINERRORS
        return True  # unknown OSError on Windows -> try again rather than crash
    errno_val = getattr(exc, "errno", None)
    return errno_val in _RETRY_ERRNOS or isinstance(exc, PermissionError)


def _snapshot(path: os.PathLike | str) -> tuple[int, int] | None:
    """Capture (size, mtime_ns) so deferred deletion can detect recreation."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _deferred_delete_worker(
    target: str,
    snapshot: tuple[int, int] | None,
    attempts: int,
    delay: float,
) -> None:
    """Background retry loop (runs on a daemon thread; never raises)."""
    for i in range(max(1, attempts)):
        if delay > 0:
            time.sleep(delay * (i + 1))
        # Identity guard: if the path disappeared or was replaced by a
        # different file (e.g. a new job reused the same name), stop.
        if snapshot is not None and _snapshot(target) != snapshot:
            return
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return
        except FileNotFoundError:
            return  # already gone
        except OSError as exc:
            logger.warning(
                "Deferred cleanup of %s still failing (attempt %s/%s): %s",
                target,
                i + 1,
                attempts,
                exc,
            )
            if not _is_retryable(exc):
                return


def remove_path_robust(
    path: os.PathLike | str,
    *,
    attempts: int | None = None,
    delay: float | None = None,
    defer: bool = True,
    defer_attempts: int | None = None,
    defer_delay: float | None = None,
) -> bool:
    """Delete a file or directory tree best-effort; never raises.

    Inline phase: tries ``attempts`` times (default 4) with ``delay``
    seconds (default 0.25) between tries.  A missing path counts as
    success.  If the path is still locked afterwards and ``defer`` is
    true, up to ``defer_attempts`` (default 3) further attempts run on a
    background daemon thread with growing backoff, skipping the delete if
    the file's identity (size/mtime) changed in the meantime.

    Returns ``True`` when the path is gone (or never existed) by the end
    of the inline phase; ``False`` when it is still present (a deferred
    attempt may still remove it shortly after).
    """
    target = os.fspath(path)
    if not target:
        return True
    n_attempts = max(
        1, attempts if attempts is not None else _inline_attempts_default()
    )
    sleep_delay = delay if delay is not None else _retry_delay_default()
    last_exc: BaseException | None = None

    for _ in range(n_attempts):
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            last_exc = exc
            if not _is_retryable(exc):
                break
            if sleep_delay > 0:
                time.sleep(sleep_delay)

    if os.path.exists(target):
        logger.warning(
            "Failed to remove %s after %s attempt(s) (errno=%s, winerror=%s): %s",
            target,
            n_attempts,
            getattr(last_exc, "errno", None),
            getattr(last_exc, "winerror", None),
            last_exc or "unknown error",
        )
        if defer and os.name == "nt":
            n_defer = (
                defer_attempts
                if defer_attempts is not None
                else _defer_attempts_default()
            )
            d_delay = defer_delay if defer_delay is not None else _defer_delay_default()
            snapshot = _snapshot(target)
            threading.Thread(
                target=_deferred_delete_worker,
                args=(target, snapshot, n_defer, d_delay),
                name="pdf2zh-deferred-cleanup",
                daemon=True,
            ).start()
        return False
    return True


# Backward-compatible alias for callers that only delete files.
remove_file_robust = remove_path_robust
