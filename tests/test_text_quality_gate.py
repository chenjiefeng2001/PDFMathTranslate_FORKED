"""scan_damaged 报告 §6.1/§6.2 落地——文本质量预检 gate 与 magicpdf 自动切换测试。

覆盖：
- high_level._run_text_quality_gate：无文件路径 / 命中损坏信号时写
  v3_output["text_quality"] 并输出警告；
- pdf2zh._try_auto_switch_magicpdf：命中扫描信号且 magic-pdf 可用 → 自动
  切换 parse_engine=magicpdf + OCR；不可用 → 仅警告；env 关闭 / 防重入。
"""
import argparse
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from pdf2zh.high_level import _run_text_quality_gate
from pdf2zh.pdf2zh import _try_auto_switch_magicpdf


def make_decision(scanned=True, reasons=None):
    d = MagicMock()
    d.is_scanned = scanned
    d.reasons = reasons or ["cid_ratio"]
    d.to_dict.return_value = {"is_scanned": scanned,
                              "reasons": d.reasons}
    return d


class TestTextQualityGate(unittest.TestCase):
    def test_bytesio_without_path_writes_empty(self):
        v3 = {}
        _run_text_quality_gate(io.BytesIO(b"%PDF"), v3)
        self.assertIn("text_quality", v3)
        self.assertFalse(v3["text_quality"]["scanned"])

    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_scanned_hit_writes_decision(self, mock_pre):
        mock_pre.return_value = make_decision(scanned=True,
                                              reasons=["cid_ratio", "fffd"])
        v3 = {}
        with patch("pdf2zh.high_level.os.path.exists", return_value=True):
            class FakeFile(io.BytesIO):
                name = "suspicious.pdf"

            _run_text_quality_gate(FakeFile(b"%PDF"), v3)
        tq = v3["text_quality"]
        self.assertTrue(tq["scanned"])
        self.assertEqual(tq["reasons"], ["cid_ratio", "fffd"])
        self.assertTrue(tq["preflight"]["is_scanned"])

    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_preflight_exception_is_safe(self, mock_pre):
        mock_pre.side_effect = RuntimeError("boom")

        class FakeFile(io.BytesIO):
            name = "ok.pdf"

        v3 = {}
        with patch("pdf2zh.high_level.os.path.exists", return_value=True):
            _run_text_quality_gate(FakeFile(b"%PDF"), v3)
        self.assertIn("text_quality", v3)


def make_args(**kw):
    ns = argparse.Namespace(
        files=[], babeldoc=False, parse_engine="auto",
        magicpdf_ocr=False, _auto_switch_attempted=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestAutoSwitchMagicpdf(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PDF2ZH_AUTO_SWITCH_MAGICPDF", None)

    def test_env_off_returns_false(self):
        os.environ["PDF2ZH_AUTO_SWITCH_MAGICPDF"] = "0"
        args = make_args(files=["a.pdf"])
        self.assertFalse(_try_auto_switch_magicpdf(args))

    def test_already_attempted_returns_false(self):
        args = make_args(files=["a.pdf"], _auto_switch_attempted=True)
        self.assertFalse(_try_auto_switch_magicpdf(args))

    @patch("pdf2zh.engine_env.available_backend")
    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_switches_when_scanned_and_engine_available(
            self, mock_pre, mock_backend):
        mock_pre.return_value = make_decision(True, ["fffd_ratio"])
        mock_backend.return_value = ("magicpdf", True)
        args = make_args(files=["scan.pdf"])
        result = _try_auto_switch_magicpdf(args)
        self.assertTrue(result)
        self.assertEqual(args.parse_engine, "magicpdf")
        self.assertTrue(args.magicpdf_ocr)
        self.assertTrue(args._auto_switch_attempted)

    @patch("pdf2zh.engine_env.available_backend")
    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_no_switch_when_engine_unavailable(
            self, mock_pre, mock_backend):
        mock_pre.return_value = make_decision(True, ["cid_ratio"])
        mock_backend.return_value = ("magicpdf", False)
        args = make_args(files=["scan.pdf"])
        result = _try_auto_switch_magicpdf(args)
        self.assertFalse(result)
        self.assertEqual(args.parse_engine, "auto")

    @patch("pdf2zh.engine_env.available_backend")
    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_no_switch_when_not_scanned(self, mock_pre, mock_backend):
        mock_pre.return_value = make_decision(False, [])
        args = make_args(files=["normal.pdf"])
        self.assertFalse(_try_auto_switch_magicpdf(args))
        mock_backend.assert_not_called()

    @patch("pdf2zh.scanned_detection.preflight_scan_check")
    def test_non_pdf_skipped(self, mock_pre):
        args = make_args(files=["notes.docx"])
        self.assertFalse(_try_auto_switch_magicpdf(args))
        mock_pre.assert_not_called()


if __name__ == "__main__":
    unittest.main()
