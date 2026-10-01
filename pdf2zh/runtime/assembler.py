"""StreamingAssembler — 流式组装。

不再等待所有 artifact，而是流式写入。

用法：
    assembler = StreamingAssembler(source_pdf)

    assembler.begin()
    for artifact in stream:
        assembler.append(artifact)
    assembler.finalize()
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pikepdf


class StreamingAssembler:
    """流式 PDF 组装器。

    接口：
        - begin(source): 开始
        - append(artifact): 追加页面
        - finalize(): 完成写入

    实现：
        Phase 1: 使用 pikepdf 全量写入
        Phase 2: 增量写入
    """

    def __init__(self) -> None:
        self._pdf: Optional[pikepdf.Pdf] = None
        self._pages: List[Any] = []
        self._metrics = {
            "pages_appended": 0,
            "begin_ms": 0.0,
            "append_ms": 0.0,
            "finalize_ms": 0.0,
        }

    def begin(self, source: str | Path | pikepdf.Pdf) -> None:
        """开始组装。"""
        t0 = time.perf_counter()

        if isinstance(source, pikepdf.Pdf):
            self._pdf = source
        elif isinstance(source, (str, Path)):
            self._pdf = pikepdf.open(str(source))
        else:
            raise TypeError(f"Unsupported source: {type(source)}")

        self._pages = list(self._pdf.pages)
        self._metrics["begin_ms"] = (time.perf_counter() - t0) * 1000

    def append(
        self,
        page_index: int,
        content_stream: Optional[bytes] = None,
        resources: Optional[Dict] = None,
    ) -> None:
        """追加/更新页面。"""
        if self._pdf is None:
            raise RuntimeError("Call begin() first")

        t0 = time.perf_counter()

        if page_index < len(self._pages):
            page = self._pages[page_index]

            # 替换 content stream
            if content_stream is not None:
                stream = pikepdf.Stream(self._pdf, content_stream)
                ref = self._pdf.make_indirect(stream)
                page["/Contents"] = ref

            # 更新 resources
            if resources:
                for key, value in resources.items():
                    page["/Resources"][key] = value

        self._metrics["pages_appended"] += 1
        self._metrics["append_ms"] += (time.perf_counter() - t0) * 1000

    def finalize(self, output: str | Path) -> Dict[str, Any]:
        """完成写入。"""
        if self._pdf is None:
            raise RuntimeError("Call begin() first")

        t0 = time.perf_counter()

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        self._pdf.save(str(output))

        self._metrics["finalize_ms"] = (time.perf_counter() - t0) * 1000

        return {
            "output": str(output),
            "output_size": output.stat().st_size,
            **self._metrics,
        }

    @property
    def metrics(self) -> dict:
        return self._metrics.copy()
