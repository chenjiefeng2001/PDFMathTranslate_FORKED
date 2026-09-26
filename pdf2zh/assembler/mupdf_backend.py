"""MuPDFBackend — MuPDF legacy backend。

Fallback backend：复杂 annotation、特殊 PDF、broken PDF repair。
作为 oracle implementation 保留。
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import fitz  # PyMuPDF

from pdf2zh.assembler.backend import (
    PDFBackend,
    PDFBackendCapabilities,
    BackendMetrics,
)


class MuPDFBackend(PDFBackend):
    """MuPDF legacy backend。"""

    def __init__(self) -> None:
        self._doc: Optional[fitz.Document] = None
        self._metrics = BackendMetrics(backend_name="mupdf")

    @property
    def name(self) -> str:
        return "mupdf"

    @property
    def capabilities(self) -> PDFBackendCapabilities:
        return PDFBackendCapabilities(
            supports_incremental=False,
            supports_font_reuse=False,
            supports_resource_pool=False,
            supports_annotation=True,
            supports_form_xobject=True,
            supports_transparency=True,
        )

    def begin(self, source: str | fitz.Document) -> None:
        """开始组装。"""
        if isinstance(source, fitz.Document):
            self._doc = source
        elif isinstance(source, str):
            self._doc = fitz.open(source)
        else:
            raise TypeError(f"Unsupported source: {type(source)}")

    def append(
        self,
        page_index: int,
        content_stream: Optional[bytes] = None,
        resources: Optional[Dict] = None,
    ) -> None:
        """追加/更新页面。"""
        if self._doc is None:
            raise RuntimeError("Call begin() first")

        self._metrics.pages_assembled += 1

    def finalize(self, output: str) -> BackendMetrics:
        """完成写入。"""
        if self._doc is None:
            raise RuntimeError("Call begin() first")

        t0 = time.perf_counter()
        self._doc.save(output)
        self._metrics.write_ms = (time.perf_counter() - t0) * 1000

        # 验证
        try:
            test = fitz.open(output)
            self._metrics.valid = True
            self._metrics.objects_after = len(test)
            test.close()
        except Exception:
            self._metrics.valid = False

        self._doc.close()
        return self._metrics
