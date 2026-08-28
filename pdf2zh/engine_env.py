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
import subprocess
import sys

#: ``probe_mineru_override`` 的探测结果缓存（按解释器路径），避免每次探测
#: 都启动子进程（mineru 在 venv 内的可导入性只需校验一次）。
_OVERRIDE_PROBE_CACHE: dict[str, bool] = {}

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
    优先 mineru（受支持且已安装，或隔离 venv / ``PDF2ZH_MINERU_PYTHON``
    已就绪），其次 magic-pdf；均未安装时返回 ``(backend_hint(), False)``。
    """
    if probe_mineru_override() is not None:
        return "mineru", True
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
    """读取隔离 venv 解释器：优先 ``PDF2ZH_MINERU_PYTHON``，否则自动探测。

    - ``PDF2ZH_MINERU_PYTHON`` 指向任意装有 ``mineru[pipeline]`` 的解释器
      （如用户用 ``uv`` 自建的 venv）时，解析经子进程走该解释器；
    - 未显式设置时，自动探测 ``pdf2zh-setup-mineru`` 构建的
      ``vendor/MinerU/.venv``（torch 等重依赖与主进程完全隔离，DLL 加载
      顺序/依赖冲突面归零），免去每次手动 ``set`` 环境变量。

    与 :mod:`pdf2zh.kernel.mineru_env` 配套。
    """
    value = os.environ.get("PDF2ZH_MINERU_PYTHON", "").strip()
    if value:
        return value
    try:
        from pdf2zh.kernel.mineru_env import default_venv_python

        return default_venv_python()
    except Exception:  # noqa: BLE001 -- 兜底：探测失败视为未配置
        return None


def probe_mineru_override() -> str | None:
    """探测隔离 venv / ``PDF2ZH_MINERU_PYTHON`` 指定的 MinerU 解释器是否可用。

    该路径下 MinerU 装在隔离解释器里，主进程并不 ``import mineru``，因此
    常规 ``probe_mineru`` 会漏报。这里启动该解释器用 ``find_spec`` 轻量校验
    （不触发 torch 等重导入），可用则返回解释器路径，否则返回 ``None``。

    结果按解释器路径缓存，避免 ``available_backend`` / ``backend`` 频繁调用时
    反复启动子进程。供 :func:`available_backend` 与
    :meth:`pdf2zh.magicpdf_adapter.MagicPdfAdapter.backend` 用于「主进程未装
    mineru 但隔离环境已就绪」时仍判定 MinerU 可用。
    """
    python = mineru_python_override()
    if not python or not os.path.exists(python):
        return None
    if python in _OVERRIDE_PROBE_CACHE:
        return python if _OVERRIDE_PROBE_CACHE[python] else None
    ok = False
    try:
        result = subprocess.run(
            [
                python,
                "-c",
                "import importlib.util, sys; "
                # 深度校验：与 probe_mineru 保持一致，不仅查顶层 mineru 包，
                # 还需官方编程入口 mineru.cli.common（do_parse）存在——残缺安装
                # （缺 cli 子模块）会在此漏报，避免运行时才炸。
                "sys.exit(0 if (importlib.util.find_spec('mineru') is not None "
                "and importlib.util.find_spec('mineru.cli.common') is not None) "
                "else 1)",
            ],
            capture_output=True,
            timeout=60,
        )
        ok = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        ok = False
    _OVERRIDE_PROBE_CACHE[python] = ok
    return python if ok else None


def mineru_install_hint() -> str:
    """按当前 Python 版本/运行形态给出可执行的安装建议（含依赖冲突提示）。"""
    if getattr(sys, "frozen", False):
        # 桌面/冻结分发：venv 隔离、torch 不进安装包，提供一键构建入口。
        return (
            "桌面版不内置 MinerU/torch（体积上限）。在「设置 → MinerU」中点击"
            "「一键安装 MinerU」即可在用户数据目录构建隔离环境（需本机存在"
            " Python 3.10–3.13，且具备 venv 模块）；模型在首次解析时下载到"
            "用户缓存，与应用目录分离。"
        )
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
