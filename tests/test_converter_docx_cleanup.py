"""Temporary-directory ownership rules for the DOCX → PDF conversion helper.

`cleanup_converted_pdf` used to delete any `<temp>/pdf2zh_docx_*` directory,
which also matched user directories that merely shared the prefix. Cleanup must
now be restricted to directories this process actually created.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from asyncio import CancelledError
from pathlib import Path

import pytest

from pdf2zh import converter_docx
from pdf2zh.converter_docx import cleanup_converted_pdf


def test_cleanup_ignores_foreign_temp_dir():
    foreign = Path(tempfile.gettempdir()) / "pdf2zh_docx_not_ours"
    foreign.mkdir(parents=True, exist_ok=True)
    keep = foreign / "user.pdf"
    keep.write_bytes(b"%PDF-1.4 user data")
    try:
        cleanup_converted_pdf(str(foreign / "user.pdf"))
        assert keep.exists(), "user directory must never be deleted"
        assert foreign.exists()
    finally:
        for path in (keep, foreign):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()


def test_cleanup_removes_owned_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    owned = tmp_path / "pdf2zh_docx_owned"
    owned.mkdir()
    pdf = owned / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 converted")

    converter_docx._register_tmpdir(str(owned))
    cleanup_converted_pdf(str(pdf))
    assert not owned.exists(), "owned temp dir must be removed"


def test_cleanup_is_noop_for_path_outside_temp_root(tmp_path, monkeypatch):
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    stray = other_root / "pdf2zh_docx_stray"
    stray.mkdir()
    pdf = stray / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    converter_docx._register_tmpdir(str(stray))
    cleanup_converted_pdf(str(pdf))
    assert stray.exists(), "a moved temp dir must not be deleted blindly"


@pytest.mark.parametrize("exc", [KeyboardInterrupt, CancelledError, RuntimeError])
def test_convert_cleans_up_on_base_exception(tmp_path, monkeypatch, exc):
    """Cancellation (BaseException) must not leak the LibreOffice temp tree."""
    monkeypatch.setattr(converter_docx.shutil, "which", lambda name: "soffice")
    tmp_root = Path(tempfile.gettempdir())
    before = {p.name for p in tmp_root.glob("pdf2zh_docx_*")}

    def _boom(*a, **k):
        raise exc("cancelled")

    monkeypatch.setattr(converter_docx.subprocess, "run", _boom)
    src = tmp_path / "in.docx"
    src.write_bytes(b"x")
    with pytest.raises(exc):
        converter_docx.convert_to_pdf(str(src))

    leaked = {p.name for p in tmp_root.glob("pdf2zh_docx_*")} - before
    for name in leaked:
        shutil.rmtree(tmp_root / name, ignore_errors=True)
    assert not leaked, "temp dir leaked after failure"
