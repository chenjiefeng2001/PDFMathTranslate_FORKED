"""Convert doc/docx files to PDF using LibreOffice headless."""

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from pdf2zh.fs_utils import remove_path_robust

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".doc", ".docx"}

#: 本进程创建过的转换临时目录。清理只认这张表，不靠目录名前缀猜测 ——
#: 前缀匹配会把用户自建的 <temp>/pdf2zh_docx_xxx 一并删掉。
_OWNED_TMPDIRS: set[str] = set()
_OWNED_LOCK = threading.Lock()


def _register_tmpdir(path: str) -> None:
    with _OWNED_LOCK:
        _OWNED_TMPDIRS.add(str(path))


def _discard_tmpdir(path: str) -> None:
    with _OWNED_LOCK:
        _OWNED_TMPDIRS.discard(str(path))


def is_convertible(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def cleanup_converted_pdf(output_path: str) -> None:
    """Remove the temp directory this process created for a converted PDF."""
    path = Path(output_path)
    parent = path.parent
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        with _OWNED_LOCK:
            owned = str(parent) in _OWNED_TMPDIRS
        if not owned:
            return
        if parent.resolve().parent != temp_root:
            # 目录被移动过：宁可留垃圾，也不误删 temp 之外的东西。
            logger.debug("skip cleanup outside temp root: %s", parent)
            return
        remove_path_robust(parent, defer=True)
    except OSError:
        logger.debug(
            "failed to clean converted PDF directory: %s", parent, exc_info=True
        )
    finally:
        _discard_tmpdir(str(parent))


def convert_to_pdf(input_path: str) -> str:
    """Convert a doc/docx file to PDF using LibreOffice.

    Returns the path to the generated temporary PDF file.
    The caller is responsible for cleaning up the temp file.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError(
            "LibreOffice is required to convert doc/docx files. "
            "Install it with: apt-get install libreoffice-core (Linux) "
            "or brew install --cask libreoffice (macOS)"
        )

    p = Path(input_path)
    tmpdir = tempfile.mkdtemp(prefix="pdf2zh_docx_")
    _register_tmpdir(tmpdir)

    try:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmpdir, str(p)],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

        pdf_path = Path(tmpdir) / f"{p.stem}.pdf"
        if not pdf_path.exists():
            raise RuntimeError(f"Conversion produced no output. Expected: {pdf_path}")

        logger.info(f"Converted {p.name} -> {pdf_path}")
        return str(pdf_path)
    except BaseException:
        # BaseException: KeyboardInterrupt / CancelledError 同样必须清理，否则
        # 取消一次转换就会在 temp 根目录留下一整个 LibreOffice 产物树。
        shutil.rmtree(tmpdir, ignore_errors=True)
        _discard_tmpdir(tmpdir)
        raise
