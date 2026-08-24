"""Step 1.1 — 解析引擎环境探测与选择。

按 Python 版本 / 操作系统动态选择 magic-pdf / MinerU 后端，供
:mod:`pdf2zh.magicpdf_adapter` 使用：

- Py3.10~3.13（任一 OS）→ 优先 ``mineru`` 3.x（pipeline 本地后端；
  Windows 上 vlm-engine/hybrid 因 ``ray`` 仍限 3.10~3.12，不影响本用途）；
- ``magic-pdf`` 1.x（已停更，最后 1.3.12 @2025-05）降级为手动兜底：
  仅当已安装且 mineru 缺失时由 :class:`MagicPdfAdapter` 回退使用；
- 缺依赖环境保持可调用：探测函数返回不可用原因，配合熔断降级。

环境变量：
- ``PDF2ZH_MINERU_PREFER=0``：关闭 mineru 优先，强制偏好 magic-pdf；
- ``PDF2ZH_MAGICPDF_DEVICE=cpu|cuda|auto``：覆盖引擎运行设备。
"""

from __future__ import annotations

import importlib.util
import os
import sys

MINERU_MIN_PY = (3, 10)
MINERU_MAX_PY = (3, 13)  # MinerU >=3.1 官方支持范围（requires-python <3.14）
MAGICPDF_MIN_PY = (3, 8)  # magic-pdf 1.x 支持范围（下限）


def python_version() -> tuple[int, int]:
    """当前 (major, minor)。"""
    return (sys.version_info.major, sys.version_info.minor)


def prefer_mineru() -> bool:
    """是否优先 mineru 3.x（Py3.10-3.13；``PDF2ZH_MINERU_PREFER=0`` 关闭）。"""
    pref = os.environ.get("PDF2ZH_MINERU_PREFER", "1").strip().lower()
    if pref in ("0", "false", "no", "off"):
        return False
    return MINERU_MAX_PY >= python_version() >= MINERU_MIN_PY


def backend_hint() -> str:
    """推荐的引擎名：``mineru`` / ``magicpdf``（不探测是否已安装）。"""
    return "mineru" if prefer_mineru() else "magicpdf"


def _find_spec(name: str) -> object | None:
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def probe_magicpdf() -> object | None:
    """探测 ``magic_pdf`` 模块；可导入则返回模块对象，否则 None。"""
    spec = _find_spec("magic_pdf")
    if spec is None:
        return None
    try:
        return importlib.import_module("magic_pdf")
    except Exception:  # noqa: BLE001 -- 探测失败视为不可用
        return None


def probe_mineru() -> object | None:
    """探测 ``mineru`` 及其编程入口 ``mineru.cli.common``；可用则返回模块。

    只查 ``mineru`` 顶层不够——残缺安装（缺 cli 子模块）会让适配器在
    解析时才炸，这里一并验证官方编程入口存在。
    """
    spec = _find_spec("mineru")
    if spec is None:
        return None
    try:
        mod = importlib.import_module("mineru")
        importlib.import_module("mineru.cli.common")
        return mod
    except Exception:  # noqa: BLE001 -- 探测失败视为不可用
        return None


def mineru_supported() -> bool:
    """当前 Python 版本是否支持 MinerU 3.x（Py3.10~3.13）。"""
    return MINERU_MAX_PY >= python_version() >= MINERU_MIN_PY


def available_backend() -> tuple[str, bool]:
    """探测已安装引擎，返回 ``(backend, is_available)``。

    与 :class:`MagicPdfAdapter.backend` 的实际解析选择保持一致：
    优先 mineru（受支持且已安装），其次 magic-pdf；
    均未安装时返回 ``(backend_hint(), False)``。
    """
    if probe_mineru() is not None and mineru_supported():
        return "mineru", True
    if probe_magicpdf() is not None:
        return "magicpdf", True
    return backend_hint(), False


def resolve_device(requested: str = "auto") -> str:
    """解析运行设备：``PDF2ZH_MAGICPDF_DEVICE`` 优先，其次参数。"""
    env = os.environ.get("PDF2ZH_MAGICPDF_DEVICE", "").strip().lower()
    return env or (requested or "auto")


def mineru_python_override() -> str | None:
    """读取 ``PDF2ZH_MINERU_PYTHON``：指定隔离 venv 解释器时走子进程解析。

    与 ``pdf2zh-setup-mineru``（:mod:`pdf2zh.kernel.mineru_env`）配套：
    torch 等重依赖与主进程完全隔离，DLL 加载顺序/依赖冲突面归零。
    """
    value = os.environ.get("PDF2ZH_MINERU_PYTHON", "").strip()
    return value or None


def mineru_install_hint() -> str:
    """按当前 Python 版本给出可执行的安装建议（含依赖冲突提示）。"""
    if not prefer_mineru():
        return (
            'uv pip install -U "magic-pdf[full]<2"  # 手动兜底：magic-pdf 1.x'
            "（已停更；pip 遇 pymupdf/pdfminer 冲突见 docs/ADVANCED.md）"
        )
    return (
        'uv pip install -U "mineru[pipeline]>=3.1"  '
        "# MinerU 3.x（pipeline 本地后端，Py3.10-3.13；模型权重经"
        " MINERU_MODEL_SOURCE=modelscope|huggingface 下载；"
        "pip 遇冲突见 docs/ADVANCED.md）"
    )
