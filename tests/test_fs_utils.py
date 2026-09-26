"""Unit tests for pdf2zh.fs_utils best-effort deletion helpers.

Coverage:
  - missing paths are a no-op success;
  - plain files/directories are removed;
  - transient Windows-style locks (PermissionError / WinError 32) are
    retried until the delete succeeds;
  - a persistently locked file does NOT raise and does NOT fake success;
  - background deferred deletion removes the file once the lock clears;
  - the deferred identity guard skips deletion when the path was
    recreated/replaced meanwhile;
  - env-var overrides are honoured (and garbage falls back to defaults).
"""

import logging
import os
import time

from pdf2zh import fs_utils


def _write(path, data=b"x"):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _lock_error(path):
    """A Windows-style sharing-violation PermissionError."""
    err = PermissionError(
        13,
        "The process cannot access the file because it is being used by "
        "another process",
        os.fspath(path),
    )
    err.errno = 13
    err.winerror = 32
    return err


def _flakey_remove(real_remove, fail_first):
    """os.remove stand-in that fails `fail_first` times then delegates."""
    state = {"calls": 0}

    def _inner(path):
        state["calls"] += 1
        if state["calls"] <= fail_first:
            raise _lock_error(path)
        return real_remove(path)

    return _inner, state


class TestBasicRemoval:
    def test_missing_path_counts_as_success(self, tmp_path):
        target = tmp_path / "ghost.pdf"
        assert (
            fs_utils.remove_path_robust(target, attempts=2, delay=0, defer=False)
            is True
        )

    def test_plain_file_is_removed(self, tmp_path):
        target = _write(tmp_path / "plain.pdf")
        assert (
            fs_utils.remove_path_robust(target, attempts=2, delay=0, defer=False)
            is True
        )
        assert not target.exists()

    def test_directory_tree_is_removed(self, tmp_path):
        d = tmp_path / "work"
        (d / "sub").mkdir(parents=True)
        _write(d / "sub" / "a.pdf")
        _write(d / "raw.pdf")
        assert fs_utils.remove_path_robust(d, attempts=2, delay=0, defer=False) is True
        assert not d.exists()


class TestRetryOnLock:
    def test_transient_lock_is_retried_until_success(self, tmp_path, monkeypatch):
        target = _write(tmp_path / "locked.pdf")
        real = os.remove
        fake, state = _flakey_remove(real, fail_first=2)
        monkeypatch.setattr(os, "remove", fake)
        assert (
            fs_utils.remove_path_robust(target, attempts=5, delay=0, defer=False)
            is True
        )
        assert not target.exists()
        assert state["calls"] == 3  # 2 failures + 1 success

    def test_persistent_lock_never_raises_and_reports_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        target = _write(tmp_path / "stuck.pdf")

        def _always_fail(path):
            raise _lock_error(path)

        monkeypatch.setattr(os, "remove", _always_fail)
        with caplog.at_level(logging.WARNING, logger="pdf2zh.fs_utils"):
            ok = fs_utils.remove_path_robust(target, attempts=3, delay=0, defer=False)
        assert ok is False
        assert target.exists()  # honest: file is still there
        assert "stuck.pdf" in caplog.text
        assert "winerror=32" in caplog.text

    def test_not_retryable_error_gives_up_immediately(self, tmp_path, monkeypatch):
        # IsADirectoryError on a file remove (path vanished/mutated) should
        # not spin the retry loop.
        target = _write(tmp_path / "odd.pdf")

        def _raise_isadir(path):
            raise IsADirectoryError(21, "Is a directory", os.fspath(path))

        monkeypatch.setattr(os, "remove", _raise_isadir)
        assert (
            fs_utils.remove_path_robust(target, attempts=9, delay=0, defer=False)
            is False
        )


class TestDeferredCleanup:
    def test_deferred_removal_after_lock_clears(self, tmp_path, monkeypatch):
        target = _write(tmp_path / "deferred.pdf")
        real = os.remove
        fake, _ = _flakey_remove(real, fail_first=1)
        monkeypatch.setattr(os, "remove", fake)
        # Inline phase: single attempt fails -> returns False, schedules retries.
        assert (
            fs_utils.remove_path_robust(
                target,
                attempts=1,
                delay=0,
                defer=True,
                defer_attempts=4,
                defer_delay=0.02,
            )
            is False
        )
        deadline = time.time() + 5.0
        while target.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not target.exists()

    def test_deferred_worker_skips_recreated_file(self, tmp_path):
        target = _write(tmp_path / "recreated.pdf", b"old")
        snapshot = fs_utils._snapshot(target)
        assert snapshot is not None
        # Simulate a later job replacing the file at the same path.
        _write(target, b"new content")
        st = target.stat()
        os.utime(
            target, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000)
        )
        # Background worker must notice the identity changed and leave it alone.
        fs_utils._deferred_delete_worker(str(target), snapshot, attempts=2, delay=0)
        assert target.exists()


class TestEnvOverrides:
    def test_retry_count_env_override_and_garbage_fallback(
        self, tmp_path, monkeypatch, caplog
    ):
        target = _write(tmp_path / "env.pdf")

        def _always_fail(path):
            raise _lock_error(path)

        monkeypatch.setattr(os, "remove", _always_fail)

        monkeypatch.setenv("PDF2ZH_CLEANUP_RETRIES", "2")
        with caplog.at_level(logging.WARNING, logger="pdf2zh.fs_utils"):
            fs_utils.remove_path_robust(target, delay=0, defer=False)
        assert "after 2 attempt(s)" in caplog.text

        caplog.clear()
        monkeypatch.setenv("PDF2ZH_CLEANUP_RETRIES", "garbage")
        with caplog.at_level(logging.WARNING, logger="pdf2zh.fs_utils"):
            fs_utils.remove_path_robust(target, delay=0, defer=False)
        assert "after 4 attempt(s)" in caplog.text  # default
