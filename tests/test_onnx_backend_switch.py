"""
Tests for the per-task ONNX inference backend switch.

Covers the GUI/backend wiring added to give users a front-end toggle for the
BabelDOC / doclayout ONNX execution backend (auto / cpu / cuda / dml):

  1. ``TranslationRequest.backend`` defaults to ``"auto"`` and round-trips.
  2. ``RuntimeService._apply_request_backend``:
     - does nothing when the requested backend matches the current one,
     - switches the process-global backend and resets the cached
       ``ModelInstance`` singleton when it changes,
     - normalises unknown values back to ``"auto"``.
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest


class _FakeModelInstance:
    value = object()


class TestTranslationRequestBackend:
    def test_defaults_to_auto(self):
        req = TranslationRequest(source_path="/tmp/test.pdf")
        assert req.backend == "auto"

    def test_round_trips_explicit_value(self):
        for value in ("auto", "cpu", "cuda", "dml"):
            req = TranslationRequest(source_path="/tmp/test.pdf", backend=value)
            assert req.backend == value


class TestApplyRequestBackend:
    """Exercise the backend-application logic without touching ONNX Runtime."""

    def _svc(self):
        svc = RuntimeService()
        svc._store = MagicMock()
        return svc

    def test_auto_default_does_not_switch(self):
        svc = self._svc()
        calls = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: None)
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t1", TranslationRequest(source_path="a.pdf", backend="auto")
            )
        assert calls == []

    def test_switch_to_cuda_applies_and_resets_singleton(self):
        svc = self._svc()
        calls = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: None)
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            _FakeModelInstance.value = "old-session"
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t2", TranslationRequest(source_path="a.pdf", backend="cuda")
            )
        assert calls == [("set", "cuda")]
        assert _FakeModelInstance.value is None

    def test_same_backend_is_noop(self):
        svc = self._svc()
        calls = [("set", "cuda")]  # already applied in a previous task
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: "cuda")
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            _FakeModelInstance.value = "session"
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t3", TranslationRequest(source_path="a.pdf", backend="cuda")
            )
        assert calls == [("set", "cuda")]  # unchanged
        assert _FakeModelInstance.value == "session"  # singleton kept

    def test_switch_back_to_auto(self):
        svc = self._svc()
        calls = [("set", "cuda")]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: "cuda")
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            _FakeModelInstance.value = "session"
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t4", TranslationRequest(source_path="a.pdf", backend="auto")
            )
        assert calls == [("set", "cuda"), ("set", "auto")]
        assert _FakeModelInstance.value is None

    def test_unknown_value_normalises_to_auto(self):
        svc = self._svc()
        calls = [("set", "cuda")]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: "cuda")
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            _FakeModelInstance.value = "session"
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t5", TranslationRequest(source_path="a.pdf", backend="bogus")
            )
        assert calls == [("set", "cuda"), ("set", "auto")]
        assert _FakeModelInstance.value is None

    def test_dml_is_supported(self):
        svc = self._svc()
        calls = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.doclayout.get_backend", lambda: None)
            mp.setattr(
                "pdf2zh.doclayout.set_backend",
                lambda name: calls.append(("set", name)),
            )
            mp.setattr("pdf2zh.doclayout.ModelInstance", _FakeModelInstance)
            svc._apply_request_backend(
                "t6", TranslationRequest(source_path="a.pdf", backend="dml")
            )
        assert calls == [("set", "dml")]
        assert _FakeModelInstance.value is None
