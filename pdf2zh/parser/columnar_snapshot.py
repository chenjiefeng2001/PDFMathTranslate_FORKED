"""ColumnarSnapshot — columnar 存储的 PageSnapshot。

保持 PageSnapshot API 不变，内部使用 columnar storage。

用法：
    snapshot = ColumnarSnapshot.from_page(page, page_index=0)

    # 和 PageSnapshot 完全兼容
    for span in snapshot.text_spans:
        print(span.text, span.bbox)

    # 但内存更小
    print(snapshot.memory_summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

import fitz  # PyMuPDF

from pdf2zh.parser.font_interning import FontInterning
from pdf2zh.parser.geometry_store import GeometryStore
from pdf2zh.parser.span_store import TextSpanStore, TextSpanView


@dataclass
class ImageInfo:
    """图片信息。"""

    xref: int = 0
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    width: int = 0
    height: int = 0
    colorspace: str = ""


class ColumnarSnapshot:
    """Columnar 存储的 PageSnapshot。

    内部：
        _span_store: TextSpanStore
        _font_interning: FontInterning
        _images: List[ImageInfo]

    外部 API 和 PageSnapshot 兼容。
    """

    def __init__(
        self, page_index: int = 0, width: float = 0, height: float = 0
    ) -> None:
        self.page_index = page_index
        self.width = width
        self.height = height
        self.rotation = 0

        self._span_store = TextSpanStore()
        self._font_interning = FontInterning()
        self._images: List[ImageInfo] = []
        self._raw_blocks: list = []
        self._content_hash: Optional[str] = None

    @classmethod
    def from_page(cls, page: fitz.Page, page_index: int = 0) -> ColumnarSnapshot:
        """从 PyMuPDF Page 创建。"""
        snap = cls(
            page_index=page_index,
            width=page.rect.width,
            height=page.rect.height,
        )
        snap.rotation = page.rotation

        # Font interning
        font_cache = {}

        # 1. Text spans
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        font_name = span.get("font", "")
                        if font_name not in font_cache:
                            font_cache[font_name] = snap._font_interning.intern(
                                font_name
                            )
                        font_id = font_cache[font_name]

                        snap._span_store.add(
                            text=span.get("text", ""),
                            bbox=tuple(span.get("bbox", (0, 0, 0, 0))),
                            font_id=font_id,
                            font_size=span.get("size", 0),
                            flags=span.get("flags", 0),
                            color=span.get("color", 0),
                        )

        # 2. Images
        img_list = page.get_images()
        for img in img_list:
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                bbox = rects[0] if rects else (0, 0, 0, 0)
            except Exception:
                bbox = (0, 0, 0, 0)

            snap._images.append(
                ImageInfo(
                    xref=xref,
                    bbox=tuple(bbox) if hasattr(bbox, "__iter__") else (0, 0, 0, 0),
                    width=img[2],
                    height=img[3],
                    colorspace=img[5] if len(img) > 5 else "",
                )
            )

        # 3. Raw blocks
        blocks = page.get_text("blocks")
        for b in blocks:
            snap._raw_blocks.append(
                {
                    "bbox": tuple(b[:4]),
                    "text": b[4] if len(b) > 4 else "",
                    "block_no": b[5] if len(b) > 5 else 0,
                    "block_type": b[6] if len(b) > 6 else 0,
                }
            )

        return snap

    # ── Public API (兼容 PageSnapshot) ──

    @property
    def text_spans(self) -> List[TextSpanView]:
        """返回 text spans（兼容 API）。"""
        return list(self._span_store)

    @property
    def images(self) -> List[ImageInfo]:
        return self._images

    @property
    def raw_blocks(self) -> list:
        return self._raw_blocks

    @property
    def fonts(self) -> list:
        """字体列表（从 interning 提取）。"""
        # 简化：返回 font names
        return [
            {"name": self._font_interning.resolve(i)}
            for i in range(self._font_interning.count)
        ]

    @property
    def text_only(self) -> str:
        return " ".join(s.text for s in self._span_store if s.text.strip())

    @property
    def has_images(self) -> bool:
        return len(self._images) > 0

    @property
    def image_count(self) -> int:
        return len(self._images)

    @property
    def content_hash(self) -> str:
        if self._content_hash is None:
            import hashlib

            parts = [s.text for s in self._span_store]
            self._content_hash = hashlib.sha256("".join(parts).encode()).hexdigest()[
                :16
            ]
        return self._content_hash

    def memory_summary(self) -> str:
        """内存使用摘要。"""
        return (
            f"ColumnarSnapshot(page={self.page_index}) | "
            f"spans={len(self._span_store)} | "
            f"fonts={self._font_interning.count} | "
            f"span_store={self._span_store.memory_estimate / 1024:.1f}KB"
        )

    def summary(self) -> str:
        return (
            f"ColumnarSnapshot(page={self.page_index}) | "
            f"{self.width:.0f}x{self.height:.0f} | "
            f"spans={len(self._span_store)} | "
            f"images={len(self._images)} | "
            f"blocks={len(self._raw_blocks)}"
        )
