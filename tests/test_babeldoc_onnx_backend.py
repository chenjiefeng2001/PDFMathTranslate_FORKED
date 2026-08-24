"""BabelDOC 内部 ONNX 后端的 GPU（CUDA/DML）适配开关测试。

覆盖 ``pdf2zh/babeldoc_onnx_backend.py``：

- ``resolve_babeldoc_providers``：auto 保持 BabelDOC 原生 CPU-only；显式
  cuda/dml 时 GPU provider 优先 + CPU 兜底；GPU 不可用回退 CPU。
- ``get_babeldoc_backend``：``PDF2ZH_BABELDOC_BACKEND`` 环境变量优先级、
  ``auto``/非法取值处理。
- ``apply_babeldoc_backend`` / ``reset_babeldoc_backend``：幂等补丁与恢复。
- ``_patched_init``：cuda 下用 GPU provider 建会话、auto 走原始实现、
  GPU 会话失败自动回退 CPU。
- ``set_backend`` 会把补丁同步到 BabelDOC（端到端链路）。
- ``PaddleDocLayoutV2Detector`` 遵循后端开关（真实迷你 ONNX 模型冒烟）。
"""

import threading

import pytest

import pdf2zh.babeldoc_onnx_backend as bobe
from pdf2zh.doclayout import get_backend, set_backend


@pytest.fixture(autouse=True)
def _isolate_backends():
    """每个测试后恢复 pdf2zh 后端选择与 babeldoc 补丁状态，避免跨测试泄漏。"""
    import os

    old_backend = get_backend()
    old_env = os.environ.get(bobe._ENV_BACKEND)
    saved_orig = None
    try:
        from babeldoc.docvision.doclayout import OnnxModel as BabelOnnxModel

        saved_orig = BabelOnnxModel.__init__
    except Exception:  # noqa: BLE001 -- babeldoc 缺失时测试仍可跑
        saved_orig = None
    yield
    # 1) 恢复 pdf2zh 后端选择（可能重新 apply 补丁）；
    set_backend(old_backend if old_backend else "auto")
    # 2) 强制恢复 babeldoc 类与模块全局（防御测试中把 _ORIGINAL_INIT 换成了桩）。
    if saved_orig is not None:
        try:
            BabelOnnxModel.__init__ = saved_orig
        except Exception:  # noqa: BLE001
            pass
    bobe._ORIGINAL_INIT = None
    # 3) 恢复环境变量。
    if old_env is None:
        os.environ.pop(bobe._ENV_BACKEND, None)
    else:
        os.environ[bobe._ENV_BACKEND] = old_env


def _patch_providers(monkeypatch, providers):
    """把 provider 列表来源固定为给定列表（不依赖 onnxruntime 顶层属性）。

    onnxruntime 1.20.x 顶层缺少 ``get_available_providers``（1.21+ 修复），
    这里直接 patch ``pdf2zh.doclayout._ort_available_providers`` 兼容封装。
    """
    monkeypatch.setattr(
        "pdf2zh.doclayout._ort_available_providers", lambda: list(providers)
    )


# ── resolve_babeldoc_providers ──────────────────────────────────────────────


def test_auto_keeps_cpu_only_even_with_gpu_available(monkeypatch):
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    set_backend("auto")
    assert bobe.resolve_babeldoc_providers("auto") == ["CPUExecutionProvider"]
    assert bobe.resolve_babeldoc_providers(None) == ["CPUExecutionProvider"]


def test_cuda_maps_to_cuda_then_cpu(monkeypatch):
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers", lambda: {"CUDAExecutionProvider"}
    )
    providers = bobe.resolve_babeldoc_providers("cuda")
    assert providers == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_cuda_registered_but_ineffective_falls_back_to_cpu(monkeypatch):
    # CUDA 已注册但执行级探测判定无效（缺运行库 DLL，ORT 静默回退 CPU）：
    # 必须回退 CPU-only 并给出会话级警告，而不是创建无效 GPU 会话。
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr("pdf2zh.doclayout._exec_gpu_providers", lambda: set())
    captured = []
    monkeypatch.setattr(
        "pdf2zh.doclayout.warn_gpu_session_fallback",
        lambda backend, requested, effective: captured.append(backend),
    )
    providers = bobe.resolve_babeldoc_providers("cuda")
    assert providers == ["CPUExecutionProvider"]
    assert captured == ["cuda"]


def test_cuda_falls_back_to_cpu_when_unavailable(monkeypatch):
    _patch_providers(monkeypatch, ["CPUExecutionProvider"])
    assert bobe.resolve_babeldoc_providers("cuda") == ["CPUExecutionProvider"]


def test_cuda_unavailable_logs_warning(monkeypatch):
    # 显式请求 cuda 但 GPU provider 缺失：CPU 兜底项使交集非空，
    # 必须记录明确警告（含 onnxruntime-gpu 安装提示），而非静默回退。
    _patch_providers(monkeypatch, ["CPUExecutionProvider"])
    with monkeypatch.context() as m:
        captured = []
        m.setattr(
            "pdf2zh.doclayout.warn_gpu_unavailable",
            lambda backend, wanted, available: captured.append(backend),
        )
        providers = bobe.resolve_babeldoc_providers("cuda")
    assert providers == ["CPUExecutionProvider"]
    assert captured == ["cuda"]


def test_dml_missing_logs_warning(monkeypatch):
    _patch_providers(monkeypatch, ["CPUExecutionProvider"])
    with monkeypatch.context() as m:
        captured = []
        m.setattr(
            "pdf2zh.doclayout.warn_gpu_unavailable",
            lambda backend, wanted, available: captured.append(backend),
        )
        providers = bobe.resolve_babeldoc_providers("dml")
    assert providers == ["CPUExecutionProvider"]
    assert captured == ["dml"]


def test_dml_uses_new_azure_name(monkeypatch):
    _patch_providers(monkeypatch, ["AzureExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers",
        lambda: {"AzureExecutionProvider"},
    )
    assert bobe.resolve_babeldoc_providers("dml") == [
        "AzureExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_dml_falls_back_to_legacy_name(monkeypatch):
    _patch_providers(monkeypatch, ["DmlExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers",
        lambda: {"DmlExecutionProvider"},
    )
    assert bobe.resolve_babeldoc_providers("dml") == [
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_cpu_backend_is_strictly_cpu(monkeypatch):
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    assert bobe.resolve_babeldoc_providers("cpu") == ["CPUExecutionProvider"]


def test_unknown_backend_uses_cpu(monkeypatch):
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    assert bobe.resolve_babeldoc_providers("tpu") == ["CPUExecutionProvider"]


# ── get_babeldoc_backend（环境变量优先级） ──────────────────────────────────


def test_env_var_overrides_set_backend(monkeypatch):
    monkeypatch.setenv(bobe._ENV_BACKEND, "cuda")
    set_backend("cpu")
    assert bobe.get_babeldoc_backend() == "cuda"


def test_env_var_auto_returns_none(monkeypatch):
    monkeypatch.setenv(bobe._ENV_BACKEND, "auto")
    set_backend("dml")
    assert bobe.get_babeldoc_backend() is None


def test_env_var_invalid_falls_through_to_set_backend(monkeypatch):
    monkeypatch.setenv(bobe._ENV_BACKEND, "tpu")
    set_backend("dml")
    assert bobe.get_babeldoc_backend() == "dml"


def test_no_env_follows_set_backend():
    set_backend("cuda")
    assert bobe.get_babeldoc_backend() == "cuda"
    set_backend("auto")
    assert bobe.get_babeldoc_backend() is None


# ── apply / reset 补丁 ──────────────────────────────────────────────────────


def test_apply_is_idempotent_and_reset_restores():
    from babeldoc.docvision.doclayout import OnnxModel as BabelOnnxModel

    orig = BabelOnnxModel.__init__
    try:
        assert bobe.apply_babeldoc_backend() is True
        assert bobe.apply_babeldoc_backend() is True  # 幂等
        assert BabelOnnxModel.__init__ is bobe._patched_init
        assert bobe._ORIGINAL_INIT is orig
    finally:
        assert bobe.reset_babeldoc_backend() is True
    assert BabelOnnxModel.__init__ is orig
    assert bobe._ORIGINAL_INIT is None


def test_set_backend_syncs_patch_to_babeldoc():
    """CLI --backend 链路：set_backend 必须同时把补丁打到 BabelDOC 类上。"""
    from babeldoc.docvision.doclayout import OnnxModel as BabelOnnxModel

    assert bobe._ORIGINAL_INIT is None  # fixture 已复位
    set_backend("auto")
    assert bobe._ORIGINAL_INIT is not None
    assert BabelOnnxModel.__init__ is bobe._patched_init


# ── _patched_init 会话构造 ──────────────────────────────────────────────────


class _FakeMetadataProp:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class _FakeModel:
    metadata_props = [
        _FakeMetadataProp("stride", "32"),
        _FakeMetadataProp("names", "{0: 'text', 1: 'figure'}"),
    ]

    def SerializeToString(self):
        return b"fake-model-bytes"


class _FakeSession:
    def __init__(self, providers_arg):
        self.providers_arg = list(providers_arg)

    def get_providers(self):
        return list(self.providers_arg)


class _Dummy:
    """带 __dict__ 的最小实例载体（object.__new__(object) 无法挂属性）。"""


def _install_fake_onnx(monkeypatch):
    """把 onnx.load / onnxruntime.InferenceSession 换成轻量桩。"""
    import onnx
    import onnxruntime

    monkeypatch.setattr(onnx, "load", lambda _path: _FakeModel())
    monkeypatch.setattr(
        onnxruntime,
        "InferenceSession",
        lambda path, opts=None, providers=None: _FakeSession(providers),
    )


def test_patched_init_uses_gpu_providers(monkeypatch):
    _install_fake_onnx(monkeypatch)
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers", lambda: {"CUDAExecutionProvider"}
    )
    set_backend("cuda")
    assert bobe.apply_babeldoc_backend() is True

    obj = _Dummy()
    bobe._patched_init(obj, "fake.onnx")
    assert obj.model.providers_arg == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    assert obj._stride == 32
    assert obj._names == {0: "text", 1: "figure"}
    # 跨版本：threading.Lock 在 3.13+ 是类、3.12- 是工厂函数，
    # isinstance(x, threading.Lock) 在旧版本会直接 TypeError。
    assert isinstance(obj.lock, type(threading.Lock()))


def test_patched_init_warns_when_session_falls_back_to_cpu(monkeypatch):
    """注册表有 CUDAExecutionProvider 但会话实际只跑到 CPU（缺 CUDA/cuDNN
    运行库 DLL 的典型场景）：必须给出明确警告，而非静默回退。"""
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(
        "pdf2zh.doclayout._exec_gpu_providers", lambda: {"CUDAExecutionProvider"}
    )
    set_backend("cuda")
    assert bobe.apply_babeldoc_backend() is True

    def _fake_init(self_, model_path, providers):
        # 模拟 ORT 创建会话时 CUDA 初始化失败 → 实际生效只有 CPU
        self_.model = _FakeSession(["CPUExecutionProvider"])
        self_._stride = 32
        self_._names = {0: "text", 1: "figure"}
        self_.lock = threading.Lock()

    monkeypatch.setattr(bobe, "_init_with_providers", _fake_init)
    with monkeypatch.context() as m:
        captured = []
        m.setattr(
            "pdf2zh.doclayout.warn_gpu_session_fallback",
            lambda backend, requested, effective: captured.append(backend),
        )
        obj = _Dummy()
        bobe._patched_init(obj, "fake.onnx")
    assert captured == ["cuda"]


def test_patched_init_auto_uses_gpu_when_available(monkeypatch):
    """auto 语义与主链路一致：CUDA 注册且执行级可用 → BabelDOC 内部也走 GPU。

    修复前 auto 一律回退 BabelDOC 原生 CPU-only 初始化，导致 GUI 默认后端
    （auto）下"主链路 doclayout 走 GPU、BabelDOC 内部 doclayout 仍 CPU"的
    撕裂；修复后 auto 复用 pdf2zh.doclayout.resolve_providers(None) 的
    执行级探测结果。
    """
    _install_fake_onnx(monkeypatch)
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    set_backend("auto")
    assert bobe.apply_babeldoc_backend() is True

    obj = _Dummy()
    bobe._patched_init(obj, "fake.onnx")
    assert obj.model.providers_arg == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_patched_init_auto_delegates_to_original_when_no_gpu(monkeypatch):
    """无可执行 GPU 时 auto 回退 BabelDOC 原生初始化（保持 CPU / CoreML 行为）。"""
    _patch_providers(monkeypatch, ["CPUExecutionProvider"])
    set_backend("auto")
    assert bobe.apply_babeldoc_backend() is True

    calls = []

    def _fake_original(self_, path):
        calls.append(path)

    monkeypatch.setattr(bobe, "_ORIGINAL_INIT", _fake_original)
    obj = object.__new__(object)
    bobe._patched_init(obj, "fake.onnx")
    assert calls == ["fake.onnx"]
    assert not hasattr(obj, "model")  # 未走 GPU 分支


def test_patched_init_falls_back_when_gpu_session_fails(monkeypatch):
    import onnx
    import onnxruntime

    monkeypatch.setattr(onnx, "load", lambda _path: _FakeModel())

    def _boom(path, opts=None, providers=None):
        raise RuntimeError("CUDA session init failed")

    monkeypatch.setattr(onnxruntime, "InferenceSession", _boom)
    _patch_providers(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    set_backend("cuda")
    assert bobe.apply_babeldoc_backend() is True

    calls = []

    def _fake_original(self_, path):
        calls.append(path)

    monkeypatch.setattr(bobe, "_ORIGINAL_INIT", _fake_original)
    obj = object.__new__(object)
    bobe._patched_init(obj, "fake.onnx")
    assert calls == ["fake.onnx"]


# ── PaddleDocLayoutV2Detector 集成冒烟 ──────────────────────────────────────


def test_paddle_detector_follows_backend_switch(monkeypatch, tmp_path):
    """cuda 不可用时 PP-DocLayoutV2 检测器仍能用 CPU 加载迷你模型。"""
    import onnx
    from onnx import TensorProto, helper

    x = helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 800, 800])
    y = helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, 300, 8])
    node = helper.make_node("Identity", ["image"], ["out"])
    graph = helper.make_graph([node], "mini", [x], [y])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    # 兼容旧版 onnxruntime（1.20.x 仅支持 IR <= 10）
    model.ir_version = 10
    model_path = tmp_path / "mini.onnx"
    onnx.save(model, str(model_path))

    from pdf2zh.doclayout_pseudocode import PaddleDocLayoutV2Detector

    # 用真实 onnxruntime 可用 provider（不造假桩），请求 cuda：本机无 CUDA
    # provider 时解析器应回退 CPU，检测器仍能正常加载会话。
    set_backend("cuda")
    det = PaddleDocLayoutV2Detector(model_path)
    assert det._session is not None
    assert "CPUExecutionProvider" in det._session.get_providers()
