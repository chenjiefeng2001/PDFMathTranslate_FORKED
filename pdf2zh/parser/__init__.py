"""PDF Parser — 解析层 IR。

PDF → PageSnapshot → Layout → Translation

PageSnapshot 是解析层的核心 IR：
    - 一次解析，处处复用
    - 避免每页重复调用 get_text/get_images/get_drawings

Priority 8: Columnar storage for memory optimization.
"""

from pdf2zh.parser.font_interning import FontInterning
from pdf2zh.parser.geometry_store import GeometryStore
from pdf2zh.parser.page_snapshot import FontInfo, ImageInfo, PageSnapshot, TextSpan
from pdf2zh.parser.span_store import TextSpanStore, TextSpanView
from pdf2zh.parser.columnar_snapshot import ColumnarSnapshot

__all__ = [
    "PageSnapshot",
    "TextSpan",
    "ImageInfo",
    "FontInfo",
    # Priority 8: Columnar storage
    "ColumnarSnapshot",
    "TextSpanStore",
    "TextSpanView",
    "GeometryStore",
    "FontInterning",
]
