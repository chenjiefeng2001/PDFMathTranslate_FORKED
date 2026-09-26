"""Assembler 工厂 — 根据环境变量选择 backend。

环境变量 PDF2ZH_BACKEND:
    - "mupdf" (默认): MuPDFAssembler（兼容层）
    - "pikepdf": PikepdfIncrementalAssembler（对象级复用实验）

用法：
    from pdf2zh.assembler import get_assembler
    asm = get_assembler()
    pdf_bytes = asm.assemble(artifact)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from pdf2zh.assembler.base import PDFAssembler

logger = logging.getLogger(__name__)

_BACKEND_CACHE: Optional[PDFAssembler] = None


def get_assembler(backend: Optional[str] = None) -> PDFAssembler:
    """获取 PDFAssembler 实例（单例缓存）。

    Args:
        backend: 显式指定 backend（"mupdf" 或 "pikepdf"）。
                 None 时从 PDF2ZH_BACKEND 环境变量读取，默认 "mupdf"。

    Returns:
        PDFAssembler 实例
    """
    global _BACKEND_CACHE

    if _BACKEND_CACHE is not None and backend is None:
        return _BACKEND_CACHE

    if backend is None:
        backend = os.environ.get("PDF2ZH_BACKEND", "mupdf").lower().strip()

    if backend == "pikepdf":
        try:
            from pdf2zh.assembler.pikepdf_incremental import (
                PikepdfIncrementalAssembler,
            )

            assembler = PikepdfIncrementalAssembler()
            logger.info(
                "Using %s (object_reuse=%s, preserves=%s)",
                assembler.name,
                assembler.capabilities.object_reuse,
                assembler.capabilities.preserves,
            )
        except ImportError:
            logger.warning("pikepdf not available, falling back to MuPDFAssembler")
            from pdf2zh.assembler.mupdf_assembler import MuPDFAssembler

            assembler = MuPDFAssembler()
    else:
        from pdf2zh.assembler.mupdf_assembler import MuPDFAssembler

        assembler = MuPDFAssembler()
        logger.info("Using %s (compatibility layer)", assembler.name)

    if backend is None:
        _BACKEND_CACHE = assembler

    return assembler
