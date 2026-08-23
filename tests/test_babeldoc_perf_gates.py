"""BabelDOC 大文档提速修复（P0）回归测试。

对应 ``doc/babeldoc_large_doc_slow_progress_report.md`` §四：

1. 伪代码保护三态开关（``PDF2ZH_BABELDOC_PSEUDO_PROTECT``）与页数门控
   （``PDF2ZH_BABELDOC_PSEUDO_PROTECT_MAX_PAGES``，默认 30）——
   大文档跳过每页双模型推理（报告 §2.3）；
2. 健康文本层信任预检、跳过 BabelDOC 内部 SSIM 二次扫描检测
   （``PDF2ZH_BABELDOC_TRUST_PREFLIGHT``，报告 §2.5）——混合扫描文档
   （任一页无文本层）绝不跳过；
3. CPU 回退时的一次性 GPU 加速提示。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pdf2zh.babeldoc_ocr_mode as bom
import pdf2zh.doclayout_pseudocode as dlp


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """清掉相关环境变量，避免跨测试泄漏。"""
    for name in (
        dlp.PSEUDO_PROTECT_ENV,
        dlp.PSEUDO_PROTECT_MAX_PAGES_ENV,
        bom._ENV_OCR_MODE,
        bom._ENV_TRUST_PREFLIGHT,
    ):
        monkeypatch.delenv(name, raising=False)


# ── P0-1：伪代码保护开关与页数门控 ───────────────────────────────────────────


class TestPseudoProtectGate:
    def test_mode_resolution(self, monkeypatch):
        assert dlp.get_pseudo_code_protect_mode() == "auto"
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_ENV, "on")
        assert dlp.get_pseudo_code_protect_mode() == "on"
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_ENV, "OFF")
        assert dlp.get_pseudo_code_protect_mode() == "off"
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_ENV, "garbage")
        assert dlp.get_pseudo_code_protect_mode() == "auto"

    def test_gate_off_on_auto(self, monkeypatch):
        monkeypatch.setattr(dlp, "_pdf_page_count", lambda p: 500)
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_ENV, "off")
        assert dlp.is_pseudo_code_protection_active("/tmp/x.pdf") == (
            False, f"{dlp.PSEUDO_PROTECT_ENV}=off",
        )
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_ENV, "on")
        active, reason = dlp.is_pseudo_code_protection_active("/tmp/x.pdf")
        assert active and "=on" in reason

    def test_auto_gates_by_page_count(self, monkeypatch):
        counts = iter([10, 31])
        monkeypatch.setattr(
            dlp, "_pdf_page_count", lambda p: next(counts),
        )
        # 小文档：启用
        active, reason = dlp.is_pseudo_code_protection_active("/tmp/small.pdf")
        assert active and "<= 30" in reason
        # 超过默认上限：跳过
        active, reason = dlp.is_pseudo_code_protection_active("/tmp/big.pdf")
        assert not active and "> 30" in reason

    def test_auto_page_cap_env_override(self, monkeypatch):
        monkeypatch.setattr(dlp, "_pdf_page_count", lambda p: 100)
        monkeypatch.setenv(dlp.PSEUDO_PROTECT_MAX_PAGES_ENV, "200")
        active, _ = dlp.is_pseudo_code_protection_active("/tmp/x.pdf")
        assert active

    def test_unknown_page_count_keeps_protection(self, monkeypatch):
        monkeypatch.setattr(dlp, "_pdf_page_count", lambda p: None)
        active, reason = dlp.is_pseudo_code_protection_active("/tmp/x.pdf")
        assert active and "unknown" in reason

    def test_build_returns_none_when_gated_off(self, monkeypatch):
        """被门控禁用时必须返回 None 且不触碰任何模型加载。"""
        monkeypatch.setattr(dlp, "_pdf_page_count", lambda p: 999)
        sentinel_called = []

        def _boom(*a, **k):  # pragma: no cover -- 被禁用后不应到达
            sentinel_called.append(True)
            raise AssertionError("model builder must not run when gated off")

        monkeypatch.setattr(dlp, "_build_with_mineru_or_paddle", _boom)
        monkeypatch.setattr(dlp, "_load_base_layout_model", _boom)
        assert dlp.build_pseudo_code_protected_layout_model(
            pdf_path="/tmp/big.pdf"
        ) is None
        assert not sentinel_called

    def test_build_passes_through_when_enabled(self, monkeypatch):
        monkeypatch.setattr(dlp, "_pdf_page_count", lambda p: 5)
        sentinel = object()
        monkeypatch.setattr(
            dlp, "_build_with_mineru_or_paddle", lambda p: sentinel,
        )
        assert dlp.build_pseudo_code_protected_layout_model(
            pdf_path="/tmp/small.pdf"
        ) is sentinel


# ── P0-2：健康文本层跳过 BabelDOC 二次扫描检测 ───────────────────────────────


def _write_pdf(tmp_path, texts_per_page, filename="t.pdf"):
    """用 pymupdf 生成测试 PDF：texts_per_page[i] 为第 i 页文本。"""
    import pymupdf

    path = tmp_path / filename
    with pymupdf.open() as doc:
        for text in texts_per_page:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        doc.save(str(path))
    return str(path)


class TestTrustPreflightSkip:
    def _decision(self, is_scanned):
        from pdf2zh.scanned_detection import ScanDecision

        return ScanDecision(is_scanned=is_scanned)

    def test_scanned_still_forces_ocr(self, monkeypatch, tmp_path):
        pdf = _write_pdf(tmp_path, ["hello world " * 8])
        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check",
            lambda p, **k: self._decision(True),
        )
        assert bom.resolve_ocr_flags("auto", source_path=pdf) == (
            True, False, False,
        )

    def test_healthy_text_layer_skips_recheck(self, monkeypatch, tmp_path):
        pdf = _write_pdf(tmp_path, ["hello world " * 8] * 3)
        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check",
            lambda p, **k: self._decision(False),
        )
        assert bom.resolve_ocr_flags("auto", source_path=pdf) == (
            False, False, True,
        )

    def test_mixed_doc_never_skips(self, monkeypatch, tmp_path):
        """任一页无文本层（混合扫描文档）→ 不跳过，回退 auto 检测。"""
        pdf = _write_pdf(tmp_path, ["hello world " * 8, "", "more text " * 8])
        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check",
            lambda p, **k: self._decision(False),
        )
        assert bom.resolve_ocr_flags("auto", source_path=pdf) == (
            False, True, False,
        )

    def test_trust_preflight_zero_restores_old_behavior(
        self, monkeypatch, tmp_path,
    ):
        pdf = _write_pdf(tmp_path, ["hello world " * 8])
        monkeypatch.setenv(bom._ENV_TRUST_PREFLIGHT, "0")
        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check",
            lambda p, **k: self._decision(False),
        )
        assert bom.resolve_ocr_flags("auto", source_path=pdf) == (
            False, True, False,
        )

    def test_preflight_failure_keeps_auto(self, monkeypatch, tmp_path):
        pdf = _write_pdf(tmp_path, ["hello world " * 8])

        def _boom(p, **k):
            raise RuntimeError("preflight down")

        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check", _boom,
        )
        assert bom.resolve_ocr_flags("auto", source_path=pdf) == (
            False, True, False,
        )

    def test_non_pdf_source_untouched(self, monkeypatch):
        monkeypatch.setattr(
            "pdf2zh.scanned_detection.preflight_scan_check",
            lambda p, **k: (_ for _ in ()).throw(AssertionError("called")),
        )
        assert bom.resolve_ocr_flags("auto", source_path="/tmp/x.docx") == (
            False, True, False,
        )


# ── P0-3：CPU 回退一次性 GPU 提示 ────────────────────────────────────────────


class TestGpuHint:
    def test_hint_logged_once(self, caplog):
        import logging

        from pdf2zh import babeldoc_onnx_backend as bob

        with caplog.at_level(logging.INFO, logger="pdf2zh.babeldoc_onnx_backend"):
            bob._GPU_HINT_LOGGED = False
            bob._log_gpu_acceleration_hint(["CPUExecutionProvider"])
            bob._log_gpu_acceleration_hint(["CPUExecutionProvider"])
        msgs = [r.message for r in caplog.records
                if "layout inference is running on CPU" in r.message]
        assert len(msgs) == 1
        assert "PDF2ZH_BABELDOC_BACKEND" in msgs[0]
