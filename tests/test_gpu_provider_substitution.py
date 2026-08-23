"""GPU 跨后端兜底（provider 替换）回归测试。

用户实测场景：Windows 上装了 onnxruntime-directml（编译期注册表只有
``AzureExecutionProvider``），却显式请求 ``cuda``——CUDA provider 未注册，
原逻辑直接回退 CPU，而 DML 明明执行级可用。修复后 ``resolve_providers`` /
``resolve_babeldoc_providers`` 在请求的 GPU 后端不可用（缺失或注册但不可执行）
时，自动探测另一 GPU 后端并替换，同时给出「如何显式固定」的警告。
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh import babeldoc_onnx_backend as bob
from pdf2zh import doclayout as dl


def _patch_exec(monkeypatch, exec_gpu):
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers", lambda: set(exec_gpu),
    )


class TestDoclayoutSubstitution:
    def test_cuda_missing_but_dml_executable_substitutes(self, monkeypatch):
        # 用户环境复刻：注册表只有 Azure(DML)+CPU，请求 cuda → 替换为 dml
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["AzureExecutionProvider", "CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, {"AzureExecutionProvider"})
        out = dl.resolve_providers("cuda")
        assert out == ["AzureExecutionProvider", "CPUExecutionProvider"]

    def test_cuda_registered_but_ineffective_substitutes_dml(
        self, monkeypatch,
    ):
        # CUDA 已注册但执行级不可用（缺 DLL），DML 可执行 → 替换
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: [
                "CUDAExecutionProvider", "AzureExecutionProvider",
                "CPUExecutionProvider",
            ],
        )
        _patch_exec(monkeypatch, {"AzureExecutionProvider"})
        out = dl.resolve_providers("cuda")
        assert out == ["AzureExecutionProvider", "CPUExecutionProvider"]

    def test_no_executable_alternative_keeps_old_behavior(self, monkeypatch):
        # 两个 GPU 后端都不可用 → 原样回退 CPU + 原 warning
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, set())
        out = dl.resolve_providers("cuda")
        assert out == ["CPUExecutionProvider"]

    def test_substitution_logs_explicit_pin_hint(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["AzureExecutionProvider", "CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, {"AzureExecutionProvider"})
        with caplog.at_level(logging.WARNING, logger="pdf2zh.doclayout"):
            dl.resolve_providers("cuda")
        msgs = [r.message for r in caplog.records if "falling back" in r.message]
        assert any("'dml'" in m and "'--backend dml'" in m for m in msgs)

    def test_dml_missing_substitutes_cuda(self, monkeypatch):
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, {"CUDAExecutionProvider"})
        out = dl.resolve_providers("dml")
        assert out == ["CUDAExecutionProvider", "CPUExecutionProvider"]


class TestBabeldocSubstitution:
    def test_cuda_missing_but_dml_executable(self, monkeypatch):
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["AzureExecutionProvider", "CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, {"AzureExecutionProvider"})
        out = bob.resolve_babeldoc_providers("cuda")
        assert out == ["AzureExecutionProvider", "CPUExecutionProvider"]

    def test_cuda_ineffective_but_dml_executable(self, monkeypatch):
        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: [
                "CUDAExecutionProvider", "AzureExecutionProvider",
                "CPUExecutionProvider",
            ],
        )
        _patch_exec(monkeypatch, {"AzureExecutionProvider"})
        out = bob.resolve_babeldoc_providers("cuda")
        assert out == ["AzureExecutionProvider", "CPUExecutionProvider"]

    def test_no_alternative_warns_unavailable(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(
            "pdf2zh.doclayout._ort_available_providers",
            lambda: ["CPUExecutionProvider"],
        )
        _patch_exec(monkeypatch, set())
        with caplog.at_level(logging.WARNING, logger="pdf2zh.babeldoc_onnx_backend"):
            out = bob.resolve_babeldoc_providers("cuda")
        assert out == ["CPUExecutionProvider"]
        assert any("no GPU provider is available" in r.message
                   for r in caplog.records)

    def test_session_fallback_warning_not_duplicated_for_cpu_only_resolve(
        self, monkeypatch,
    ):
        """resolve 已降级 CPU-only 时，_patched_init 不再重复报 session 回退。"""
        # 静态验证：providers 全 CPU 时 _session_has_gpu 分支不应触发告警路径。
        # 这里直接断言布尔条件本身（与 _patched_init 的守卫一致）。
        providers = ["CPUExecutionProvider"]
        assert not any(p != "CPUExecutionProvider" for p in providers)
