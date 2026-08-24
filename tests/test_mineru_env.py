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

    def fake_run(cmd, timeout):
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
    monkeypatch.setattr(
        "pdf2zh.magicpdf_adapter._run_mineru_process", fake_run
    )
    results = _fake_backend_mineru.parse(
        __file__, progress_cb=lambda d: None,
    )

    assert seen["cmd"][1].endswith("mineru_worker.py")
    assert seen["cmd"][4] == "auto"          # parse_method 非 OCR 默认
    assert seen["timeout"] >= 3600           # torch 系安装/解析的宽松超时
    assert len(results) == 1
    assert results[0].text() == "subprocess ok"
    assert results[0].backend == "mineru"


def test_parse_subprocess_uses_ocr_flag(
    _fake_backend_mineru, monkeypatch, tmp_path
):
    def fake_run(cmd, timeout):
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

    monkeypatch.setattr(
        "pdf2zh.magicpdf_adapter._run_mineru_process", fake_run
    )
    results = MagicPdfAdapter()._parse_mineru_subprocess(  # type: ignore[arg-type]
        __file__, ocr=True, python_exe="unused-python",
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
        lambda cmd, timeout: _Failed(),
    )
    try:
        with pytest.raises(MagicPdfParseError, match="six"):
            MagicPdfAdapter()._parse_mineru_subprocess(  # type: ignore[arg-type]
                __file__, ocr=False, python_exe="unused-python",
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
