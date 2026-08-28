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


def _user_data_dir() -> Path:
    """跨平台用户数据目录（桌面/冻结环境可写，与应用安装目录分离）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "pdf2zh"


def default_venv_dir() -> Path:
    """隔离 venv 的优先构建位置（可写）。

    - ``PDF2ZH_MINERU_VENV_DIR`` 显式指定时优先（便于便携/多用户定制）；
    - 否则落在用户数据目录 ``<appdata>/pdf2zh/mineru-venv``，规避安装目录
      （Program Files 等只读位置），使桌面/冻结分发也能就地构建。
    """
    env_dir = os.environ.get("PDF2ZH_MINERU_VENV_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return _user_data_dir() / "mineru-venv"


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

    1. ``PDF2ZH_MINERU_VENV_DIR`` 指定的目录；
    2. 用户数据目录 ``<appdata>/pdf2zh/mineru-venv``（桌面/冻结环境可写）；
    3. 当前解释器所在目录（如 Tauri sidecar 的 onedir）；
    4. 仓库内 submodule 锚点 ``vendor/MinerU/.venv``（开发态）。

    ``PDF2ZH_MINERU_PYTHON`` 由 :func:`pdf2zh.engine_env.mineru_python_override`
    优先处理，无需在此重复。
    """
    candidates: list[Path] = []
    env_dir = os.environ.get("PDF2ZH_MINERU_VENV_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(_user_data_dir() / "mineru-venv")
    candidates.append(Path(sys.executable).parent)
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
        p = venv_python(c)
        if os.path.exists(p):
            return p
    return None


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


def ensure_venv(cuda: bool = False) -> str:
    """确保隔离 venv 存在且装有 mineru[pipeline]；返回解释器路径。

    优先复用已构建且可用的 venv（任意候选位置，见
    :func:`default_venv_python`）；否则新建。源码优先使用仓库内
    ``vendor/MinerU`` submodule（pin 版本、可复现），但桌面/冻结分发未携带
    submodule 时自动回退 PyPI 安装 ``mineru[pipeline]``，使无 submodule 的
    环境也能一键构建。

    构建位置：``PDF2ZH_MINERU_VENV_DIR`` > 仓库 submodule（可写时）>
    用户数据目录（见 :func:`default_venv_dir`）。

    Args:
        cuda: True 时确保 venv 内 torch 为 CUDA 版——若已存在可用的 venv 但
            torch 无 CUDA（``PDF2ZH_MINERU_CUDA=1`` 新建的 CPU 环境），会用
            pytorch 官方 index 原位升级 CUDA torch/torchvision（不动 mineru
            与模型缓存）。用于「启用 MinerU GPU」按钮（桌面端一键切换）。
    """
    existing = default_venv_python()
    if existing and _package_importable(existing):
        if cuda and not _venv_torch_cuda(existing):
            logger.info(
                "Enabling CUDA torch in existing venv %s "
                "(upgrading torch/torchvision from %s)...",
                existing,
                _mineru_torch_index_url(),
            )
            subprocess.run(
                [
                    existing, "-m", "pip", "install",
                    "--upgrade",
                    "torch", "torchvision",
                    "--index-url", _mineru_torch_index_url(),
                ],
                check=True, timeout=_INSTALL_TIMEOUT,
            )
            # 校验并区分两种“仍无 CUDA”：
            #  - torch.version.cuda 为空 → wheel 仍是 CPU 构建（index 未生效/
            #    缺 --upgrade 导致未重装），需检查 index URL；
            #  - torch.version.cuda 有值但 is_available()=False → 已装 CUDA 版
            #    torch，但本机无可用 NVIDIA GPU / 驱动不匹配，需检查驱动。
            torch_cuda_tag = _venv_torch_cuda_tag(existing)
            if torch_cuda_tag is None:
                raise RuntimeError(
                    "MinerU venv 的 torch 仍为 CPU 构建（未装 CUDA wheel）。"
                    "请检查 index URL：%s" % _mineru_torch_index_url()
                )
            if not _venv_torch_cuda(existing):
                raise RuntimeError(
                    "MinerU venv 已安装 CUDA 版 torch（%s）但本机无可用 "
                    "NVIDIA GPU / 驱动不支持 CUDA。请检查 NVIDIA 驱动版本。"
                    % torch_cuda_tag
                )
        return existing

    # submodule 源码可用且所在目录可写时，使用 pin 版本；否则 PyPI 回退。
    use_submodule = submodule_available() and os.access(
        str(_SUBMODULE_DIR.parent), os.W_OK
    )
    if use_submodule:
        target_dir = _VENV_DIR
        source_spec = f"{_SUBMODULE_DIR}[pipeline]"
        install_cwd = str(_SUBMODULE_DIR)
    else:
        target_dir = default_venv_dir()
        source_spec = "mineru[pipeline]"
        install_cwd = None

    target = venv_python(target_dir)
    if os.path.exists(target) and _package_importable(target):
        return target

    if not os.path.exists(target):
        logger.info("Creating isolated MinerU venv at %s ...", target_dir)
        subprocess.run(
            [_find_system_python(), "-m", "venv", str(target_dir)],
            check=True, timeout=120,
        )
        subprocess.run(
            [target, "-m", "pip", "install", "-U", "pip"],
            check=True, timeout=300,
        )

    logger.info(
        "Installing %s into %s (large download: torch etc.) ...",
        source_spec, target_dir,
    )
    if _mineru_cuda_requested():
        # CUDA 版 MinerU：先用 pytorch 官方 index 装 CUDA torch/torchvision，
        # 再装 mineru（其 torch 依赖会复用已装版本）。若不先装，mineru 的
        # torch 依赖会从 PyPI 默认源解析到 CPU 版，导致 `--backend cuda`
        # 请求被 torch.cuda.is_available()=False 拒绝。
        subprocess.run(
            [
                target, "-m", "pip", "install",
                "torch", "torchvision",
                "--index-url", _mineru_torch_index_url(),
            ],
            check=True, timeout=_INSTALL_TIMEOUT,
        )
    subprocess.run(
        [target, "-m", "pip", "install", source_spec, "six"],
        check=True, timeout=_INSTALL_TIMEOUT,
        cwd=install_cwd,
    )
    logger.info("Isolated MinerU environment ready: %s", target)
    return target


#: CUDA torch 安装开关：``PDF2ZH_MINERU_CUDA=1`` 时在 venv 内装 CUDA 版 torch。
_MINERU_CUDA_ENV = "PDF2ZH_MINERU_CUDA"


def _mineru_cuda_requested() -> bool:
    """是否请求 CUDA 版 MinerU（``PDF2ZH_MINERU_CUDA=1``）。"""
    return os.environ.get(_MINERU_CUDA_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _mineru_torch_index_url() -> str:
    """pytorch CUDA wheel 源（``PDF2ZH_MINERU_TORCH_INDEX`` 覆盖，默认 cu126）。"""
    return (
        os.environ.get("PDF2ZH_MINERU_TORCH_INDEX", "").strip()
        or "https://download.pytorch.org/whl/cu126"
    )


def _venv_torch_cuda(interpreter: str) -> bool:
    """轻量探测隔离 venv 内 torch 是否 CUDA 可用（不加载模型，秒级）。"""
    try:
        probe = subprocess.run(
            [
                interpreter, "-c",
                "import torch; print(int(torch.cuda.is_available()))",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 -- 探测失败按不可用处理
        return False
    return probe.returncode == 0 and probe.stdout.strip().endswith("1")


def _venv_torch_cuda_tag(interpreter: str) -> str | None:
    """隔离 venv 内 torch 的 CUDA 构建标识（torch.version.cuda），None=CPU 版。"""
    try:
        probe = subprocess.run(
            [
                interpreter, "-c",
                "import torch; print(torch.version.cuda or '')",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 -- 探测失败按 CPU 处理
        return None
    tag = probe.stdout.strip()
    return tag or None


def _package_importable(interpreter: str) -> bool:
    """隔离 venv 里 mineru.cli.common 是否可导入（残缺安装自愈）。"""
    cwd = str(_SUBMODULE_DIR) if _SUBMODULE_DIR.is_dir() else None
    try:
        result = subprocess.run(
            [interpreter, "-c", "from mineru.cli.common import do_parse"],
            capture_output=True, timeout=60,
            cwd=cwd,
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
        "pdf2zh auto-detects this venv on every run (no environment variable "
        "required). To force a specific interpreter, set:\n"
    )
    if sys.platform == "win32":
        print(f'  set PDF2ZH_MINERU_PYTHON={interpreter}')
    else:
        print(f'  export PDF2ZH_MINERU_PYTHON={interpreter}')
    print("  pdf2zh --parse-engine magicpdf your.pdf\n")
    print("In the desktop app, MinerU is now selectable as the parse engine.")


if __name__ == "__main__":
    setup_mineru_cli()
