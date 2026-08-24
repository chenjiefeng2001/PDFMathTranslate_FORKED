"""词表贯通测试：TranslationRequest → 双 babeldoc adapter → CLI/GUI 入口。

不触发真实翻译：
- ``_execute_babeldoc`` 的透传用假 adapter 捕获 kwargs；
- next 内核映射检查 ``build_next_settings`` 产物（离线、无网络）；
- CLI 解析器接受 ``--glossary-files``；非 babeldoc 引擎告警忽略；
- GUI worker 的 gr.Files 值归一化。
"""

from __future__ import annotations

import csv
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh import glossary_store as gs
from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest


def _write_csv(path, rows=(("kernel", "内核", ""),)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "target", "tgt_lng"])
        w.writerows(rows)
    return str(path)


class TestTranslationRequestGlossary:
    def test_default_empty(self):
        req = TranslationRequest(source_path="/tmp/a.pdf")
        assert req.glossary_files == []

    def test_batch_replace_carries_glossary(self):
        req = TranslationRequest(
            source_path="/tmp/a.pdf",
            files=["/tmp/a.pdf", "/tmp/b.pdf"],
            glossary_files=["/tmp/g.csv"],
        )
        sub = dataclasses.replace(req, source_path="/tmp/b.pdf", files=[])
        assert sub.glossary_files == ["/tmp/g.csv"]


class TestExecuteBabeldocPassthrough:
    """_execute_babeldoc 必须把 request.glossary_files 传给两条 adapter。"""

    def _run(self, glossary_files):
        svc = RuntimeService()
        svc._sweeper = None
        tid = "t_glossary"
        svc._store.create_task(tid)
        captured = {}
        with pytest.MonkeyPatch.context() as mp:

            def fake_next(**kwargs):
                captured.update(kwargs)
                return [{"name": "mono.pdf", "path": "/tmp/mono.pdf"}]

            mp.setattr(
                "pdf2zh.babeldoc_next_adapter.run_babeldoc_next_translation",
                fake_next,
            )
            mp.setattr(
                "pdf2zh.babeldoc_adapter.run_babeldoc_translation",
                lambda **kw: (_ for _ in ()).throw(
                    AssertionError("next kernel should have handled it")
                ),
            )
            try:
                svc._execute_babeldoc(
                    tid,
                    TranslationRequest(
                        source_path="/tmp/test.pdf",
                        parse_engine="babeldoc",
                        glossary_files=glossary_files,
                    ),
                )
            except Exception:  # noqa: BLE001 -- 结果收集阶段的失败与本测试无关
                pass
        return captured

    def test_passes_files_through(self):
        captured = self._run(["/tmp/g1.csv", "/tmp/g2.csv"])
        assert captured["glossary_files"] == ["/tmp/g1.csv", "/tmp/g2.csv"]

    def test_empty_list_passed(self):
        captured = self._run([])
        assert captured["glossary_files"] == []


class TestNextKernelMapping:
    def test_settings_carry_comma_separated_paths(self, tmp_path):
        from pdf2zh.babeldoc_next_adapter import build_next_settings

        g1 = _write_csv(tmp_path / "g1.csv")
        settings = build_next_settings(
            service="google",
            lang_in="en",
            lang_out="zh-CN",
            ocr_mode="off",
            glossary_files=[g1],
        )
        assert settings.translation.glossaries == g1

    def test_invalid_csv_fails_before_kernel(self, tmp_path):
        from pdf2zh.babeldoc_next_adapter import build_next_settings

        bad = tmp_path / "bad.csv"
        bad.write_text("nope,wrong\n", encoding="utf-8")
        with pytest.raises(gs.GlossaryError):
            build_next_settings(
                service="google",
                lang_in="en",
                lang_out="zh-CN",
                ocr_mode="off",
                glossary_files=[str(bad)],
            )

    def test_none_leaves_kernel_default(self):
        from pdf2zh.babeldoc_next_adapter import build_next_settings

        settings = build_next_settings(
            service="google",
            lang_in="en",
            lang_out="zh-CN",
            ocr_mode="off",
        )
        assert settings.translation.glossaries is None


class TestCliArg:
    def test_parser_accepts_multiple_paths(self):
        from pdf2zh.pdf2zh import create_parser

        # nargs="+" 贪婪吞掉后续位置参数：PDF 需置于旗标前或用 `--` 分隔
        args = create_parser().parse_args(
            [
                "--glossary-files",
                "/tmp/a.csv",
                "/tmp/b.csv",
                "--",
                "/tmp/test.pdf",
            ]
        )
        assert args.glossary_files == ["/tmp/a.csv", "/tmp/b.csv"]
        assert args.files == ["/tmp/test.pdf"]

    def test_default_empty(self):
        from pdf2zh.pdf2zh import create_parser

        args = create_parser().parse_args(["/tmp/test.pdf"])
        assert args.glossary_files == []


class TestWorkerResolver:
    def test_strings_and_dicts(self, tmp_path):
        from pdf2zh.gui.worker import _resolve_glossary_paths

        real = _write_csv(tmp_path / "w.csv")
        out = _resolve_glossary_paths(
            [real, {"name": str(tmp_path / "w2.csv")}, "", None]
        )
        assert out == [real]

    def test_missing_file_skipped_with_warning(self, tmp_path):
        from pdf2zh.gui.worker import _resolve_glossary_paths

        assert _resolve_glossary_paths([str(tmp_path / "ghost.csv")]) == []

    def test_scalar_and_empty(self):
        from pdf2zh.gui.worker import _resolve_glossary_paths

        assert _resolve_glossary_paths(None) == []
        assert _resolve_glossary_paths([]) == []
