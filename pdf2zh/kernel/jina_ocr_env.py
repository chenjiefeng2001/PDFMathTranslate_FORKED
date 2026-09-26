from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PYTHON_OVERRIDE_ENV = "PDF2ZH_JINA_OCR_PYTHON"
VENV_DIR_ENV = "PDF2ZH_JINA_OCR_VENV_DIR"
DEPENDENCIES = (
    "torch",
    "torchvision",
    "transformers==4.57.3",
    "Pillow",
    "huggingface-hub",
    "numpy",
    "tqdm",
)
_INSTALL_TIMEOUT = 3600
_VENV_LOCK = threading.Lock()


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "pdf2zh"


def default_venv_dir() -> Path:
    value = os.environ.get(VENV_DIR_ENV, "").strip()
    return (
        Path(value).expanduser().resolve()
        if value
        else _user_data_dir() / "jina-ocr-venv"
    )


def venv_python(venv_dir: Optional[Path] = None) -> str:
    base = venv_dir if venv_dir is not None else default_venv_dir()
    if sys.platform == "win32":
        return str(base / "Scripts" / "python.exe")
    return str(base / "bin" / "python")


def default_venv_python() -> Optional[str]:
    candidates: list[Path] = []
    override = os.environ.get(PYTHON_OVERRIDE_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    venv_dir = os.environ.get(VENV_DIR_ENV, "").strip()
    if venv_dir:
        candidates.append(default_venv_dir())
    candidates.append(_user_data_dir() / "jina-ocr-venv")
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return str(resolved)
        executable = venv_python(resolved)
        if os.path.isfile(executable):
            return executable
    return None


def jina_python_override() -> Optional[str]:
    return default_venv_python()


def _import_check_code() -> str:
    names = (
        "torch",
        "torchvision",
        "transformers",
        "PIL",
        "huggingface_hub",
        "numpy",
        "tqdm",
    )
    return (
        "import importlib.util,sys; "
        f"names={names!r}; "
        "sys.exit(0 if all(importlib.util.find_spec(name) is not None "
        "for name in names) else 1)"
    )


def probe_jina_python() -> Optional[str]:
    python = jina_python_override()
    if not python or not os.path.isfile(python):
        return None
    try:
        result = subprocess.run(
            [python, "-c", _import_check_code()],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return python if result.returncode == 0 else None


def _find_system_python() -> str:
    candidates: list[str] = []
    if not getattr(sys, "frozen", False) and sys.executable:
        candidates.append(sys.executable)
    candidates.extend(["python3", "python"])
    seen: set[str] = set()
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode != 0 or probe.args[0] in seen:
            continue
        seen.add(probe.args[0])
        version = tuple(int(item) for item in probe.stdout.strip().split(".")[:2])
        if version < (3, 11):
            continue
        try:
            venv_probe = subprocess.run(
                [probe.args[0], "-m", "venv", "--help"],
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if venv_probe.returncode == 0:
            return str(probe.args[0])
    raise RuntimeError("No usable Python 3.11+ interpreter found for Jina OCR")


def _package_importable(interpreter: str) -> bool:
    try:
        result = subprocess.run(
            [interpreter, "-c", _import_check_code()],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ensure_venv_unlocked(force_recreate: bool = False) -> str:
    existing = default_venv_python()
    if existing and not force_recreate and _package_importable(existing):
        return existing
    target_dir = default_venv_dir()
    if force_recreate:
        import shutil

        shutil.rmtree(target_dir, ignore_errors=True)
        existing = None
    target = venv_python(target_dir)
    if existing and os.path.isfile(target) and _package_importable(target):
        return target
    if not os.path.isfile(target):
        logger.info("Creating isolated Jina OCR venv at %s", target_dir)
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
    logger.info("Installing Jina OCR dependencies into %s", target_dir)
    subprocess.run(
        [target, "-m", "pip", "install", *DEPENDENCIES],
        check=True,
        timeout=_INSTALL_TIMEOUT,
    )
    if not _package_importable(target):
        raise RuntimeError(f"Jina OCR environment verification failed: {target}")
    return target


def ensure_venv(force_recreate: bool = False) -> str:
    with _VENV_LOCK:
        return _ensure_venv_unlocked(force_recreate=force_recreate)


def setup_jina_cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    interpreter = ensure_venv()
    print("Jina OCR isolated environment ready.")
    print(f"Interpreter: {interpreter}")
    print(f"Set {PYTHON_OVERRIDE_ENV} to force this interpreter.")


__all__ = [
    "DEPENDENCIES",
    "PYTHON_OVERRIDE_ENV",
    "VENV_DIR_ENV",
    "default_venv_dir",
    "venv_python",
    "default_venv_python",
    "jina_python_override",
    "probe_jina_python",
    "ensure_venv",
    "setup_jina_cli",
]


if __name__ == "__main__":
    setup_jina_cli()
