"""Marker 隔离环境管理（doc/7o/adaptive_ingestion_v1_1_contract.md 场景 2–5）。

``vendor/marker`` 子模块 pin 在 datalab-to/marker v2（scenarios 2–5 的 fallback
后端），但其依赖树与 pdf2zh 主环境存在**硬性冲突**（见 pyproject.toml
[tool.uv] 注释）：gradio 锁 pydantic<2.12 而 marker 的 google-genai 要求
>=2.12.5，surya-ocr 精确锁 opencv-python-headless 与 pillow<11。这些冲突在
同一解释器内不可共存，因此 Marker 以**隔离 venv**运行：

- ``pdf2zh-setup-marker`` CLI 从 vendored 子模块源码（pin 版本、可复现）或
  PyPI（``marker-pdf``，无 submodule 的桌面/冻结环境回退）构建隔离 venv；
- live 摄入经 :mod:`pdf2zh.kernel.marker_worker` 子进程消费（同 MinerU 的
  ``PDF2ZH_MINERU_PYTHON`` 模式），产物 ``{stem}.json`` 直接喂给
  ``MarkerBackend.ingest_json``，主进程零 marker 依赖。

使用::

    git submodule update --init vendor/marker
    pdf2zh-setup-marker                      # 构建 <user-data>/pdf2zh/marker-venv
    pdf2zh --ingest-backend marker paper.pdf  # 自动探测，无需环境变量

升级 = ``git -C vendor/marker fetch --depth 1 origin tag <tag> &&
checkout <tag>`` 后重跑 setup + 回归。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: 仓库内源码锚点：<repo>/vendor/marker（.gitmodules pin 到 datalab-to/marker）
_SUBMODULE_DIR = Path(__file__).resolve().parents[2] / "vendor" / "marker"

#: 开发态仓库内 venv 位置（与 MinerU 的 submodule/.venv 锚点一致）
_VENV_DIR = _SUBMODULE_DIR / ".venv"

#: 隔离 venv 的 pip 安装超时（torch 系下载量大，与 mineru_env 同款）
_INSTALL_TIMEOUT = 3600

#: live 摄入解释器覆盖环境变量（对齐 PDF2ZH_MINERU_PYTHON 命名）
PYTHON_OVERRIDE_ENV = "PDF2ZH_MARKER_PYTHON"

#: 隔离 venv 构建位置覆盖环境变量
VENV_DIR_ENV = "PDF2ZH_MARKER_VENV_DIR"

#: 子模块源码不可用时的 PyPI 回退包名（marker-pdf 2.x）
PYPI_PACKAGE = "marker-pdf>=2,<3"


def _user_data_dir() -> Path:
    """跨平台用户数据目录（与 mineru_env 同款；桌面/冻结环境可写）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "pdf2zh"


def default_venv_dir() -> Path:
    """隔离 venv 的优先构建位置（可写）。

    - ``PDF2ZH_MARKER_VENV_DIR`` 显式指定时优先；
    - 否则落在用户数据目录 ``<appdata>/pdf2zh/marker-venv``，规避只读安装目录。
    """
    env_dir = os.environ.get(VENV_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir)
    return _user_data_dir() / "marker-venv"


def submodule_dir() -> Path:
    """子模块目录（可被测试替换）。"""
    return _SUBMODULE_DIR


def venv_python(venv_dir: Path | None = None) -> str:
    """隔离 venv 解释器路径。"""
    base = venv_dir if venv_dir is not None else _VENV_DIR
    if sys.platform == "win32":
        return str(base / "Scripts" / "python.exe")
    return str(base / "bin" / "python")


def default_venv_python() -> str | None:
    """自动探测已构建的隔离 venv 解释器（仅查文件存在）。

    搜索顺序（首个命中即返回）：

    1. ``PDF2ZH_MARKER_PYTHON`` 显式指定的解释器；
    2. ``PDF2ZH_MARKER_VENV_DIR`` 指定的目录；
    3. 用户数据目录 ``<appdata>/pdf2zh/marker-venv``；
    4. 仓库内 submodule 锚点 ``vendor/marker/.venv``（开发态）。

    （MinerU 版本还有第 3 项「当前解释器所在目录」——marker 从不进主依赖树，
    该位置不会有 marker，无探测意义。）
    """
    candidates: list[Path] = []
    override = os.environ.get(PYTHON_OVERRIDE_ENV, "").strip()
    if override:
        candidates.append(Path(override))
    env_dir = os.environ.get(VENV_DIR_ENV, "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(_user_data_dir() / "marker-venv")
    candidates.append(_VENV_DIR)
    seen: set[Path] = set()
    for c in candidates:
        try:
            c = c.resolve()
        except OSError:
            continue
        if c in seen:
            continue
        seen.add(c)
        if c.is_file():
            # PDF2ZH_MARKER_PYTHON 直接指向解释器文件
            return str(c)
        p = venv_python(c)
        if os.path.exists(p):
            return p
    return None


def submodule_available() -> bool:
    """源码锚点是否就位（已 clone 且包含包本体）。

    vendor/marker 是 PEP 420 隐式命名空间包（无 ``__init__.py``），以
    ``marker/converters`` 目录存在性为锚点判据。
    """
    d = submodule_dir()
    return (d / "pyproject.toml").exists() and (d / "marker" / "converters").is_dir()


def _find_system_python() -> str:
    """寻找可用的宿主 Python（PyInstaller 冻结态回退 PATH 搜索）。

    与 :mod:`pdf2zh.kernel.mineru_env` 同款策略；marker 2.x 的 requires-python
    为 ``>=3.10,<4``，因此 3.10+ 均可（上不封顶，与 MinerU 的 <3.14 不同）。
    """
    candidates: list[str] = []
    if not getattr(sys, "frozen", False) and sys.executable:
        candidates.append(sys.executable)
    candidates += ["python3", "python"]

    seen: set[str] = set()
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if probe.returncode != 0:
            continue
        resolved = probe.args[0]
        if resolved in seen:
            continue
        seen.add(resolved)
        version = tuple(int(x) for x in probe.stdout.strip().split(".")[:2]) or (0, 0)
        if version < (3, 10):
            logger.info(
                "Skip interpreter %s (version %s below marker's required 3.10)",
                resolved,
                probe.stdout.strip(),
            )
            continue
        try:
            venv_ok = subprocess.run(
                [resolved, "-m", "venv", "--help"],
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if venv_ok.returncode == 0:
            logger.info("Discovered host Python %s: %s", probe.stdout.strip(), resolved)
            return resolved

    raise RuntimeError(
        "No usable host Python (>=3.10 with venv support) found to build "
        "the isolated Marker environment. Install Python from python.org and "
        "ensure it is on PATH."
    )


def ensure_venv(force_recreate: bool = False) -> str:
    """确保隔离 venv 存在且装有 marker；返回解释器路径。

    优先复用已构建且可用的 venv（任意候选位置，见
    :func:`default_venv_python`）；否则新建。源码优先使用仓库内
    ``vendor/marker`` submodule（pin 版本、可复现），但桌面/冻结分发未携带
    submodule 时自动回退 PyPI 安装 ``marker-pdf>=2,<3``。

    构建位置：``PDF2ZH_MARKER_VENV_DIR`` > 仓库 submodule（可写时）>
    用户数据目录（见 :func:`default_venv_dir`）。

    Args:
        force_recreate: True 时删除既有 venv 重建（残缺安装自愈的第二手段；
            常规残缺由 :func:`_package_importable` 探测 + 裸 pip 补装修复）。
    """
    existing = default_venv_python()
    if existing and not force_recreate and _package_importable(existing):
        return existing

    if force_recreate:
        import shutil

        for candidate in (_VENV_DIR, default_venv_dir()):
            try:
                shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                pass
        existing = None

    # submodule 源码可用且所在目录可写时，使用 pin 版本；否则 PyPI 回退。
    use_submodule = submodule_available() and os.access(
        str(_SUBMODULE_DIR.parent), os.W_OK
    )
    if use_submodule:
        target_dir = _VENV_DIR
        source_spec = str(_SUBMODULE_DIR)
        install_cwd = str(_SUBMODULE_DIR)
    else:
        target_dir = default_venv_dir()
        source_spec = PYPI_PACKAGE
        install_cwd = None

    target = venv_python(target_dir)
    if existing and os.path.exists(target) and _package_importable(target):
        return target

    if not os.path.exists(target):
        logger.info("Creating isolated Marker venv at %s ...", target_dir)
        subprocess.run(
            [_find_system_python(), "-m", "venv", str(target_dir)],
            check=True,
            timeout=120,
        )
        subprocess.run(
            [target, "-m", "pip", "install", "-U", "pip"],
            check=True,
            timeout=300,
        )

    logger.info(
        "Installing %s into %s (large download: torch etc.) ...",
        source_spec,
        target_dir,
    )
    subprocess.run(
        [target, "-m", "pip", "install", source_spec],
        check=True,
        timeout=_INSTALL_TIMEOUT,
        cwd=install_cwd,
    )
    logger.info("Isolated Marker environment ready: %s", target)
    return target


def _package_importable(interpreter: str) -> bool:
    """隔离 venv 里 marker 转换入口是否可导入（残缺安装自愈）。

    深度校验到 ``marker.config.parser.ConfigParser`` 与
    ``marker.models.create_model_dict``（live 摄入 worker 的完整依赖面），
    仅查顶层 ``marker`` 会漏报残缺安装。轻量子进程，不加载 torch 模型。
    """
    cwd = str(_SUBMODULE_DIR) if _SUBMODULE_DIR.is_dir() else None
    try:
        result = subprocess.run(
            [
                interpreter,
                "-c",
                "from marker.config.parser import ConfigParser; "
                "from marker.models import create_model_dict",
            ],
            capture_output=True,
            timeout=120,
            cwd=cwd,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 -- 探测失败按未安装处理
        return False


def marker_python_override() -> str | None:
    """读取 live 摄入解释器：优先 ``PDF2ZH_MARKER_PYTHON``，否则自动探测。

    与 :func:`pdf2zh.engine_env.mineru_python_override` 同款语义；供
    :class:`pdf2zh.v3.ingestion.marker_backend.MarkerBackend.ingest` 与
    ``_marker_live_available`` 消费。
    """
    value = os.environ.get(PYTHON_OVERRIDE_ENV, "").strip()
    if value:
        return value
    try:
        return default_venv_python()
    except Exception:  # noqa: BLE001 -- 探测失败视为未配置
        return None


def setup_marker_cli() -> None:
    """CLI 入口：构建/修复隔离 venv 并打印用法提示。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    interpreter = ensure_venv()
    print("\nMarker isolated environment ready.")
    print(
        "pdf2zh auto-detects this venv on every run (no environment variable "
        "required). To force a specific interpreter, set:\n"
    )
    if sys.platform == "win32":
        print(f"  set {PYTHON_OVERRIDE_ENV}={interpreter}")
    else:
        print(f"  export {PYTHON_OVERRIDE_ENV}={interpreter}")
    print("  pdf2zh --ingest-backend marker your.pdf\n")
    print("In the desktop app, Marker is selectable as the ingestion backend.")


if __name__ == "__main__":
    setup_marker_cli()
