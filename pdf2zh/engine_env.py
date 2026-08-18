"""Step 1.1 — 解析引擎环境探测与选择。

按 Python 版本 / 操作系统动态选择 magic-pdf / MinerU 后端，供
:mod:`pdf2zh.magicpdf_adapter` 使用：

- Py3.10~3.12（任一 OS）→ 优先 ``mineru`` 2.x；
- Py3.13（含 Windows）→ 兜底 ``magic-pdf`` 1.x（ONNX 轻量后端）；
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
MINERU_MAX_PY = (3, 12)  # MinerU 2.x 官方支持范围
MAGICPDF_MIN_PY = (3, 8)  # magic-pdf 1.x 支持范围（下限）


def python_version() -> tuple[int, int]:
    """当前 (major, minor)。"""
    return (sys.version_info.major, sys.version_info.minor)


def prefer_mineru() -> bool:
    """是否优先 mineru 2.x（Py3.10-3.12；``PDF2ZH_MINERU_PREFER=0`` 关闭）。"""
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
    """探测 ``mineru`` 模块；可导入则返回模块对象，否则 None。"""
    spec = _find_spec("mineru")
    if spec is None:
        return None
    try:
        return importlib.import_module("mineru")
    except Exception:  # noqa: BLE001 -- 探测失败视为不可用
        return None


def mineru_supported() -> bool:
    """当前 Python 版本是否支持 MinerU 2.x（Py3.10~3.12）。"""
    return MINERU_MAX_PY >= python_version() >= MINERU_MIN_PY


def available_backend() -> tuple[str, bool]:
    """探测已安装引擎，返回 ``(backend, is_available)``。

    优先 magic-pdf（``magic_pdf`` 模块），其次 mineru（``mineru`` 模块）；
    均未安装时返回 ``(backend_hint(), False)``。
    """
    if probe_magicpdf() is not None:
        return "magicpdf", True
    if probe_mineru() is not None and mineru_supported():
        return "mineru", True
    return backend_hint(), False


def resolve_device(requested: str = "auto") -> str:
    """解析运行设备：``PDF2ZH_MAGICPDF_DEVICE`` 优先，其次参数。"""
    env = os.environ.get("PDF2ZH_MAGICPDF_DEVICE", "").strip().lower()
    return env or (requested or "auto")


def mineru_install_hint() -> str:
    """按当前 Python 版本给出可执行的安装建议。"""
    if not prefer_mineru():
        return (
            "pip install -U \"magic-pdf[full]<2\"  # Py3.13 兜底：magic-pdf 1.x"
        )
    return "pip install -U \"mineru[full]>=2\"  # Py3.10-3.12：MinerU 2.x"
