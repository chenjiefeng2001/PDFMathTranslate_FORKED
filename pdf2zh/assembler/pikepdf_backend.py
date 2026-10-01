"""PikepdfBackend — pikepdf backend。

默认 backend：pikepdf 作为 PDF engine。
支持 font reuse、resource pool、incremental update。
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import pikepdf

from pdf2zh.assembler.backend import (
    PDFBackend,
    PDFBackendCapabilities,
    BackendMetrics,
)


class PikepdfBackend(PDFBackend):
    """Pikepdf backend。"""

    def __init__(self) -> None:
        self._pdf: Optional[pikepdf.Pdf] = None
        self._pages = []
        self._metrics = BackendMetrics(backend_name="pikepdf")

    @property
    def name(self) -> str:
        return "pikepdf"

    @property
    def capabilities(self) -> PDFBackendCapabilities:
        return PDFBackendCapabilities(
            supports_incremental=True,
            supports_font_reuse=True,
            supports_resource_pool=True,
            supports_annotation=True,
            supports_form_xobject=True,
            supports_transparency=True,
        )

    def begin(self, source: str | pikepdf.Pdf) -> None:
        """开始组装。"""
        if isinstance(source, pikepdf.Pdf):
            self._pdf = source
        elif isinstance(source, str):
            self._pdf = pikepdf.open(source)
        else:
            raise TypeError(f"Unsupported source: {type(source)}")

        self._pages = list(self._pdf.pages)
        self._metrics.objects_before = len(self._pdf.objects)

    def append(
        self,
        page_index: int,
        content_stream: Optional[bytes] = None,
        resources: Optional[Dict] = None,
    ) -> None:
        """追加/更新页面。"""
        if self._pdf is None:
            raise RuntimeError("Call begin() first")

        if page_index < len(self._pages):
            page = self._pages[page_index]

            if content_stream is not None:
                stream = pikepdf.Stream(self._pdf, content_stream)
                ref = self._pdf.make_indirect(stream)
                page["/Contents"] = ref

            if resources:
                res = page.get("/Resources")
                if res is None:
                    res = pikepdf.Dictionary()
                    page["/Resources"] = res
                for key, value in resources.items():
                    res[key] = value

        self._metrics.pages_assembled += 1

    def finalize(self, output: str) -> BackendMetrics:
        """完成写入。"""
        if self._pdf is None:
            raise RuntimeError("Call begin() first")

        t0 = time.perf_counter()
        self._pdf.save(output)
        self._metrics.write_ms = (time.perf_counter() - t0) * 1000

        # 验证
        try:
            test = pikepdf.open(output)
            self._metrics.valid = True
            self._metrics.objects_after = len(test.objects)
            test.close()
        except Exception:
            self._metrics.valid = False

        self._pdf.close()
        return self._metrics
