"""P1 — MinerU 源码锚点（vendor/MinerU 子模块 + 隔离 venv）回归测试。

对应 ``doc/mineru_submodule_feasibility_report.md`` P1：
- .gitmodules 记录了 vendor/MinerU 源码锚点；
- mineru_env 的路径/探测/覆盖逻辑；
- PDF2ZH_MINERU_PYTHON 存在时 _parse_mineru 改走子进程路径；
- worker 脚本契约（stdlib-only、__main__ 防护、参数校验）。
"""

from __future__ import annotations

import importlib
import os
import py_compile
import sys

import pytest

from pdf2zh.kernel import mineru_env
from pdf2zh.magicpdf_adapter import (
    MagicPdfAdapter,
    MagicPdfParseError,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 源码锚点声明 ─────────────────────────────────────────────────────────────


def test_gitmodules_declares_mineru_anchor():
    with open(os.path.join(REPO_ROOT, ".gitmodules"), encoding="utf-8") as fh:
        content = fh.read()
    assert "vendor/MinerU" in content
    assert "opendatalab/MinerU" in content


# ── mineru_env 纯逻辑 ───────────────────────────────────────────────────────


def test_submodule_available_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mineru_env, "_SUBMODULE_DIR", tmp_path)
    assert not mineru_env.submodule_available()


def test_submodule_available_true_when_pinned_source_present(tmp_path):
    (tmp_path / "mineru").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='mineru'\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(mineru_env, "_SUBMODULE_DIR", tmp_path)
    try:
        assert mineru_env.submodule_available()
    finally:
        monkey.undo()


def test_mineru_python_override(monkeypatch):
    monkeypatch.delenv("PDF2ZH_MINERU_PYTHON", raising=False)
    # 抑制隔离 venv 自动探测，模拟「未配置任何 mineru 解释器」
    import pdf2zh.kernel.mineru_env as _me

    monkeypatch.setattr(_me, "default_venv_python", lambda: None)
    assert mineru_python_override_none()

    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", "  ")
    assert mineru_python_override_none()

    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", r"C:\venv\python.exe")
    assert (
        importlib.import_module("pdf2zh.engine_env").mineru_python_override()
        == r"C:\venv\python.exe"
    )


def mineru_python_override_none() -> bool:
    from pdf2zh.engine_env import mineru_python_override

    return mineru_python_override() is None


def test_venv_python_layout(tmp_path):
    py = mineru_env.venv_python(tmp_path)
    if sys.platform == "win32":
        assert py.endswith(("Scripts\\python.exe", "Scripts/python.exe"))
    else:
        assert py.endswith("bin/python")


# ── 子进程解析路径 ───────────────────────────────────────────────────────────


@pytest.fixture()
def _fake_backend_mineru(monkeypatch):
    adapter = MagicPdfAdapter()
    monkeypatch.setattr(adapter, "backend", lambda: "mineru")
    return adapter


_MIDDLE = {
    "pdf_info": [
        {
            "page_size": [612.0, 792.0],
            "para_blocks": [
                {
                    "type": "text",
                    "bbox": [0, 0, 10, 10],
                    "lines": [
                        {
                            "bbox": [0, 0, 10, 10],
                            "spans": [
                                {
                                    "bbox": [0, 0, 10, 10],
                                    "content": "subprocess ok",
                                    "type": "text",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def test_parse_routes_to_subprocess_when_override_set(
    _fake_backend_mineru, monkeypatch
):
    seen: dict = {}

    def fake_run(cmd, timeout, **kw):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        # 模拟 worker：在 out_dir 内产出 middle.json
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    # 指向一个真实存在的解释器（runner 已打桩，不会真正执行）
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    results = _fake_backend_mineru.parse(
        __file__,
        progress_cb=lambda d: None,
    )

    assert seen["cmd"][1].endswith("mineru_worker.py")
    assert seen["cmd"][4] == "auto"  # parse_method 非 OCR 默认
    assert seen["timeout"] >= 3600  # torch 系安装/解析的宽松超时
    assert len(results) == 1
    assert results[0].text() == "subprocess ok"
    assert results[0].backend == "mineru"


def test_parse_subprocess_uses_ocr_flag(_fake_backend_mineru, monkeypatch, tmp_path):
    def fake_run(cmd, timeout, **kw):
        assert cmd[4] == "ocr"
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "ocr")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    results = MagicPdfAdapter()._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        ocr=True,
        python_exe="unused-python",
        out_dir=str(tmp_path / "out"),
    )
    assert results and results[0].blocks


def test_parse_subprocess_failure_raises(tmp_path):
    class _Failed(_FakeCompleted):
        returncode = 1
        stderr = "boom: no module named six"

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "pdf2zh.magicpdf_adapter._run_mineru_process",
        lambda cmd, timeout, **kw: _Failed(),
    )
    try:
        with pytest.raises(MagicPdfParseError, match="six"):
            MagicPdfAdapter()._parse_mineru_subprocess(  # type: ignore[arg-type]
                __file__,
                ocr=False,
                python_exe="unused-python",
                out_dir=str(tmp_path / "out"),
            )
    finally:
        monkey.undo()


def test_override_points_to_missing_interpreter_raises(
    _fake_backend_mineru, monkeypatch
):
    missing = os.path.join(os.path.dirname(__file__), "_no_such_python_xyz.exe")
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", missing)
    with pytest.raises(MagicPdfParseError, match="missing interpreter"):
        _fake_backend_mineru.parse(__file__)


# ── worker 脚本契约 ─────────────────────────────────────────────────────────


def test_worker_script_compiles_and_has_main_guard():
    worker = os.path.join(REPO_ROOT, "pdf2zh", "kernel", "mineru_worker.py")
    py_compile.compile(worker, doraise=True)
    with open(worker, encoding="utf-8") as fh:
        src = fh.read()
    # Windows spawn 下 ProcessPoolExecutor 会重新导入本模块，必须有防护
    assert 'if __name__ == "__main__":' in src


# ── 设备传递（MinerU 3.x MINERU_DEVICE_MODE / 子进程 CUDA 预检） ─────────────


def test_mineru_device_mode_mapping():
    from pdf2zh.magicpdf_adapter import _mineru_device_mode

    assert _mineru_device_mode("cuda") == "cuda"
    assert _mineru_device_mode("gpu") == "cuda"
    assert _mineru_device_mode("cpu") == "cpu"
    assert _mineru_device_mode("mps") == "mps"
    assert _mineru_device_mode("dml") is None  # DirectML 对 MinerU 无效
    assert _mineru_device_mode("auto") is None
    assert _mineru_device_mode("") is None


def test_worker_apply_device_mode():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mineru_worker_mod", "pdf2zh/kernel/mineru_worker.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for device, expected in [
        ("cuda", "cuda"),
        ("cpu", "cpu"),
        ("mps", "mps"),
        ("dml", None),
        ("auto", None),
    ]:
        monkey = pytest.MonkeyPatch()
        monkey.delenv("MINERU_DEVICE_MODE", raising=False)
        try:
            mod._apply_device_mode(device)
            actual = os.environ.get("MINERU_DEVICE_MODE")
            assert actual == expected, (device, actual)
        finally:
            monkey.undo()


def test_parse_subprocess_passes_device_and_cuda_fallback(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    """cuda 请求但 venv torch 无 CUDA → 子进程 device 降级 cpu 并产出。"""
    seen: dict = {}

    def fake_run(cmd, timeout, **kw):
        seen["cmd"] = cmd
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    monkeypatch.setattr(
        MagicPdfAdapter, "_venv_torch_cuda", staticmethod(lambda py: False)
    )
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    MagicPdfAdapter(device="cuda")._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        python_exe=sys.executable,
        out_dir=str(tmp_path / "out2"),
    )
    assert seen["cmd"][-2] == "cpu"  # cuda 请求 → venv 无 CUDA → 降级 cpu


def test_parse_subprocess_cuda_when_venv_has_cuda(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    """隔离 venv torch 有 CUDA → 子进程 device 透传 cuda。"""
    seen: dict = {}

    def fake_run(cmd, timeout, **kw):
        seen["cmd"] = cmd
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    monkeypatch.setattr(
        MagicPdfAdapter, "_venv_torch_cuda", staticmethod(lambda py: True)
    )
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    MagicPdfAdapter(device="cuda")._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        python_exe=sys.executable,
        out_dir=str(tmp_path / "out3"),
    )
    assert seen["cmd"][-2] == "cuda"


# ── ensure_venv(cuda=True)：对已存在 CPU venv 原位升级 CUDA torch ────────────


def test_ensure_venv_cuda_upgrades_existing_cpu_venv(monkeypatch):
    """已有可用 venv 但 torch 为 CPU → cuda=True 时先升级 torch 再返回。"""
    calls = []

    def fake_default_venv_python():
        return r"C:\\venv\\python.exe"

    def fake_importable(py):
        return True

    def fake_venv_torch_cuda(py):
        # 第一次（升级前）False，升级后 True
        calls.append("probe")
        return len(calls) >= 2

    def fake_run(cmd, **kw):
        assert cmd[0] == r"C:\\venv\\python.exe"
        assert cmd[1] == "-m" and cmd[2] == "pip" and cmd[3] == "install"
        # 关键修复：必须带 --upgrade，否则已装的 CPU torch 会满足无版本要求，
        # pip 报 "Requirement already satisfied" 跳过重装，升级不生效。
        assert "--upgrade" in cmd
        assert "torch" in cmd and "torchvision" in cmd
        assert cmd[cmd.index("--index-url") + 1].startswith(
            "https://download.pytorch.org/whl/"
        )
        calls.append("upgrade")
        return None

    monkeypatch.setattr(mineru_env, "default_venv_python", fake_default_venv_python)
    monkeypatch.setattr(mineru_env, "_package_importable", fake_importable)
    monkeypatch.setattr(mineru_env, "_venv_torch_cuda", fake_venv_torch_cuda)
    # 升级后 wheel 已带 CUDA 构建标识（torch.version.cuda=12.6）
    monkeypatch.setattr(mineru_env, "_venv_torch_cuda_tag", lambda py: "12.6")
    monkeypatch.setattr(mineru_env, "subprocess", _FakeSubprocess(fake_run))
    interpreter = mineru_env.ensure_venv(cuda=True)
    assert interpreter == r"C:\\venv\\python.exe"
    assert "upgrade" in calls


def test_ensure_venv_cuda_skips_upgrade_when_already_cuda(monkeypatch):
    """venv 已是 CUDA torch → cuda=True 不重复安装。"""
    calls = []

    def fake_default_venv_python():
        return r"C:\\venv\\python.exe"

    def fake_importable(py):
        return True

    def fake_venv_torch_cuda(py):
        return True  # 已是 CUDA

    def fake_run(cmd, **kw):
        calls.append("run")
        return None

    monkeypatch.setattr(mineru_env, "default_venv_python", fake_default_venv_python)
    monkeypatch.setattr(mineru_env, "_package_importable", fake_importable)
    monkeypatch.setattr(mineru_env, "_venv_torch_cuda", fake_venv_torch_cuda)
    monkeypatch.setattr(mineru_env, "subprocess", _FakeSubprocess(fake_run))
    interpreter = mineru_env.ensure_venv(cuda=True)
    assert interpreter == r"C:\\venv\\python.exe"
    assert calls == []  # 不触发任何 pip


def test_venv_torch_cuda_probe(monkeypatch):
    """_venv_torch_cuda 按子进程输出解析。"""

    class _OK:
        returncode = 0
        stdout = "1\n"

    class _NG:
        returncode = 0
        stdout = "0\n"

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        if kw.get("capture_output") and cmd[-1].startswith("import torch"):
            # 固定模拟 torch.cuda.is_available()=True
            return _OK()
        return _NG()

    monkeypatch.setattr(mineru_env, "subprocess", _FakeSubprocess(fake_run, text=True))
    assert mineru_env._venv_torch_cuda("py") is True
    assert mineru_env._venv_torch_cuda("py") is True  # 第二次同样输出 1
    assert "import torch" in " ".join(seen["cmd"])


def test_venv_torch_cuda_tag_probe(monkeypatch):
    """_venv_torch_cuda_tag 返回 torch.version.cuda（None=CPU 构建）。"""

    class _Cu:
        returncode = 0
        stdout = "12.6\n"

    class _Cpu:
        returncode = 0
        stdout = "\n"

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        if "torch.version.cuda" in cmd[-1]:
            return _Cu()
        return _Cpu()

    monkeypatch.setattr(mineru_env, "subprocess", _FakeSubprocess(fake_run, text=True))
    assert mineru_env._venv_torch_cuda_tag("py") == "12.6"
    assert "torch.version.cuda" in " ".join(seen["cmd"])


class _FakeSubprocess:
    """minimal subprocess stand-in used by the tests above."""

    def __init__(self, run_fn=None, text=False):
        self._run_fn = run_fn
        self.text = text

    def run(self, cmd, **kwargs):
        if self._run_fn is not None:
            return self._run_fn(cmd, **kwargs)
        raise AssertionError("unexpected run: %r" % (cmd,))


def test_worker_conservative_vram_budget(monkeypatch):
    """8GB 卡注入 MINERU_VIRTUAL_VRAM_SIZE=6（batch_ratio=2）；16GB→16；
    用户显式配置优先；非 CUDA 不注入。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mineru_worker_vram", "pdf2zh/kernel/mineru_worker.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    class _FakeCuda:
        def __init__(self, avail, mem_gb=0):
            self._avail = avail
            self._mem_gb = mem_gb

        def is_available(self):
            return self._avail

        def get_device_properties(self, idx):
            class _P:
                total_memory = self._mem_gb * (1024**3)

            return _P()

    class _FakeTorch:
        pass

    fake_torch = _FakeTorch()

    # 8GB 卡 → 预算 6（batch_ratio=2）；worker 内 `import torch` 走 sys.modules
    monkeypatch.delenv("MINERU_VIRTUAL_VRAM_SIZE", raising=False)
    fake_torch.cuda = _FakeCuda(avail=True, mem_gb=8)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    mod._apply_conservative_vram_budget()
    assert os.environ["MINERU_VIRTUAL_VRAM_SIZE"] == "6"

    # 16GB 卡 → 预算 16（ratio=8）
    monkeypatch.delenv("MINERU_VIRTUAL_VRAM_SIZE", raising=False)
    fake_torch.cuda = _FakeCuda(avail=True, mem_gb=16)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    mod._apply_conservative_vram_budget()
    assert os.environ["MINERU_VIRTUAL_VRAM_SIZE"] == "16"

    # 用户显式配置优先
    monkeypatch.setenv("MINERU_VIRTUAL_VRAM_SIZE", "7")
    mod._apply_conservative_vram_budget()
    assert os.environ["MINERU_VIRTUAL_VRAM_SIZE"] == "7"

    # 非 CUDA（CPU）不注入新值
    monkeypatch.delenv("MINERU_VIRTUAL_VRAM_SIZE", raising=False)
    fake_torch.cuda = _FakeCuda(avail=False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    mod._apply_conservative_vram_budget()
    assert os.environ.get("MINERU_VIRTUAL_VRAM_SIZE") in (None, "7")


def test_parse_subprocess_forwards_mineru_config_env(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    """mineru_vram_size / window_size 经子进程 env 透传。"""
    seen = {}

    def fake_run(cmd, timeout, **kw):
        seen["env"] = kw.get("env")
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    adapter = MagicPdfAdapter(
        device="cuda",
        mineru_vram_size="4",
        mineru_window_size="8",
    )
    adapter._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        python_exe=sys.executable,
        out_dir=str(tmp_path / "out"),
    )
    env = seen["env"]
    assert env is not None
    assert env["MINERU_VIRTUAL_VRAM_SIZE"] == "4"
    assert env["MINERU_PROCESSING_WINDOW_SIZE"] == "8"


def test_parse_subprocess_no_env_when_config_empty(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    """配置为空（auto）时不透传 env（worker 自动保守估算）。"""
    seen = {}

    def fake_run(cmd, timeout, **kw):
        seen["env"] = kw.get("env")
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    MagicPdfAdapter(device="cuda")._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        python_exe=sys.executable,
        out_dir=str(tmp_path / "out"),
    )
    assert seen["env"] is None


def test_parse_subprocess_explicit_mode_and_backend(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    """mineru_parse_method=ocr + mineru_backend=hybrid 显式透传到 worker cmd。"""
    seen = {}

    def fake_run(cmd, timeout, **kw):
        seen["cmd"] = cmd
        out_dir = cmd[3]
        nested = os.path.join(out_dir, "paper", "auto")
        os.makedirs(nested, exist_ok=True)
        import json as _json

        with open(
            os.path.join(nested, "paper_middle.json"), "w", encoding="utf-8"
        ) as fh:
            _json.dump(_MIDDLE, fh)
        return _FakeCompleted()

    monkeypatch.setattr("pdf2zh.magicpdf_adapter._run_mineru_process", fake_run)
    monkeypatch.setenv("PDF2ZH_MINERU_PYTHON", sys.executable)
    MagicPdfAdapter(
        device="cuda",
        mineru_parse_method="ocr",
        mineru_backend="hybrid",
    )._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__,
        python_exe=sys.executable,
        out_dir=str(tmp_path / "out"),
    )
    # cmd: [py, worker, pdf, outdir, parse_method, lang, device, backend]
    assert seen["cmd"][4] == "ocr"
    assert seen["cmd"][-1] == "hybrid"
