"""MagicPDF 解析引擎 OCR 三态开关（auto/on/off）测试。

覆盖 ``pdf2zh.pdf2zh.resolve_magicpdf_ocr_mode`` 与 ``pdf2zh.magicpdf_cli``
中对三态的处理：

- 三态归一化：``--magicpdf-ocr-mode`` 合法值直接生效，非法值回退 ``auto``；
- ``--magicpdf-ocr``（历史 bool 开关）等价 ``on`` 且优先于 mode；
- ``off`` 模式下预检命中扫描/损坏信号也绝不强制开启 OCR；
- ``auto`` 模式下预检命中才自动开启 OCR（历史行为保留）；
- ``on`` 模式强制开启 OCR（不做预检）。
"""

import argparse
import os
import tempfile
from unittest.mock import Mock, patch

from pdf2zh.pdf2zh import resolve_magicpdf_ocr_mode


def make_args(**kw):
    ns = argparse.Namespace(
        files=["paper.pdf"],
        output="",
        pages=None,
        lang_in="en",
        lang_out="zh",
        service="google",
        thread=4,
        no_parallel=False,
        parallel_workers=None,
        vfont="",
        vchar="",
        envs={},
        prompt=None,
        ignore_cache=False,
        compatible=False,
        debug=False,
        dir=False,
        backend="auto",
        mode="fast",
        parse_engine="magicpdf",
        magicpdf_ocr=False,
        magicpdf_ocr_mode="auto",
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# ── resolve_magicpdf_ocr_mode ──────────────────────────────────────────────────


def test_default_is_auto():
    assert resolve_magicpdf_ocr_mode(make_args()) == "auto"


def test_mode_auto_on_off():
    for mode in ("auto", "on", "off"):
        assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr_mode=mode)) == mode


def test_invalid_mode_falls_back_to_auto():
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr_mode="bogus")) == "auto"
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr_mode="")) == "auto"
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr_mode=None)) == "auto"


def test_legacy_bool_ocr_wins_over_mode():
    # --magicpdf-ocr（历史 bool）等价 on 且优先于 mode。
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr=True, magicpdf_ocr_mode="off")) == "on"


def test_legacy_bool_false_uses_mode():
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr=False, magicpdf_ocr_mode="off")) == "off"
    assert resolve_magicpdf_ocr_mode(make_args(magicpdf_ocr=False, magicpdf_ocr_mode="on")) == "on"


def test_missing_attrs_default_to_auto():
    ns = argparse.Namespace(files=[])
    assert resolve_magicpdf_ocr_mode(ns) == "auto"


# ── CLI 解析 ───────────────────────────────────────────────────────────────────


def test_cli_parses_mode_choices():
    from pdf2zh.pdf2zh import parse_args

    args = parse_args(["--parse-engine", "magicpdf", "--magicpdf-ocr-mode", "off", "x.pdf"])
    assert args.magicpdf_ocr_mode == "off"
    assert args.magicpdf_ocr is False
    assert args.files == ["x.pdf"]


# ── run_magicpdf_main 的三态行为 ───────────────────────────────────────────────


def _patch_flow(tmp_path, **ns_kw):
    """搭建 run_magicpdf_main 的最小 mock 链路，捕获传给 adapter.parse 的 ocr。"""
    from pdf2zh.magicpdf_cli import run_magicpdf_main

    pdf_path = os.path.join(str(tmp_path), "paper.pdf")
    with open(pdf_path, "w", encoding="utf-8") as fh:
        fh.write("%PDF-1.4 placeholder")

    captured = {}

    class _FakeAdapter:
        def __init__(self, *a, **k):
            pass

        @classmethod
        def is_available(cls):
            return True

        def parse(self, path, pages=None, ocr=False):
            captured["ocr"] = ocr
            return []

    class _FakeDoc:
        pages = []
        to_dict = lambda self: {"pages": []}

    fake_translator = Mock()
    fake_translator.translate = Mock(side_effect=lambda t: "T:" + t)

    with tempfile.TemporaryDirectory() as tmp2:
        args = make_args(files=[pdf_path], output=tmp2, **ns_kw)
        with patch(
            "pdf2zh.magicpdf_adapter.MagicPdfAdapter", _FakeAdapter
        ), patch(
            "pdf2zh.v3.magicpdf_bridge.MagicPdfBridge.to_document_model",
            return_value=_FakeDoc(),
        ), patch(
            "pdf2zh.v3.magicpdf_bridge.MagicPdfBridge.convert_all",
            return_value=[],
        ), patch(
            "pdf2zh.translator.build_translator", return_value=fake_translator,
        ), patch(
            "pdf2zh.v3.document_model.translate_document",
            return_value={"translated": 0, "preserved": 0},
        ), patch(
            "pdf2zh.v3.document_model.render_plan_from_model",
            return_value=([], Mock()),
        ), patch(
            "pdf2zh.v3.render_takeover.fixup_render_plan",
            return_value=([], {}),
        ), patch(
            "pdf2zh.scanned_detection.preflight_scan_check",
            return_value=Mock(is_scanned=True, reasons=["font_to_unicode: 1.000 >= 0.60"]),
        ):
            run_magicpdf_main(args)
    return captured


def test_off_mode_never_auto_enables_ocr(tmp_path):
    """off 模式下即使预检命中扫描/损坏信号也绝不开启 OCR。"""
    captured = _patch_flow(tmp_path, magicpdf_ocr_mode="off")
    assert captured["ocr"] is False


def test_on_mode_forces_ocr(tmp_path):
    """on 模式强制开启 OCR，不做预检。"""
    captured = _patch_flow(tmp_path, magicpdf_ocr_mode="on")
    assert captured["ocr"] is True


def test_auto_mode_enables_ocr_on_scanned_hit(tmp_path):
    """auto 模式下预检命中扫描/损坏信号自动开启 OCR（历史行为）。"""
    captured = _patch_flow(tmp_path, magicpdf_ocr_mode="auto")
    assert captured["ocr"] is True


def test_legacy_bool_flag_means_on(tmp_path):
    """历史 --magicpdf-ocr（bool=True）等价 on。"""
    captured = _patch_flow(tmp_path, magicpdf_ocr=True, magicpdf_ocr_mode="auto")
    assert captured["ocr"] is True


# ── _try_auto_switch_magicpdf 尊重 off ─────────────────────────────────────────


def test_auto_switch_skipped_when_off(monkeypatch):
    """用户显式 off 时，legacy 预检命中扫描信号也不自动切换 magicpdf。"""
    from pdf2zh.pdf2zh import _try_auto_switch_magicpdf

    ns = make_args(files=["paper.pdf"], parse_engine="legacy", magicpdf_ocr_mode="off")
    switched = _try_auto_switch_magicpdf(ns)
    assert switched is False
    assert getattr(ns, "parse_engine") == "legacy"
    # 不应被改写为强制 OCR
    assert getattr(ns, "magicpdf_ocr", False) is False


def test_auto_switch_still_happens_on_auto(monkeypatch):
    """auto（默认）模式：预检命中 + magicpdf 可用 → 自动切换并开启 OCR。"""
    from pdf2zh.pdf2zh import _try_auto_switch_magicpdf

    ns = make_args(files=["paper.pdf"], parse_engine="legacy", magicpdf_ocr_mode="auto")
    with patch(
        "pdf2zh.scanned_detection.preflight_scan_check",
        return_value=Mock(is_scanned=True, reasons=["scan"]),
    ), patch(
        "pdf2zh.engine_env.available_backend",
        return_value=("cpu", True),
    ):
        switched = _try_auto_switch_magicpdf(ns)
    assert switched is True
    assert ns.parse_engine == "magicpdf"


def test_cli_legacy_bool_flag():
    from pdf2zh.pdf2zh import parse_args

    args = parse_args(["--parse-engine", "magicpdf", "--magicpdf-ocr", "x.pdf"])
    assert args.magicpdf_ocr is True
    assert args.magicpdf_ocr_mode == "auto"
    assert resolve_magicpdf_ocr_mode(args) == "on"


def test_cli_defaults():
    from pdf2zh.pdf2zh import parse_args

    args = parse_args(["x.pdf"])
    assert args.magicpdf_ocr is False
    assert args.magicpdf_ocr_mode == "auto"
