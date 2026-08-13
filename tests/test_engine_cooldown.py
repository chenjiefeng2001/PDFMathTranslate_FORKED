"""Engine health-circuit-breaker regression tests (HTTP 429 / CAPTCHA).

When a translation service hard-blocks an IP (Google 429 / reCAPTCHA), the
BabelDOC health check inside ``create_babeldoc_config`` fails deterministically.
Without a circuit breaker a *batch* task re-runs that health check for every
file, producing a long screen of repeated "BabelDOC failed" errors that looks
like a hang. These tests pin the breaker contract:

* ``_is_rate_limited_error`` recognises rate-limit / CAPTCHA messages.
* ``_mark_engine_unavailable`` + ``_engine_cooldown_error`` implement the
  per-engine cooldown window (isolated by engine + envs).
* ``_execute_babeldoc`` fast-fails during cooldown *without* touching the
  adapter, and enters cooldown on a rate-limited exception.

Tasks are created directly through the store (no background worker thread) so
the tests are deterministic.
"""

import time

import pytest

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
    TranslationRequest,
)
from pdf2zh.services.runtime_service import _is_rate_limited_error


def _svc() -> RuntimeService:
    svc = RuntimeService()
    return svc


def _task(svc: RuntimeService, tid: str = "task_cd") -> str:
    svc._store.create_task(tid)
    svc._store.update_task(tid, status=TaskStage.PENDING.value)
    return tid


def _patch_next_runner(monkeypatch, fn):
    """Patch the next-kernel adapter entry point used by _execute_babeldoc."""
    import pdf2zh.babeldoc_next_adapter as na
    monkeypatch.setattr(na, "run_babeldoc_next_translation", fn)


class TestIsRateLimitedError:
    def test_recognizes_next_kernel_message(self):
        exc = RuntimeError(
            "Translation service is rate-limited (HTTP 429 / CAPTCHA blocked). "
            "Please switch proxy/IP or retry later."
        )
        assert _is_rate_limited_error(exc)

    def test_recognizes_legacy_rate_limited_message(self):
        exc = RuntimeError("Google translate rate limited (HTTP 429, url=...)")
        assert _is_rate_limited_error(exc)

    def test_recognizes_captcha_redirect(self):
        exc = RuntimeError("RateLimitedError: Google translate rate limited")
        assert _is_rate_limited_error(exc)

    def test_rejects_unrelated_errors(self):
        assert not _is_rate_limited_error(
            RuntimeError("BabelDOC produced no output files")
        )
        assert not _is_rate_limited_error(RuntimeError("File not found"))


class TestEngineCooldownState:
    def test_query_is_none_before_failure(self):
        svc = _svc()
        key = ("google", "auto", "zh-CN", ())
        assert svc._engine_cooldown_error(key) is None

    def test_mark_then_query_returns_cached_error(self):
        svc = _svc()
        key = ("google", "auto", "zh-CN", ())
        svc._mark_engine_unavailable(key, "429 boom")
        assert svc._engine_cooldown_error(key) == "429 boom"

    def test_cooldown_is_isolated_per_engine(self):
        svc = _svc()
        key = ("google", "auto", "zh-CN", ())
        other = ("bing", "auto", "zh-CN", ())
        svc._mark_engine_unavailable(key, "429 boom")
        assert svc._engine_cooldown_error(other) is None

    def test_cooldown_is_isolated_per_envs(self):
        svc = _svc()
        key = ("openai", "auto", "zh-CN", (("openai_api_key", "aaa"),))
        other = ("openai", "auto", "zh-CN", (("openai_api_key", "bbb"),))
        svc._mark_engine_unavailable(key, "429 boom")
        assert svc._engine_cooldown_error(other) is None

    def test_cooldown_expires_after_window(self, monkeypatch):
        import pdf2zh.services.runtime_service as rs_mod
        monkeypatch.setattr(rs_mod, "_ENGINE_COOLDOWN_SECONDS", -1.0)
        svc = _svc()
        key = ("google", "auto", "zh-CN", ())
        svc._mark_engine_unavailable(key, "429 boom")
        assert svc._engine_cooldown_error(key) is None


class TestExecuteBabeldocBreaker:
    def test_fast_fails_during_cooldown_without_touching_adapter(self, monkeypatch):
        svc = _svc()
        tid = _task(svc)
        req = TranslationRequest(
            source_path="/tmp/a.pdf", target_lang="zh-CN", engine="google",
        )
        svc._mark_engine_unavailable(svc._engine_key(req), "rate-limited (HTTP 429)")

        calls = {"n": 0}

        def boom(*args, **kwargs):  # pragma: no cover - must never be reached
            calls["n"] += 1
            raise AssertionError("health check must not run during cooldown")

        _patch_next_runner(monkeypatch, boom)
        svc._execute_babeldoc(tid, req)

        assert calls["n"] == 0
        state = svc._store.get_task(tid)
        assert state.status == TaskStage.FAILED.value
        assert "temporarily unavailable" in (state.error_message or "")

    def test_rate_limited_exception_enters_cooldown(self, monkeypatch):
        svc = _svc()
        tid = _task(svc)
        req = TranslationRequest(
            source_path="/tmp/a.pdf", target_lang="zh-CN", engine="google",
        )

        def boom(*args, **kwargs):
            raise RuntimeError(
                "Translation service is rate-limited (HTTP 429 / CAPTCHA blocked)"
            )

        _patch_next_runner(monkeypatch, boom)
        svc._execute_babeldoc(tid, req)

        err = svc._engine_cooldown_error(svc._engine_key(req))
        assert err is not None and "429" in err
        assert svc._store.get_task(tid).status == TaskStage.FAILED.value

    def test_non_rate_limited_failure_does_not_enter_cooldown(self, monkeypatch):
        svc = _svc()
        tid = _task(svc)
        req = TranslationRequest(
            source_path="/tmp/a.pdf", target_lang="zh-CN", engine="google",
        )

        def boom(*args, **kwargs):
            raise RuntimeError("create_babeldoc_config exploded")

        _patch_next_runner(monkeypatch, boom)
        svc._execute_babeldoc(tid, req)

        assert svc._engine_cooldown_error(svc._engine_key(req)) is None
        assert svc._store.get_task(tid).status == TaskStage.FAILED.value

    def test_batch_like_second_file_fast_fails_after_first_rate_limit(self, monkeypatch):
        svc = _svc()
        tid = _task(svc)
        req = TranslationRequest(
            source_path="/tmp/a.pdf", target_lang="zh-CN", engine="google",
        )
        calls = {"n": 0}

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("Google translate rate limited (HTTP 429, url=...)")

        _patch_next_runner(monkeypatch, boom)

        # First file: real health-check failure -> task FAILED, engine enters cooldown.
        svc._execute_babeldoc(tid, req)
        assert calls["n"] == 1
        # Second file: breaker fast-fails; the adapter must not be invoked again.
        svc._execute_babeldoc(tid, req)
        assert calls["n"] == 1

