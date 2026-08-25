"""性能基准报告（doc/perf/itbook-benchmark）修复的单元测试。

覆盖四个 P0 修复，全部离线、不触发真实翻译/网络：
- P0 #1 magicpdf 页切片解析（parse 调度 + 页号还原 + 熔断开关）
- P0 #2 legacy 切片-回贴（translate_stream hook + pymupdf 回贴 helpers）
- P0 #3 babeldoc 子进程隔离（worker 协议 + 子进程 runner + 取消）
- P0 #4 服务启动 layout 模型预热
"""

from __future__ import annotations

import io
import json
import os
import time
from dataclasses import dataclass, field
from unittest.mock import patch

import pymupdf
import pytest

import pdf2zh.high_level as high_level
from pdf2zh.babeldoc_next_adapter import (
    BabeldocNextUnavailableError,
    _BabeldocNextCancelledError,
    run_babeldoc_next_translation_subprocess,
)


# ── 共享夹具 ────────────────────────────────────────────────────────────────


def _make_pdf(n_pages: int, toc: list | None = None) -> bytes:
    """构造 n 页测试 PDF，每页含可识别文本；可选书签。"""
    doc = pymupdf.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"PAGE-{i}-ORIGINAL")
    if toc:
        doc.set_toc(toc)
    data = doc.tobytes()
    doc.close()
    return data


def _text(data: bytes, page_no: int) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        return d[page_no].get_text()


def _page_count(data: bytes) -> int:
    with pymupdf.open(stream=data, filetype="pdf") as d:
        return d.page_count


# ── P0 #2：切片-回贴 helpers ────────────────────────────────────────────────


class TestSliceSpliceHelpers:
    def test_normalize_slice_pages(self):
        assert high_level._normalize_slice_pages([3, 1, 1, 9], 6) == [1, 3]
        assert high_level._normalize_slice_pages([-1, 0, 5], 6) == [0, 5]
        assert high_level._normalize_slice_pages(["bad", None], 6) == []
        assert high_level._normalize_slice_pages(None, 6) == []

    def test_slice_pdf_pages(self):
        data = _make_pdf(6)
        sliced, page_map = high_level._slice_pdf_pages(data, [1, 3])
        assert _page_count(sliced) == 2
        assert page_map == {0: 1, 1: 3}
        assert "PAGE-1-ORIGINAL" in _text(sliced, 0)
        assert "PAGE-3-ORIGINAL" in _text(sliced, 1)

    def test_splice_mono_pages_replaces_selected_keeps_toc(self):
        toc = [[1, "Chapter 1", 1], [1, "Chapter 4", 4]]
        data = _make_pdf(6, toc=toc)
        # 切片 mono：2 页，各带译文本
        mono_doc = pymupdf.open()
        for j in range(2):
            p = mono_doc.new_page()
            p.insert_text((72, 72), f"MONO-T{j}")
        mono_slice = mono_doc.tobytes()
        mono_doc.close()

        out = high_level._splice_mono_pages(data, mono_slice, [1, 3])
        assert _page_count(out) == 6
        assert "PAGE-0-ORIGINAL" in _text(out, 0)
        assert "MONO-T0" in _text(out, 1)
        assert "PAGE-2-ORIGINAL" in _text(out, 2)
        assert "MONO-T1" in _text(out, 3)
        assert "PAGE-4-ORIGINAL" in _text(out, 4)
        assert "PAGE-5-ORIGINAL" in _text(out, 5)
        with pymupdf.open(stream=out, filetype="pdf") as d:
            assert d.get_toc(simple=True) == toc

    def test_interleave_dual_pages_structure_and_toc(self):
        data = _make_pdf(6, toc=[[1, "Ch", 2]])
        # 切片 dual：[E0, Z0, E1, Z1]
        dual_doc = pymupdf.open()
        for j in range(2):
            pe = dual_doc.new_page()
            pe.insert_text((72, 72), f"DUAL-E{j}")
            pz = dual_doc.new_page()
            pz.insert_text((72, 72), f"DUAL-Z{j}")
        dual_slice = dual_doc.tobytes()
        dual_doc.close()

        out = high_level._interleave_dual_pages(data, dual_slice, [1, 3])
        assert _page_count(out) == 12
        assert "PAGE-0-ORIGINAL" in _text(out, 0)
        assert "PAGE-0-ORIGINAL" in _text(out, 1)  # 未选中页 = 原页副本
        assert "PAGE-1-ORIGINAL" in _text(out, 2)
        assert "DUAL-Z0" in _text(out, 3)
        assert "PAGE-3-ORIGINAL" in _text(out, 6)
        assert "DUAL-Z1" in _text(out, 7)
        # TOC 页 p（1 基）→ dual 1 基 2p：原页 2 → 位置 4（0 基 3... 1 基 4）
        with pymupdf.open(stream=out, filetype="pdf") as d:
            assert d.get_toc(simple=True) == [[1, "Ch", 4]]

    def test_remap_slice_local_pages(self):
        v3 = {
            "processor_reports": {0: "a", 1: "b"},
            "ir_snapshots": {1: {"x": 1}},
            "other": "untouched",
        }
        high_level._remap_slice_local_pages(v3, {0: 4, 1: 9})
        assert v3["processor_reports"] == {4: "a", 9: "b"}
        assert v3["ir_snapshots"] == {9: {"x": 1}}
        assert v3["other"] == "untouched"
        # 非 int 键 / 空输入安全
        high_level._remap_slice_local_pages({"d": {"k": 1}}, {0: 1})
        high_level._remap_slice_local_pages(None, {0: 1})


class TestTranslateStreamSliceSplice:
    @pytest.fixture()
    def slice_intercept(self, monkeypatch):
        """拦截切片递归调用：返回带标记的假切片产物，记录切片页数。"""
        calls: dict = {}

        real_ts = high_level.translate_stream

        def fake_ts(stream, pages=None, _allow_slice_splice=True, **kw):
            if pages is None and _allow_slice_splice is False:
                with pymupdf.open(stream=stream, filetype="pdf") as sl:
                    k = sl.page_count
                calls["slice_pages"] = k
                calls["v3_output"] = kw.get("v3_output")
                if isinstance(calls["v3_output"], dict):
                    calls["v3_output"]["processor_reports"] = {
                        i: {"page": i} for i in range(k)
                    }
                mono = pymupdf.open()
                dual = pymupdf.open()
                for j in range(k):
                    pm = mono.new_page()
                    pm.insert_text((72, 72), f"MONO-T{j}")
                    pe = dual.new_page()
                    pe.insert_text((72, 72), f"DUAL-E{j}")
                    pz = dual.new_page()
                    pz.insert_text((72, 72), f"DUAL-Z{j}")
                return dual.tobytes(), mono.tobytes()
            return real_ts(stream, pages=pages, _allow_slice_splice=_allow_slice_splice, **kw)

        monkeypatch.setattr(high_level, "translate_stream", fake_ts)
        return calls

    def test_subset_translation_splices_back(self, slice_intercept, monkeypatch):
        monkeypatch.delenv("PDF2ZH_NO_SLICE_SPLICE", raising=False)
        data = _make_pdf(6)
        v3_output: dict = {}

        dual, mono = high_level.translate_stream(
            data, pages=[1, 3], v3_output=v3_output
        )

        assert slice_intercept.get("slice_pages") == 2
        # mono：原位替换选中页
        assert _page_count(mono) == 6
        assert "PAGE-0-ORIGINAL" in _text(mono, 0)
        assert "MONO-T0" in _text(mono, 1)
        assert "MONO-T1" in _text(mono, 3)
        assert "PAGE-5-ORIGINAL" in _text(mono, 5)
        # dual：2N 交错，未选中页原页副本
        assert _page_count(dual) == 12
        assert "PAGE-1-ORIGINAL" in _text(dual, 2)
        assert "DUAL-Z0" in _text(dual, 3)
        assert "DUAL-Z1" in _text(dual, 7)
        # v3_output 页号键还原为原文档页号
        assert set(v3_output["processor_reports"].keys()) == {1, 3}

    def test_env_kill_switch_disables_slicing(self, slice_intercept, monkeypatch):
        """PDF2ZH_NO_SLICE_SPLICE=1 时停用切片（走全文档路径）。"""
        monkeypatch.setenv("PDF2ZH_NO_SLICE_SPLICE", "1")
        # 全文档路径需要字体：打桩避免网络/磁盘依赖；后续 Font() 构造抛错
        # 即视为「进入了全文档路径」。
        monkeypatch.setattr(
            high_level, "download_remote_fonts", lambda lang: "Z:/nope/font.ttf"
        )
        data = _make_pdf(6)
        with pytest.raises(Exception):
            high_level.translate_stream(data, pages=[1, 3])
        assert "slice_pages" not in slice_intercept

    def test_emit_ir_disables_slicing(self, slice_intercept, monkeypatch):
        monkeypatch.delenv("PDF2ZH_NO_SLICE_SPLICE", raising=False)
        monkeypatch.setattr(
            high_level, "download_remote_fonts", lambda lang: "Z:/nope/font.ttf"
        )
        data = _make_pdf(6)
        with pytest.raises(Exception):
            high_level.translate_stream(data, pages=[1, 3], emit_ir=True)
        assert "slice_pages" not in slice_intercept


# ── P0 #1：magicpdf 页切片 ──────────────────────────────────────────────────

from pdf2zh.magicpdf_adapter import (  # noqa: E402
    MagicPdfAdapter,
    MagicPdfParseResult,
    _normalize_page_selection,
    _remap_magicpdf_result_pages,
    _slice_pdf_for_pages,
)


class TestMagicPdfPageSlice:
    def test_normalize_page_selection(self):
        assert _normalize_page_selection("1-3, 5", 10) == [1, 2, 3, 5]
        assert _normalize_page_selection([4, 2, 99, "x"], 10) == [2, 4]
        assert _normalize_page_selection(None, 10) == []
        assert _normalize_page_selection("all", 10) == []
        assert _normalize_page_selection("", 10) == []

    def test_slice_for_pages_subset(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_make_pdf(6))
        slice_path, page_map = _slice_pdf_for_pages(str(pdf), [1, 3])
        try:
            assert slice_path is not None
            assert os.path.exists(slice_path)
            assert _page_count(open(slice_path, "rb").read()) == 2
            assert page_map == {0: 1, 1: 3}
        finally:
            if slice_path:
                os.unlink(slice_path)

    def test_slice_for_pages_noop_cases(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_make_pdf(3))
        assert _slice_pdf_for_pages(str(pdf), None) == (None, None)
        assert _slice_pdf_for_pages(str(pdf), "all") == (None, None)
        assert _slice_pdf_for_pages(str(pdf), "") == (None, None)
        assert _slice_pdf_for_pages(str(pdf), [0, 1, 2]) == (None, None)  # 全选
        assert _slice_pdf_for_pages(str(pdf), [7, 8]) == (None, None)  # 全越界

    def test_slice_for_pages_env_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PDF2ZH_NO_MAGICPDF_SLICE", "1")
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_make_pdf(3))
        assert _slice_pdf_for_pages(str(pdf), [1]) == (None, None)

    def test_remap_result_pages(self):
        results = [
            MagicPdfParseResult(page_num=0, width=1, height=1, raw={"page_no": 0}),
            MagicPdfParseResult(page_num=1, width=1, height=1, raw={"page_no": 1}),
        ]
        _remap_magicpdf_result_pages(results, {0: 5, 1: 9})
        assert [r.page_num for r in results] == [5, 9]
        assert [r.raw["page_no"] for r in results] == [5, 9]

    def test_parse_dispatches_sliced_and_cleans_up(self, tmp_path, monkeypatch):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_make_pdf(6))
        seen: dict = {}
        slice_paths: list = []

        def fake_parse_by_backend(self, backend, path, pages=None, ocr=False, progress_cb=None):
            seen["backend"] = backend
            seen["path_pages"] = _page_count(open(path, "rb").read())
            seen["pages_arg"] = pages
            slice_paths.append(path)
            return [
                MagicPdfParseResult(page_num=0, width=1, height=1, raw={"page_no": 0}),
                MagicPdfParseResult(page_num=1, width=1, height=1, raw={"page_no": 1}),
            ]

        monkeypatch.setattr(MagicPdfAdapter, "backend", lambda self: "magicpdf")
        monkeypatch.setattr(MagicPdfAdapter, "_parse_by_backend", fake_parse_by_backend)

        adapter = MagicPdfAdapter(device="cpu")
        results = adapter.parse(str(pdf), pages=[1, 3])

        assert seen["path_pages"] == 2  # 只分析切片
        assert seen["pages_arg"] is None  # 切片内不再过滤
        assert [r.page_num for r in results] == [1, 3]  # 页号还原
        assert all(not os.path.exists(p) for p in slice_paths)  # 临时文件清理

    def test_parse_without_pages_skips_slice(self, tmp_path, monkeypatch):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_make_pdf(3))
        seen: dict = {}

        def fake_parse_by_backend(self, backend, path, pages=None, ocr=False, progress_cb=None):
            seen["path"] = path
            seen["pages_arg"] = pages
            return []

        monkeypatch.setattr(MagicPdfAdapter, "backend", lambda self: "magicpdf")
        monkeypatch.setattr(MagicPdfAdapter, "_parse_by_backend", fake_parse_by_backend)
        adapter = MagicPdfAdapter(device="cpu")
        adapter.parse(str(pdf))
        assert seen["path"] == str(pdf)
        assert seen["pages_arg"] is None


# ── P0 #3：babeldoc 子进程隔离 ──────────────────────────────────────────────

from pdf2zh import babeldoc_next_worker as worker_mod  # noqa: E402


class TestBabeldocNextWorker:
    def _run_main(self, monkeypatch, payload, fake_impl, capsys):
        monkeypatch.setattr(
            "pdf2zh.babeldoc_next_adapter.run_babeldoc_next_translation", fake_impl
        )
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps(payload))
        )
        rc = worker_mod.main()
        out = capsys.readouterr().out.strip().splitlines()
        frames = [json.loads(line) for line in out if line.strip()]
        return rc, frames

    def test_success_frame(self, monkeypatch, capsys):
        def fake(**kw):
            # 取消不可跨进程：worker 侧恒为 None（父进程看门狗 kill 实现）
            assert kw["cancelled_check"] is None
            assert callable(kw["progress_cb"])
            return [{"name": "a_mono.pdf", "path": "/x/a_mono.pdf"}]

        rc, frames = self._run_main(
            monkeypatch, {"source_path": "a.pdf", "lang_in": "en"}, fake, capsys
        )
        assert rc == 0
        assert frames[-1] == {
            "ok": True,
            "files": [{"name": "a_mono.pdf", "path": "/x/a_mono.pdf"}],
        }

    def test_progress_frames_streamed(self, monkeypatch, capsys):
        def fake(**kw):
            kw["progress_cb"]("translating", 50.0, "half", {"current": 1})
            return []

        rc, frames = self._run_main(monkeypatch, {"source_path": "a.pdf"}, fake, capsys)
        assert rc == 0
        assert frames[0]["progress"] is True
        assert frames[0]["pct"] == 50.0
        assert frames[0]["detail"] == {"current": 1}
        assert frames[-1]["ok"] is True

    def test_unavailable_exit_code(self, monkeypatch, capsys):
        def fake(**kw):
            raise BabeldocNextUnavailableError("kernel missing")

        rc, frames = self._run_main(monkeypatch, {"source_path": "a.pdf"}, fake, capsys)
        assert rc == 2
        assert frames[-1]["error_type"] == "BabeldocNextUnavailableError"
        assert frames[-1]["ok"] is False

    def test_generic_error_exit_code(self, monkeypatch, capsys):
        def fake(**kw):
            raise RuntimeError("boom")

        rc, frames = self._run_main(monkeypatch, {"source_path": "a.pdf"}, fake, capsys)
        assert rc == 1
        assert "boom" in frames[-1]["error"]

    def test_bad_payload_exit_code(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        rc = worker_mod.main()
        assert rc == 1


class TestBabeldocSubprocessRunner:
    """子进程 runner 端到端：用轻量 stub worker 验证协议/错误映射/取消。"""

    @pytest.fixture()
    def stub_worker(self, monkeypatch):
        monkeypatch.setenv("PDF2ZH_BABELDOC_WORKER_MODULE", "tests.stub_babeldoc_worker")

    def _runner_kwargs(self, tmp_path, name="ok.pdf"):
        return dict(
            source_path=str(tmp_path / name),
            lang_in="en",
            lang_out="zh-CN",
            service="google",
        )

    def test_success_and_progress(self, stub_worker, tmp_path):
        events: list = []
        files = run_babeldoc_next_translation_subprocess(
            progress_cb=lambda *a: events.append(a), **self._runner_kwargs(tmp_path)
        )
        assert files == [{"name": "a_mono.pdf", "path": "x/a_mono.pdf"}]
        assert any(e[0] == "translating" and e[1] == 50.0 for e in events)

    def test_generic_error_mapping(self, stub_worker, tmp_path):
        with pytest.raises(RuntimeError, match="boom"):
            run_babeldoc_next_translation_subprocess(
                **self._runner_kwargs(tmp_path, "boom.pdf")
            )

    def test_unavailable_error_mapping(self, stub_worker, tmp_path):
        with pytest.raises(BabeldocNextUnavailableError):
            run_babeldoc_next_translation_subprocess(
                **self._runner_kwargs(tmp_path, "unavail.pdf")
            )

    def test_cancellation_kills_process(self, stub_worker, tmp_path):
        with pytest.raises(_BabeldocNextCancelledError):
            run_babeldoc_next_translation_subprocess(
                cancelled_check=lambda: True,
                **self._runner_kwargs(tmp_path, "slow.pdf"),
            )


# ── P0 #4：服务启动预热 ─────────────────────────────────────────────────────


class TestLayoutModelPrewarm:
    def test_prewarm_loads_model_and_fonts(self, monkeypatch):
        """create_api_app 启动后台预热：ModelInstance 单例 + 远程字体。"""
        from pdf2zh.doclayout import ModelInstance, OnnxModel

        sentinel = object()
        loaded = {}
        monkeypatch.setattr(ModelInstance, "value", None, raising=False)
        monkeypatch.setattr(
            OnnxModel, "load_available", staticmethod(lambda: sentinel)
        )

        import pdf2zh.services.api as api_mod

        def fake_fonts(lang):
            loaded["lang"] = lang
            return "Z:/fake/font.ttf"

        monkeypatch.setattr(high_level, "download_remote_fonts", fake_fonts)
        monkeypatch.delenv("PDF2ZH_NO_WARMUP", raising=False)

        app = api_mod.create_api_app()
        deadline = time.time() + 15
        while time.time() < deadline:
            if ModelInstance.value is sentinel and "lang" in loaded:
                break
            time.sleep(0.1)
        assert ModelInstance.value is sentinel
        assert loaded["lang"] == "zh"

    def test_prewarm_respects_kill_switch(self, monkeypatch):
        from pdf2zh.doclayout import ModelInstance, OnnxModel

        monkeypatch.setenv("PDF2ZH_NO_WARMUP", "1")
        monkeypatch.setattr(ModelInstance, "value", None, raising=False)

        def _fail():
            raise AssertionError("should not load model when warmup disabled")

        monkeypatch.setattr(OnnxModel, "load_available", staticmethod(_fail))

        import pdf2zh.services.api as api_mod

        api_mod.create_api_app()
        time.sleep(0.5)  # 给预热线程（若误启动）机会暴露
        assert ModelInstance.value is None
