"""MinerU 源码锚点环境管理（P1，doc/mineru_submodule_feasibility_report.md）。

``vendor/MinerU`` 子模块 pin 在经过真机验证的上游 release 上；本模块提供
``pdf2zh-setup-mineru`` CLI：从该 pin 源码构建**隔离 venv**（torch 等重依赖
不进主环境），并经 :mod:`pdf2zh.kernel.mineru_worker` 子进程消费 —— 规避
本项目已知的 torch×onnxruntime CUDA DLL 加载顺序冲突与 pymupdf 版本冲突。

使用::

    git submodule update --init vendor/MinerU
    pdf2zh-setup-mineru                      # 构建 vendor/MinerU/.venv
    set PDF2ZH_MINERU_PYTHON=<repo>/vendor/MinerU/.venv/Scripts/python.exe
    pdf2zh --parse-engine magicpdf paper.pdf # 自动改走子进程解析路径

升级 = ``git -C vendor/MinerU fetch --depth 1 origin tag <tag> &&
checkout <tag>`` 后重跑 setup + 回归。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: 仓库内源码锚点：<repo>/vendor/MinerU（.gitmodules pin 到上游 release tag）
_SUBMODULE_DIR = Path(__file__).resolve().parents[2] / "vendor" / "MinerU"
_VENV_DIR = _SUBMODULE_DIR / ".venv"

#: 隔离 venv 的 pip 安装超时（torch 系下载量大，远大于 precise 内核的 300s）
_INSTALL_TIMEOUT = 3600


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
    """自动探测 ``pdf2zh-setup-mineru`` 构建的隔离 venv 解释器（仅查文件存在）。

    仅在 ``PDF2ZH_MINERU_PYTHON`` 未设置时作为兜底；真实可用性（mineru 是否
    可导入）由 :func:`pdf2zh.engine_env.probe_mineru_override` 校验。返回
    ``None`` 表示 ``vendor/MinerU/.venv`` 尚未构建（如未执行
    ``git submodule update`` / ``pdf2zh-setup-mineru``）。
    """
    target = venv_python()
    return target if os.path.exists(target) else None


def submodule_available() -> bool:
    """源码锚点是否就位（已 clone 且包含包本体）。"""
    d = submodule_dir()
    return (d / "pyproject.toml").exists() and (d / "mineru").is_dir()


def _find_system_python() -> str:
    """寻找可用的宿主 Python（PyInstaller 冻结态回退 PATH 搜索）。

    与 :mod:`pdf2zh.kernel.precise` 同款策略；另要求版本 ≤3.13，
    （MinerU requires-python <3.14），否则给出明确指引。
    """
    candidates: list[str] = []
    if not getattr(sys, "frozen", False) and sys.executable:
        candidates.append(sys.executable)
    candidates += ["python3", "python"]

    seen: set[str] = set()
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c",
                 "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if probe.returncode != 0:
            continue
        resolved = probe.args[0]
        if resolved in seen:
            continue
        seen.add(resolved)
        version = tuple(
            int(x) for x in probe.stdout.strip().split(".")[:2]
        ) or (0, 0)
        if not ((3, 10) <= version <= (3, 13)):
            logger.info(
                "Skip interpreter %s (version %s outside MinerU's "
                "supported 3.10-3.13 range)",
                resolved, probe.stdout.strip(),
            )
            continue
        try:
            venv_ok = subprocess.run(
                [resolved, "-m", "venv", "--help"],
                capture_output=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if venv_ok.returncode == 0:
            logger.info("Discovered host Python %s: %s",
                        probe.stdout.strip(), resolved)
            return resolved

    raise RuntimeError(
        "No usable host Python (3.10-3.13 with venv support) found to build "
        "the isolated MinerU environment. Install Python from python.org and "
        "ensure it is on PATH."
    )


def ensure_venv() -> str:
    """确保隔离 venv 存在且装有 pin 源码的 ``mineru[pipeline]``；返回解释器。"""
    if not submodule_available():
        raise RuntimeError(
            f"MinerU source anchor not found at {submodule_dir()}. "
            "Run: git submodule update --init vendor/MinerU"
        )

    target = venv_python()
    if os.path.exists(target) and _package_importable(target):
        return target

    if not os.path.exists(target):
        logger.info("Creating isolated MinerU venv at %s ...", _VENV_DIR)
        subprocess.run(
            [_find_system_python(), "-m", "venv", str(_VENV_DIR)],
            check=True, timeout=120,
        )
        subprocess.run(
            [target, "-m", "pip", "install", "-U", "pip"],
            check=True, timeout=300,
        )

    logger.info(
        "Installing mineru[pipeline] from pinned source %s (large download: "
        "torch etc.) ...", _SUBMODULE_DIR,
    )
    subprocess.run(
        [target, "-m", "pip", "install",
         f"{_SUBMODULE_DIR}[pipeline]", "six"],
        check=True, timeout=_INSTALL_TIMEOUT, cwd=str(_SUBMODULE_DIR),
    )
    logger.info("Isolated MinerU environment ready: %s", target)
    return target


def _package_importable(interpreter: str) -> bool:
    """隔离 venv 里 mineru.cli.common 是否可导入（残缺安装自愈）。"""
    try:
        result = subprocess.run(
            [interpreter, "-c", "from mineru.cli.common import do_parse"],
            capture_output=True, timeout=60,
            cwd=str(_SUBMODULE_DIR),
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 -- 探测失败按未安装处理
        return False


def setup_mineru_cli() -> None:
    """CLI 入口：构建/修复隔离 venv 并打印用法提示。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    interpreter = ensure_venv()
    print("\nMinerU isolated environment ready.")
    print(
        "pdf2zh will auto-detect this venv (vendor/MinerU/.venv) on every run,\n"
        "so no environment variable is required. To route parsing through it:\n"
    )
    if sys.platform == "win32":
        print(f'  set PDF2ZH_MINERU_PYTHON={interpreter}')
    else:
        print(f'  export PDF2ZH_MINERU_PYTHON={interpreter}')
    print("  pdf2zh --parse-engine magicpdf your.pdf\n")


if __name__ == "__main__":
    setup_mineru_cli()
