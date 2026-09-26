"""PageSnapshot — 一次解析，处处复用。

Phase Priority 1 核心：避免每页重复解析 PDF。

问题：
    现在每页都调用 get_text(), get_images(), get_drawings()...
    每次调用都触发内部解析。

解决：
    一次解析 → PageSnapshot → 后续所有消费者复用

用法：
    snapshot = PageSnapshot.from_page(page, page_index=0)

    # TOC 检测
    toc = detect_toc(snapshot.text_spans)

    # 公式检测
    formulas = detect_formulas(snapshot.fonts, snapshot.text_spans)

    # Layout
    blocks = classify_blocks(snapshot)

    # 翻译
    for span in snapshot.text_spans:
        if is_translatable(span):
            translate(span)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF


@dataclass
class TextSpan:
    """文本片段。"""

    text: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    font_name: str = ""
    font_size: float = 0.0
    flags: int = 0  # bold, italic, etc.
    color: int = 0
    origin: Tuple[float, float] = (0, 0)

    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 2**4)

    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2**1)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class ImageInfo:
    """图片信息。"""

    xref: int = 0
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    width: int = 0
    height: int = 0
    colorspace: str = ""
    alt: str = ""


@dataclass
class DrawingInfo:
    """绘图信息。"""

    items: list = field(default_factory=list)
    color: Tuple[float, float, float] = (0, 0, 0)
    fill: Optional[Tuple[float, float, float]] = None
    width: float = 0.0
    rect: Tuple[float, float, float, float] = (0, 0, 0, 0)


@dataclass
class FontInfo:
    """字体信息。"""

    name: str = ""
    family: str = ""
    size: float = 0.0
    is_bold: bool = False
    is_italic: bool = False
    flags: int = 0


@dataclass
class PageSnapshot:
    """页面快照 — 一次解析，处处复用。

    包含：
        - text_spans: 所有文本片段
        - images: 所有图片
        - drawings: 所有绘图
        - fonts: 所有字体
        - raw_blocks: PyMuPDF 原始 blocks
        - geometry: 页面几何信息
    """

    page_index: int = 0
    width: float = 0.0
    height: float = 0.0
    rotation: int = 0

    text_spans: List[TextSpan] = field(default_factory=list)
    images: List[ImageInfo] = field(default_factory=list)
    drawings: List[DrawingInfo] = field(default_factory=list)
    fonts: List[FontInfo] = field(default_factory=list)
    raw_blocks: List[Dict[str, Any]] = field(default_factory=list)

    # 缓存
    _content_hash: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_page(cls, page: fitz.Page, page_index: int = 0) -> PageSnapshot:
        """从 PyMuPDF Page 创建快照。"""
        snap = cls(
            page_index=page_index,
            width=page.rect.width,
            height=page.rect.height,
            rotation=page.rotation,
        )

        # 1. 文本片段
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # text block
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        snap.text_spans.append(
                            TextSpan(
                                text=span.get("text", ""),
                                bbox=tuple(span.get("bbox", (0, 0, 0, 0))),
                                font_name=span.get("font", ""),
                                font_size=span.get("size", 0),
                                flags=span.get("flags", 0),
                                color=span.get("color", 0),
                                origin=tuple(span.get("origin", (0, 0))),
                            )
                        )

        # 2. 图片
        img_list = page.get_images()
        for img in img_list:
            xref = img[0]
            try:
                bbox = page.get_image_rects(xref)
                if bbox:
                    bbox = bbox[0]
                else:
                    bbox = (0, 0, 0, 0)
            except Exception:
                bbox = (0, 0, 0, 0)

            snap.images.append(
                ImageInfo(
                    xref=xref,
                    bbox=tuple(bbox) if hasattr(bbox, "__iter__") else (0, 0, 0, 0),
                    width=img[2],
                    height=img[3],
                    colorspace=img[5] if len(img) > 5 else "",
                )
            )

        # 3. 绘图
        try:
            drawings = page.get_drawings()
            for d in drawings:
                snap.drawings.append(
                    DrawingInfo(
                        items=d.get("items", []),
                        color=d.get("color", (0, 0, 0)),
                        fill=d.get("fill"),
                        width=d.get("width", 0),
                        rect=tuple(d.get("rect", (0, 0, 0, 0))),
                    )
                )
        except Exception:
            pass

        # 4. 字体（从文本片段提取）
        seen_fonts = set()
        for span in snap.text_spans:
            font_key = (span.font_name, span.font_size)
            if font_key not in seen_fonts:
                seen_fonts.add(font_key)
                snap.fonts.append(
                    FontInfo(
                        name=span.font_name,
                        family=(
                            span.font_name.split("-")[0]
                            if "-" in span.font_name
                            else span.font_name
                        ),
                        size=span.font_size,
                        is_bold=span.is_bold,
                        is_italic=span.is_italic,
                        flags=span.flags,
                    )
                )

        # 5. PyMuPDF blocks
        blocks = page.get_text("blocks")
        for b in blocks:
            snap.raw_blocks.append(
                {
                    "bbox": tuple(b[:4]),
                    "text": b[4] if len(b) > 4 else "",
                    "block_no": b[5] if len(b) > 5 else 0,
                    "block_type": b[6] if len(b) > 6 else 0,
                }
            )

        return snap

    @property
    def content_hash(self) -> str:
        """页面内容 hash（用于缓存）。"""
        if self._content_hash is None:
            parts = []
            for span in self.text_spans:
                parts.append(span.text)
            self._content_hash = hashlib.sha256("".join(parts).encode()).hexdigest()[
                :16
            ]
        return self._content_hash

    @property
    def text_only(self) -> str:
        """所有文本（用于快速比较）。"""
        return " ".join(s.text for s in self.text_spans if s.text.strip())

    @property
    def has_images(self) -> bool:
        return len(self.images) > 0

    @property
    def has_drawings(self) -> bool:
        return len(self.drawings) > 0

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def text_block_count(self) -> int:
        return sum(1 for b in self.raw_blocks if b.get("block_type") == 0)

    @property
    def image_block_count(self) -> int:
        return sum(1 for b in self.raw_blocks if b.get("block_type") == 1)

    def summary(self) -> str:
        return (
            f"PageSnapshot(page={self.page_index}) | "
            f"{self.width:.0f}x{self.height:.0f} | "
            f"spans={len(self.text_spans)} | "
            f"images={len(self.images)} | "
            f"drawings={len(self.drawings)} | "
            f"fonts={len(self.fonts)} | "
            f"blocks={len(self.raw_blocks)}"
        )
