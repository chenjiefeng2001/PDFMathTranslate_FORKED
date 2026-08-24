"""细粒度进度统计（P1）+ MinerU 3.x 对接回归测试。

对应 ``doc/granular_progress_feasibility_report.md`` P1 及其后续 P0：
- magic-pdf ``doc_analyze`` Batch/组件加载 loguru 日志 → 结构化 detail；
- magicpdf_cli 把适配器计数回调升格为完整进度事件（相位内插、单调）；
- 服务层 magicpdf 路径 detail 经 _emit_smooth 全链贯通；
- MinerU 官方编程入口 ``do_parse`` 对接：签名过滤、OCR 映射、页码切片、
  middle.json 消费、TypeError 最小参数重试、进度起始事件。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.magicpdf_adapter import (
    MagicPdfAdapter,
    MagicPdfParseError,
    _build_do_parse_kwargs,
    _find_mineru_middle_json,
    _magicpdf_log_component,
    _magicpdf_log_to_detail,
    _MagicPdfLogProbe,
)
from pdf2zh.magicpdf_cli import (
    _PCT_PARSE_END,
    _PCT_PARSE_START,
    _make_parse_progress,
    run_magicpdf_main,
)


# ── magic-pdf 日志 → 结构化 detail ──────────────────────────────────────────


def test_batch_log_line_to_detail():
    # magic_pdf/model/doc_analyze_by_custom_model.py:162 的原始格式
    text = "Batch 2/5: 320 pages/800 pages"
    assert _magicpdf_log_to_detail(text) == {
        "engine": "magicpdf",
        "raw_stage": "doc_analyze",
        "unit": "page",
        "current": 320,
        "total": 800,
        "batch_current": 2,
        "batch_total": 5,
    }


def test_batch_log_single_batch():
    # 未达 MIN_BATCH_INFERENCE_SIZE 时也是单批一条日志
    d = _magicpdf_log_to_detail("Batch 1/1: 3 pages/3 pages")
    assert d is not None
    assert d["current"] == 3 and d["total"] == 3


def test_non_batch_log_returns_none():
    assert _magicpdf_log_to_detail("model init cost: 12.34") is None
    assert _magicpdf_log_to_detail("") is None
    assert _magicpdf_log_to_detail("gpu_memory: 8 GB, batch_ratio: 16") is None


def test_component_keywords():
    assert _magicpdf_log_component("model init cost: 12.34") is not None
    assert _magicpdf_log_component("Loading model: yolo_v8_mfd") is not None
    assert _magicpdf_log_component("load model from disk") is not None
    assert _magicpdf_log_component("Batch 1/2: 5 pages/9 pages") is None
    assert _magicpdf_log_component("") is None


def test_probe_forwards_and_never_raises():
    seen: list[dict] = []

    class _Msg:
        def __init__(self, name: str, text: str) -> None:
            self.record = {"name": name, "message": text}

    probe = _MagicPdfLogProbe(seen.append)
    probe._on_message(
        _Msg(
            "magic_pdf.model.doc_analyze_by_custom_model",
            "Batch 1/2: 4 pages/9 pages",
        )
    )
    probe._on_message(_Msg("other.module", "Batch 1/2: 4 pages/9 pages"))
    probe._on_message(_Msg("magic_pdf.model.x", "model init cost: 3.2"))

    assert len(seen) == 2
    assert seen[0]["unit"] == "page" and seen[0]["total"] == 9
    assert seen[1]["unit"] == "component"

    # 垃圾输入不抛异常
    probe._on_message(_Msg("magic_pdf", ""))
    probe._on_message(object())
    assert len(seen) == 2


def test_probe_enter_exit_without_report_is_noop():
    probe = _MagicPdfLogProbe(None)
    with probe as p:
        assert p is probe
    assert probe._sink_id is None


def test_probe_adds_and_removes_loguru_sink():
    loguru = pytest_import_loguru()
    if loguru is None:
        return  # 环境未装 loguru（magic-pdf 缺失），跳过
    seen: list[str] = []
    probe = _MagicPdfLogProbe(lambda d: seen.append(d.get("raw_stage")))
    with probe:
        assert probe._sink_id is not None
    assert probe._sink_id is None


def pytest_import_loguru():
    try:
        import loguru  # noqa: F401

        return loguru
    except Exception:  # noqa: BLE001
        return None


# ── MinerU 3.x do_parse 对接 ────────────────────────────────────────────────

#: magic-pdf 1.3.12 / MinerU middle.json 同构 fixture（pdf_info + para_blocks）
_MIDDLE_FIXTURE = {
    "pdf_info": [
        {
            "page_size": [612.0, 792.0],
            "para_blocks": [
                {
                    "type": "text",
                    "bbox": [10, 10, 200, 40],
                    "lines": [
                        {
                            "bbox": [10, 10, 200, 40],
                            "spans": [
                                {
                                    "bbox": [10, 10, 200, 40],
                                    "content": "hello mineru",
                                    "type": "text",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        {"page_size": [612.0, 792.0], "para_blocks": []},
    ]
}


def _write_middle(output_dir: str, stem: str) -> None:
    with open(
        os.path.join(output_dir, f"{stem}_middle.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(_MIDDLE_FIXTURE, fh)


def _install_fake_mineru(monkeypatch, do_parse) -> list[dict]:
    """向 sys.modules 注入最小 ``mineru.cli.common`` 假实现，返回调用记录。"""
    calls: list[dict] = []

    def _do_parse(**kwargs):
        calls.append(kwargs)
        return do_parse(**kwargs)

    common = types.ModuleType("mineru.cli.common")
    common.do_parse = _do_parse
    common.read_fn = lambda p: b"%PDF-1.4 fake-bytes"
    cli = types.ModuleType("mineru.cli")
    cli.common = common
    root = types.ModuleType("mineru")
    root.cli = cli
    monkeypatch.setitem(sys.modules, "mineru", root)
    monkeypatch.setitem(sys.modules, "mineru.cli", cli)
    monkeypatch.setitem(sys.modules, "mineru.cli.common", common)
    return calls


@pytest.fixture()
def fake_pdf(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 not-a-real-pdf-but-file-exists")
    return str(pdf)


def test_parse_mineru_end_to_end_with_fake_engine(fake_pdf, monkeypatch):
    def do_parse(**kwargs):
        _write_middle(kwargs["output_dir"], kwargs["pdf_file_names"][0])

    calls = _install_fake_mineru(monkeypatch, do_parse)
    results = MagicPdfAdapter()._parse_mineru(
        fake_pdf, pages=[0], progress_cb=lambda d: None
    )

    assert len(calls) == 1
    kw = calls[0]
    assert kw["backend"] == "pipeline"          # 本地后端固定
    assert kw["parse_method"] == "auto"          # 非 OCR 默认
    assert kw["f_dump_middle_json"] is True      # 必须产出 middle.json
    assert kw["f_dump_md"] is False              # 不产无关产物
    assert kw["p_lang_list"] == ["ch"]
    # 归一化链路复用：block 文本与页尺寸来自 middle.json
    assert len(results) == 1
    assert results[0].backend == "mineru"
    assert results[0].width == 612.0 and results[0].height == 792.0
    assert results[0].text() == "hello mineru"


def test_parse_mineru_ocr_maps_to_parse_method(fake_pdf, monkeypatch):
    def do_parse(**kwargs):
        _write_middle(kwargs["output_dir"], kwargs["pdf_file_names"][0])

    calls = _install_fake_mineru(monkeypatch, do_parse)
    MagicPdfAdapter()._parse_mineru(fake_pdf, ocr=True)
    assert calls[0]["parse_method"] == "ocr"


def test_parse_mineru_page_range_to_slice_ids(fake_pdf, monkeypatch):
    def do_parse(**kwargs):
        _write_middle(kwargs["output_dir"], kwargs["pdf_file_names"][0])

    calls = _install_fake_mineru(monkeypatch, do_parse)
    MagicPdfAdapter()._parse_mineru(fake_pdf, pages=[3, 1, 7])
    assert calls[0]["start_page_id"] == 1
    assert calls[0]["end_page_id"] == 7


def test_parse_mineru_reports_start_progress_event(fake_pdf, monkeypatch):
    def do_parse(**kwargs):
        _write_middle(kwargs["output_dir"], kwargs["pdf_file_names"][0])

    _install_fake_mineru(monkeypatch, do_parse)
    reported: list[dict] = []
    MagicPdfAdapter()._parse_mineru(fake_pdf, progress_cb=reported.append)
    assert reported and reported[0]["engine"] == "mineru"
    assert reported[0]["unit"] == "page"
    assert reported[0]["raw_stage"] == "pipeline"


def test_parse_mineru_retries_with_minimal_args_on_type_error(
    fake_pdf, monkeypatch
):
    def do_parse(output_dir, pdf_file_names, pdf_bytes_list, p_lang_list, **kw):
        if "f_dump_middle_json" in kw:
            raise TypeError("unexpected keyword argument 'f_dump_middle_json'")
        _write_middle(output_dir, pdf_file_names[0])

    calls = _install_fake_mineru(monkeypatch, do_parse)
    results = MagicPdfAdapter()._parse_mineru(fake_pdf)
    assert len(calls) == 2  # 首次 TypeError → 最小参数集重试一次
    assert results and results[0].text() == "hello mineru"


def test_parse_mineru_missing_middle_json_raises(fake_pdf, monkeypatch):
    _install_fake_mineru(monkeypatch, lambda **kw: None)  # 不写任何文件
    with pytest.raises(MagicPdfParseError, match="middle.json"):
        MagicPdfAdapter()._parse_mineru(fake_pdf)


# ── do_parse 关键字参数签名过滤 ─────────────────────────────────────────────


def test_find_mineru_middle_json_nested(tmp_path):
    # 实测 3.4.5 产物位于 {output_dir}/{stem}/{parse_method}/ 子目录
    nested = tmp_path / "paper" / "auto"
    nested.mkdir(parents=True)
    target = nested / "paper_middle.json"
    target.write_text("{}", encoding="utf-8")
    assert _find_mineru_middle_json(str(tmp_path)) == str(target)


def test_find_mineru_middle_json_missing_returns_none(tmp_path):
    assert _find_mineru_middle_json(str(tmp_path)) is None


def test_build_do_parse_kwargs_filters_unknown_params():
    def do_parse(output_dir, pdf_file_names, backend):  # noqa: ARG001
        pass

    kw = _build_do_parse_kwargs(
        do_parse,
        {
            "output_dir": "o",
            "backend": "pipeline",
            "f_dump_middle_json": True,  # 新版本形参，旧签名没有
        },
    )
    assert set(kw) == {"output_dir", "backend"}


def test_build_do_parse_kwargs_var_keyword_passthrough():
    def do_parse(**kw):  # noqa: ARG001
        pass

    wanted = {"f_dump_middle_json": True, "effort": "high"}
    assert _build_do_parse_kwargs(do_parse, wanted) == wanted


def test_build_do_parse_kwargs_unprobeable_signature_returns_all():
    # 不可探测签名（非可调用对象）→ 原样返回，交由调用方降级路径兜底
    wanted = {"output_dir": "o"}
    assert _build_do_parse_kwargs(42, wanted) == wanted


# ── magicpdf_cli：detail 升格为完整进度事件 ─────────────────────────────────


def test_make_parse_progress_none_passthrough():
    assert _make_parse_progress(None, "a.pdf") is None


def test_parse_progress_page_interpolation_monotone():
    events: list[tuple] = []
    cb = _make_parse_progress(
        lambda stage, pct, msg, detail=None: events.append(
            (stage, pct, msg, detail)
        ),
        r"C:\dir\paper.pdf",
    )
    cb({"unit": "page", "current": 0, "total": 100})
    cb({"unit": "page", "current": 50, "total": 100})
    cb({"unit": "page", "current": 100, "total": 100})

    stages = [e[0] for e in events]
    assert stages == ["analyzing"] * 3
    assert events[0][1] == _PCT_PARSE_START
    assert events[-1][1] == _PCT_PARSE_END
    pcts = [e[1] for e in events]
    assert pcts == sorted(pcts)  # 单调不回退
    assert "analyzing page 50/100" in events[1][2]
    assert events[1][3]["current"] == 50


def test_parse_progress_component_keeps_pct():
    events: list[tuple] = []
    cb = _make_parse_progress(
        lambda stage, pct, msg, detail=None: events.append((stage, pct, msg)),
        "paper.pdf",
    )
    cb({"unit": "page", "current": 40, "total": 80})
    pct_after_pages = events[-1][1]
    cb({"unit": "component", "component": "model init cost"})
    assert events[-1][0] == "analyzing"
    assert events[-1][1] == pct_after_pages  # 组件事件不推进百分比
    assert "model init cost" in events[-1][2]


def test_run_magicpdf_main_accepts_progress_cb():
    sig = inspect.signature(run_magicpdf_main)
    assert "progress_cb" in sig.parameters
    assert sig.parameters["progress_cb"].default is None


# ── 服务层贯通：magicpdf 路径经 _emit_smooth 写入快照 ────────────────────────


def _service(tid: str):
    from pdf2zh.services.runtime_service import RuntimeService

    svc = RuntimeService()
    svc._sweeper = None
    svc._store.create_task(tid)
    return svc


def test_magicpdf_forwarder_writes_snapshot_and_event():
    svc = _service("t_gp_p1")
    detail = {
        "engine": "magicpdf", "raw_stage": "doc_analyze",
        "unit": "page", "current": 320, "total": 800,
    }
    svc._emit_smooth(
        "t_gp_p1", "analyzing", 32.25, "paper.pdf: analyzing page 320/800",
        detail=detail,
    )
    state = svc.get_task_state("t_gp_p1")
    assert state.stage_detail == detail
    events = svc._store.get_events("t_gp_p1")
    assert events[-1].detail == detail
    assert events[-1].stage == "analyzing"
