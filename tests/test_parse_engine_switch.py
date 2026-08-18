"""
Tests for the parse-engine switch (CLI / Service / GUI wiring).

Covers the work that lets a user switch the document parsing engine between
the legacy kernel, BabelDOC and the MinerU/magic-pdf pipeline:

  1. ``TranslationRequest.parse_engine`` / ``magicpdf_ocr`` defaults and
     round-trip (mirrors ``--parse-engine`` / ``--magicpdf-ocr``).
  2. ``RuntimeService._execute_task`` routing:
     - ``parse_engine == "magicpdf"``  -> ``_execute_magicpdf``,
     - ``parse_engine == "babeldoc"``  -> ``_execute_babeldoc``,
     - ``auto``                        -> legacy / mode-based routing.
  3. ``RuntimeService._execute_magicpdf`` maps the typed request onto a CLI
     style Namespace (via ``pdf2zh.parse_args`` defaults), invokes
     ``run_magicpdf_main``, collects the ``{output}/magicpdf/*.json`` dumps
     and completes the task.
  4. GUI ``worker.submit_translation_task`` passes both fields through.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest


class TestTranslationRequestParseEngine:
    def test_defaults(self):
        req = TranslationRequest(source_path="/tmp/test.pdf")
        assert req.parse_engine == "auto"
        assert req.magicpdf_ocr is False

    def test_round_trips_explicit_values(self):
        for value in ("auto", "legacy", "babeldoc", "magicpdf"):
            req = TranslationRequest(source_path="/tmp/test.pdf", parse_engine=value)
            assert req.parse_engine == value
        req = TranslationRequest(source_path="/tmp/test.pdf", magicpdf_ocr=True)
        assert req.magicpdf_ocr is True


class TestExecuteTaskRouting:
    """parse_engine routes to the right execution pipeline without heavy deps."""

    def _run(self, parse_engine: str = "auto") -> list:
        svc = RuntimeService()
        svc._sweeper = None
        tid = "t_route"
        svc._store.create_task(tid)
        calls: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(svc, "_apply_request_backend", lambda *a, **k: None)
            mp.setattr(svc, "_execute_legacy", lambda *a, **k: calls.append("legacy"))
            mp.setattr(svc, "_execute_babeldoc", lambda *a, **k: calls.append("babeldoc"))
            mp.setattr(svc, "_execute_magicpdf", lambda *a, **k: calls.append("magicpdf"))
            svc._execute_task(
                tid,
                TranslationRequest(
                    source_path="/tmp/test.pdf", parse_engine=parse_engine,
                    magicpdf_ocr=parse_engine == "magicpdf",
                ),
            )
        return calls

    def test_magicpdf_routes_to_magicpdf(self):
        assert self._run("magicpdf") == ["magicpdf"]

    def test_babeldoc_routes_to_babeldoc(self):
        assert self._run("babeldoc") == ["babeldoc"]

    def test_auto_defaults_to_legacy(self):
        assert self._run("auto") == ["legacy"]

    def test_legacy_routes_to_legacy(self):
        assert self._run("legacy") == ["legacy"]


class TestExecuteMagicpdf:
    def test_maps_request_and_completes(self, tmp_path):
        svc = RuntimeService()
        svc._sweeper = None
        tid = "t_magic"
        svc._store.create_task(tid)
        src = tmp_path / "in.pdf"
        src.write_bytes(b"%PDF-1.4 test")
        out_dir = tmp_path / "out"
        svc.config.output_dir = str(out_dir)
        magic_dir = out_dir / "magicpdf"
        magic_dir.mkdir(parents=True)
        (magic_dir / "in_magicpdf.json").write_text("{}", encoding="utf-8")
        (magic_dir / "in_document.json").write_text("{}", encoding="utf-8")

        captured = {}

        def fake_main(ns):
            captured["ns"] = ns
            return 0

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.magicpdf_cli.run_magicpdf_main", fake_main)
            svc._execute_magicpdf(
                tid,
                TranslationRequest(
                    source_path=str(src),
                    parse_engine="magicpdf",
                    magicpdf_ocr=True,
                    backend="cpu",
                    page_range="1-3",
                    engine="google",
                    source_lang="en",
                    target_lang="zh",
                ),
                svc.config,
            )

        ns = captured["ns"]
        assert ns.files == [str(src)]
        assert ns.magicpdf_ocr is True
        assert ns.backend == "cpu"
        assert ns.pages == "1-3"
        assert ns.service == "google"
        assert ns.lang_in == "en"
        assert ns.lang_out == "zh"
        assert ns.output == str(out_dir)

        state = svc.get_task_state(tid)
        assert state is not None
        assert state.status == "completed"
        assert len(state.result_files) == 2

    def test_failure_marks_task_failed(self, tmp_path):
        svc = RuntimeService()
        svc._sweeper = None
        tid = "t_magic_fail"
        svc._store.create_task(tid)
        src = tmp_path / "in.pdf"
        src.write_bytes(b"%PDF-1.4 test")

        def boom(ns):
            raise RuntimeError("parse crashed")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pdf2zh.magicpdf_cli.run_magicpdf_main", boom)
            svc._execute_magicpdf(
                tid,
                TranslationRequest(source_path=str(src), parse_engine="magicpdf"),
                svc.config,
            )
        state = svc.get_task_state(tid)
        assert state is not None
        assert state.status == "failed"
        assert "parse crashed" in (state.error_message or "")


class TestGuiWorkerPassThrough:
    def test_submit_forwards_parse_engine_fields(self):
        from pdf2zh.gui import worker as worker_mod

        fake_svc = MagicMock()
        fake_svc.submit_task.return_value = "tid_1"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(worker_mod, "get_runtime_service", lambda: fake_svc)
            mp.setattr(worker_mod, "_resolve_source_paths",
                       lambda *a, **k: ["/tmp/test.pdf"])
            task_id = worker_mod.submit_translation_task(
                client_id="c1", file_type="file", file_input="/tmp/test.pdf",
                link_input="", service="google", lang_from="en", lang_to="zh",
                page_range=None, page_input=None, threads=4,
                skip_subset_fonts=False, ignore_cache=False, vfont="", vchar="",
                mode_choice="auto", recaptcha_response="", fl_state=None,
                env0="", env1="", env2="", prompt_env="",
                backend="auto", ocr_mode="auto",
                parse_engine="magicpdf", magicpdf_ocr=True,
            )
        assert task_id == "tid_1"
        req = fake_svc.submit_task.call_args[0][0]
        assert isinstance(req, TranslationRequest)
        assert req.parse_engine == "magicpdf"
        assert req.magicpdf_ocr is True

