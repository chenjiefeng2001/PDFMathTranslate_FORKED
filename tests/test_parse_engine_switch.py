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
        assert req.magicpdf_ocr_mode == "auto"
        req = TranslationRequest(source_path="/tmp/test.pdf", magicpdf_ocr_mode="off")
        assert req.magicpdf_ocr_mode == "off"


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
            mp.setattr(
                svc, "_execute_babeldoc", lambda *a, **k: calls.append("babeldoc")
            )
            mp.setattr(
                svc, "_execute_magicpdf", lambda *a, **k: calls.append("magicpdf")
            )
            svc._execute_task(
                tid,
                TranslationRequest(
                    source_path="/tmp/test.pdf",
                    parse_engine=parse_engine,
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


class TestExecuteBatchRouting:
    """批量任务 per-file 路由必须与 ``_execute_task`` 完全一致（parse_engine 优先）。

    回归：此前 ``_execute_batch`` 只按 ``mode_choice`` 路由，批量任务默认
    ``mode_choice="auto"`` 时，即使显式选择了 ``parse_engine="babeldoc"``，
    逐文件也被错误路由到 legacy 管线——扫描 PDF 的 OCR 走了 legacy /
    magic-pdf 而不是 BabelDOC 本身的扫描检测 + OCR workaround 管线。
    """

    def _run_batch(self, parse_engine: str = "auto", mode_choice: str = "auto") -> list:
        svc = RuntimeService()
        svc._sweeper = None
        tid = "t_batch_route"
        svc._store.create_task(tid)
        calls: list = []
        files = ["/tmp/a.pdf", "/tmp/b.pdf"]
        with pytest.MonkeyPatch.context() as mp:
            # 本组测试断言逐文件调用顺序（串行语义）；并发批处理见
            # test_batch_concurrency.py。
            mp.setenv("PDF2ZH_BATCH_CONCURRENCY", "1")
            mp.setattr(svc, "_execute_legacy", lambda *a, **k: calls.append("legacy"))
            mp.setattr(
                svc, "_execute_babeldoc", lambda *a, **k: calls.append("babeldoc")
            )
            mp.setattr(svc, "_execute_v4", lambda *a, **k: calls.append("v4"))
            svc._execute_batch(
                tid,
                TranslationRequest(
                    source_path=files[0],
                    files=files,
                    parse_engine=parse_engine,
                    extra_config={"mode_choice": mode_choice},
                ),
                files,
                svc.config,
            )
        return calls

    def test_babeldoc_batch_routes_per_file_to_babeldoc(self):
        # 回归：parse_engine=babeldoc + mode_choice=auto（默认）时，批量逐文件
        # 必须走 _execute_babeldoc（BabelDOC 本身管线），而不是 legacy。
        calls = self._run_batch(parse_engine="babeldoc", mode_choice="auto")
        assert calls == ["babeldoc", "babeldoc"]

    def test_mode_babeldoc_batch_routes_to_babeldoc(self):
        # mode_choice=babeldoc 时批量逐文件走 BabelDOC（原行为保持）。
        calls = self._run_batch(parse_engine="auto", mode_choice="babeldoc")
        assert calls == ["babeldoc", "babeldoc"]

    def test_auto_batch_routes_to_legacy(self):
        # 默认 auto：批量逐文件保持 legacy。
        calls = self._run_batch(parse_engine="auto", mode_choice="auto")
        assert calls == ["legacy", "legacy"]


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

        def fake_main(ns, progress_cb=None):
            captured["ns"] = ns
            captured["progress_cb"] = progress_cb
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
        assert callable(captured["progress_cb"])  # 细粒度进度前向回调
        assert ns.files == [str(src)]
        assert ns.magicpdf_ocr is True
        assert ns.magicpdf_ocr_mode == "auto"
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

        def boom(ns, progress_cb=None):
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
            mp.setattr(
                worker_mod, "_resolve_source_paths", lambda *a, **k: ["/tmp/test.pdf"]
            )
            task_id = worker_mod.submit_translation_task(
                client_id="c1",
                file_type="file",
                file_input="/tmp/test.pdf",
                link_input="",
                service="google",
                lang_from="en",
                lang_to="zh",
                page_range=None,
                page_input=None,
                threads=4,
                skip_subset_fonts=False,
                ignore_cache=False,
                vfont="",
                vchar="",
                mode_choice="auto",
                recaptcha_response="",
                fl_state=None,
                env0="",
                env1="",
                env2="",
                prompt_env="",
                backend="auto",
                ocr_mode="auto",
                parse_engine="magicpdf",
                magicpdf_ocr="on",
            )
        assert task_id == "tid_1"
        req = fake_svc.submit_task.call_args[0][0]
        assert isinstance(req, TranslationRequest)
        assert req.parse_engine == "magicpdf"
        assert req.magicpdf_ocr is True
        assert req.magicpdf_ocr_mode == "on"
