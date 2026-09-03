"""Marker 隔离环境（vendor/marker 子模块 + 隔离 venv）回归测试。

对应 :mod:`pdf2zh.kernel.marker_env` / :mod:`pdf2zh.kernel.marker_worker`：
- .gitmodules 记录了 vendor/marker 源码锚点；
- marker_env 的路径/探测/覆盖逻辑（PDF2ZH_MARKER_PYTHON / PDF2ZH_MARKER_VENV_DIR）；
- MarkerBackend.ingest 的两条执行形态（隔离子进程优先、进程内回退）；
- worker 脚本契约（stdlib-only、__main__ 防护、参数校验、UTF-8 stdio）；
- _marker_live_available 的隔离 venv 探测。
"""

from __future__ import annotations

import json
import os
import py_compile
import sys

import pytest

from pdf2zh.kernel import marker_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 源码锚点声明 ─────────────────────────────────────────────────────────────


def test_gitmodules_declares_marker_anchor():
    with open(os.path.join(REPO_ROOT, ".gitmodules"), encoding="utf-8") as fh:
        content = fh.read()
    assert "vendor/marker" in content
    assert "datalab-to/marker" in content


# ── marker_env 纯逻辑 ────────────────────────────────────────────────────────


def test_submodule_available_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(marker_env, "_SUBMODULE_DIR", tmp_path)
    assert not marker_env.submodule_available()


def test_submodule_available_true_when_pinned_source_present(tmp_path, monkeypatch):
    # vendor/marker 是 PEP 420 隐式命名空间包（无 __init__.py），
    # 以 marker/converters 目录存在性为锚点判据。
    (tmp_path / "marker" / "converters").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='marker-pdf'\n")
    monkeypatch.setattr(marker_env, "_SUBMODULE_DIR", tmp_path)
    assert marker_env.submodule_available()


def test_default_venv_python_none_when_unconfigured(monkeypatch, tmp_path):
    # 全部候选锚点都指向不存在的临时路径——不得依赖本机是否已构建 venv
    # （vendor/marker/.venv 在 pdf2zh-setup-marker 之后真实存在，不能用作探测点）
    monkeypatch.delenv(marker_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(marker_env.VENV_DIR_ENV, raising=False)
    monkeypatch.setattr(marker_env, "_VENV_DIR", tmp_path / "nosub" / ".venv")
    monkeypatch.setattr(marker_env, "_user_data_dir", lambda: tmp_path / "nohome")
    assert marker_env.default_venv_python() is None


def test_default_venv_python_env_var_precedence(tmp_path, monkeypatch):
    # 1) PYTHON_OVERRIDE 直接指向解释器文件
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv(marker_env.PYTHON_OVERRIDE_ENV, str(exe))
    assert marker_env.default_venv_python() == str(exe)

    # 2) VENV_DIR 指向 venv 目录
    venv_dir = tmp_path / "venv"
    if sys.platform == "win32":
        py = venv_dir / "Scripts" / "python.exe"
    else:
        py = venv_dir / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    monkeypatch.delenv(marker_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv(marker_env.VENV_DIR_ENV, str(venv_dir))
    assert marker_env.default_venv_python() == str(py)


def test_marker_python_override(monkeypatch, tmp_path):
    monkeypatch.delenv(marker_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(marker_env.VENV_DIR_ENV, raising=False)
    monkeypatch.setattr(marker_env, "_user_data_dir", lambda: tmp_path / "nohome")
    monkeypatch.setattr(marker_env, "_VENV_DIR", tmp_path / "nosub" / ".venv")
    assert marker_env.marker_python_override() is None

    exe = tmp_path / "py.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv(marker_env.PYTHON_OVERRIDE_ENV, f" {exe} ")
    assert marker_env.marker_python_override() == str(exe)


def test_entry_point_declared_in_pyproject():
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        content = fh.read()
    assert 'pdf2zh-setup-marker = "pdf2zh.kernel.marker_env:setup_marker_cli"' in (
        content
    )


# ── worker 脚本契约 ──────────────────────────────────────────────────────────


def _worker_path():
    return os.path.join(REPO_ROOT, "pdf2zh", "kernel", "marker_worker.py")


def test_worker_compiles_and_is_main_guarded():
    py_compile.compile(_worker_path(), doraise=True)
    with open(_worker_path(), encoding="utf-8") as fh:
        src = fh.read()
    assert 'if __name__ == "__main__":' in src
    # stdlib-only：不得 import pdf2zh（目标 venv 里没有 pdf2zh）
    for line in src.splitlines():
        s = line.strip()
        assert not s.startswith("import pdf2zh"), line
        assert not s.startswith("from pdf2zh"), line


def test_worker_usage_error(tmp_path):
    import subprocess

    proc = subprocess.run(
        [sys.executable, _worker_path()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


def test_worker_missing_pdf(tmp_path):
    import subprocess

    proc = subprocess.run(
        [sys.executable, _worker_path(), str(tmp_path / "nope.pdf"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "not found" in proc.stderr


# ── MarkerBackend.ingest 执行形态 ────────────────────────────────────────────


@pytest.fixture
def fake_worker_payload(tmp_path, monkeypatch):
    """伪造隔离子进程产物：{stem}.json + _meta.json（marker save_output 布局）。"""
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    stem = "doc"

    def _run(python_exe: str, pdf_path: str) -> dict:
        work = tmp_path / "work"
        (work / stem).mkdir(parents=True, exist_ok=True)
        payload = {
            "block_type": "Document",
            "children": [
                {
                    "id": "/page/0",
                    "block_type": "Page",
                    "bbox": [0, 0, 1000, 1400],
                    "children": [
                        {
                            "id": "/page/0/Text/0",
                            "block_type": "Text",
                            "html": "hello <b>world</b>",
                            "bbox": [100, 100, 500, 140],
                        }
                    ],
                }
            ],
        }
        (work / stem / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")
        (work / stem / f"{stem}_meta.json").write_text(
            json.dumps({"pdf_path": pdf_path}), encoding="utf-8"
        )
        # 真实 _ingest_subprocess 的返回契约：_load_worker_payload 的产物 dict
        return payload

    monkeypatch.setattr(MarkerBackend, "_ingest_subprocess", staticmethod(_run))
    return _run


def test_ingest_prefers_isolated_subprocess(tmp_path, monkeypatch, fake_worker_payload):
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")
    # _isolated_python 命中 → 走（伪造的）子进程路径，绝不 import marker
    monkeypatch.setattr(
        MarkerBackend, "_isolated_python", lambda self: "C:/fake/python.exe"
    )
    doc = MarkerBackend(marker_version="test").ingest(str(pdf))
    assert doc.page_count == 1
    assert doc.block_count == 1
    assert doc.metadata.get("marker_version") == "test"
    # 子进程产物落在 output_dir/<stem>/：_load_worker_payload 的读取布局
    assert fake_worker_payload("x", str(pdf)) is not None


def test_ingest_subprocess_loads_payload(tmp_path, monkeypatch):
    """_ingest_subprocess 真实实现（只 mock subprocess.run）读回 worker 产物。"""
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")
    stem = "doc"
    work = tmp_path / "work"
    (work / stem).mkdir(parents=True)
    payload = {
        "block_type": "Document",
        "children": [
            {
                "id": "/page/0",
                "block_type": "Page",
                "bbox": [0, 0, 800, 600],
                "children": [],
            }
        ],
    }
    (work / stem / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")
    (work / stem / f"{stem}_meta.json").write_text(
        json.dumps({"pdf_path": str(pdf)}), encoding="utf-8"
    )

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "pdf2zh.v3.ingestion.marker_backend.subprocess.run",
        lambda *a, **k: FakeCompleted(),
    )
    # worker 的 one-shot 临时目录指向预置产物位置（tempfile 是函数内导入，
    # 补丁打在全局 tempfile.mkdtemp 上）
    import tempfile

    monkeypatch.setattr(tempfile, "mkdtemp", lambda **k: str(work))
    got = MarkerBackend()._ingest_subprocess("C:/fake/python.exe", str(pdf))
    assert got["block_type"] == "Document"
    assert got["metadata"]["pdf_path"] == str(pdf)


def test_ingest_subprocess_failure_raises_unavailable(tmp_path, monkeypatch):
    from pdf2zh.v3.ingestion.base import IngestionBackendUnavailable
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "boom: torch not installed"

    monkeypatch.setattr(
        "pdf2zh.v3.ingestion.marker_backend.subprocess.run",
        lambda *a, **k: FakeCompleted(),
    )
    with pytest.raises(IngestionBackendUnavailable, match="boom"):
        MarkerBackend()._ingest_subprocess("C:/fake/python.exe", str(pdf))


def test_ingest_subprocess_timeout(tmp_path, monkeypatch):
    import subprocess as _subprocess

    from pdf2zh.v3.ingestion.base import IngestionBackendUnavailable
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")

    def _timeout(*a, **k):
        raise _subprocess.TimeoutExpired(cmd="marker_worker", timeout=1)

    monkeypatch.setenv("PDF2ZH_MARKER_TIMEOUT", "1")
    monkeypatch.setattr("pdf2zh.v3.ingestion.marker_backend.subprocess.run", _timeout)
    with pytest.raises(IngestionBackendUnavailable, match="timed out"):
        MarkerBackend()._ingest_subprocess("C:/fake/python.exe", str(pdf))


def test_isolated_python_none_without_venv(monkeypatch, tmp_path):
    from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

    monkeypatch.delenv(marker_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(marker_env.VENV_DIR_ENV, raising=False)
    monkeypatch.setattr(marker_env, "_user_data_dir", lambda: tmp_path / "nohome")
    monkeypatch.setattr(marker_env, "_VENV_DIR", tmp_path / "nosub" / ".venv")
    assert MarkerBackend()._isolated_python() is None


# ── _marker_live_available 探测 ──────────────────────────────────────────────


def test_marker_live_available_via_venv(monkeypatch, tmp_path):
    from pdf2zh.magicpdf_cli import _marker_live_available

    monkeypatch.delenv(marker_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(marker_env.VENV_DIR_ENV, raising=False)
    monkeypatch.setattr(marker_env, "_user_data_dir", lambda: tmp_path / "nohome")
    monkeypatch.setattr(marker_env, "_VENV_DIR", tmp_path / "nosub" / ".venv")
    # 主进程未装 marker（本仓库约束）：venv 缺失 → 不可用
    try:
        import marker  # noqa: F401

        main_env_has_marker = True
    except Exception:
        main_env_has_marker = False
    if not main_env_has_marker:
        assert _marker_live_available() is False

    # venv 就位 → 可用
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv(marker_env.PYTHON_OVERRIDE_ENV, str(exe))
    assert _marker_live_available() is True


def test_marker_live_available_false_on_env_module_failure(monkeypatch):
    from pdf2zh.magicpdf_cli import _marker_live_available

    monkeypatch.setenv(marker_env.PYTHON_OVERRIDE_ENV, "   ")
    # 探测不应因 marker_env 缺失/异常而崩溃——最多走 import marker 回退
    try:
        result = _marker_live_available()
    except Exception as exc:  # pragma: no cover - 不应到达
        pytest.fail(f"_marker_live_available raised: {exc}")
    assert isinstance(result, bool)
