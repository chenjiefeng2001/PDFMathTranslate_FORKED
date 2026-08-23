"""Module 1: Parser Layer.

Unified PDF parsing interface that leverages existing pdf2zh infrastructure
(pdfminer, DocLayout YOLO, FontResolver, TextMetrics) and outputs a clean
stream of RawBlock objects — before any semantic processing.

Usage:
    parser = PDFParser()
    blocks: list[RawBlock] = parser.parse("path/to/doc.pdf")
"""

from __future__ import annotations

__all__ = ["RawBlockType", "RawSpan", "RawBlock", "PDFParser"]

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np

from pdf2zh.font_resolver import FontResolver
from pdf2zh.text_metrics import TextMetrics

logger = logging.getLogger(__name__)


# ── Output data structures ──────────────────────────────────────────────


class RawBlockType(Enum):
    """Raw block types as detected by low-level PDF analysis."""

    TEXT = "text"
    IMAGE = "image"
    VECTOR = "vector"
    FORM_XOBJECT = "form_xobject"
    UNKNOWN = "unknown"


@dataclass
class RawSpan:
    """A single continuous text run with uniform font properties."""

    text: str
    font_name: str = ""
    font_size: float = 0.0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    cid: int = -1
    confidence: float = 1.0
    lang_hint: str = ""


@dataclass
class RawBlock:
    """A primitive block output by the parser, before any semantic labeling.

    This is intentionally minimal — no reading order, no paragraph structure,
    no semantic labels. All higher-level analysis belongs to Modules 2-4.
    """

    block_type: RawBlockType = RawBlockType.UNKNOWN
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    spans: List[RawSpan] = field(default_factory=list)
    page_num: int = 0
    layout_class: int = -1  # DocLayout YOLO class id, -1 = unset
    layout_conf: float = 0.0

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def font_size_avg(self) -> float:
        if not self.spans:
            return 0.0
        return sum(s.font_size for s in self.spans) / len(self.spans)



# ── Parser ──────────────────────────────────────────────────────────────


class PDFParser:
    """Parse PDF files into a flat stream of RawBlocks.

    Bridges to existing infrastructure:
      - pdfminer via PDFPageInterpreterEx for character-level extraction
      - DocLayout YOLO (OnnxModel) for layout element classification
      - FontResolver for font name analysis
      - TextMetrics for glyph measurement (lazy-loaded)

    This parser does NOT perform paragraph reconstruction, reading order
    analysis, or semantic labeling — those belong to Modules 2-4.
    """

    def __init__(self):
        self._font_resolver_cache: dict[str, FontResolver] = {}
        self._text_metrics_cache: dict[str, TextMetrics] = {}

    def parse(
        self,
        pdf_path: str,
        dpi: int = 300,
        layout_model=None,
    ) -> List[RawBlock]:
        """Parse a PDF file into RawBlocks.

        Args:
            pdf_path: Path to a PDF file.
            dpi: Rendering DPI for layout analysis.
            layout_model: Optional DocLayout OnnxModel instance.

        Returns:
            List of RawBlock in page order (not reading order).
        """
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser as PDFMinerParser
        from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
        from pdfminer.converter import PDFPageAggregator
        from pdfminer.layout import LAParams

        blocks: List[RawBlock] = []
        # Use explicit binary mode to avoid Python 3.13 str vs bytes issue
        with open(pdf_path, "rb") as fp:
            parser = PDFMinerParser(fp)
            doc = PDFDocument(parser)
            rsrcmgr = PDFResourceManager()
            laparams = LAParams()
            device = PDFPageAggregator(rsrcmgr, laparams=laparams)
            interpreter = PDFPageInterpreter(rsrcmgr, device)

            for page_num, page in enumerate(PDFPage.create_pages(doc)):
                page_image = None
                if layout_model is not None:
                    page_image = self._render_page_to_image(
                        pdf_path, page_num, dpi
                    )

                interpreter.process_page(page)
                lt_page = device.get_result()

                page_blocks = self._extract_blocks(lt_page, page_num)
                blocks.extend(page_blocks)

            return blocks

    def _extract_blocks(
        self, lt_page, page_num: int, layout_result=None
    ) -> List[RawBlock]:
        """Extract RawBlocks from a pdfminer LTPage.

        Iterates over LTTextBoxHorizontal and LTFigure elements.
        """
        blocks: List[RawBlock] = []
        from pdfminer.layout import (
            LTTextBoxHorizontal, LTFigure, LTAnno, LTChar,
        )

        def _extract_spans_from_line(text_line) -> List[RawSpan]:
            """Extract RawSpan list from a pdfminer text line (LTTextLineHorizontal)."""
            spans: List[RawSpan] = []
            for char_obj in text_line:
                if isinstance(char_obj, LTChar):
                    bx0, by0, bx1, by1 = (
                        char_obj.bbox if isinstance(char_obj.bbox, (tuple, list))
                        else (char_obj.bbox.x0, char_obj.bbox.y0,
                              char_obj.bbox.x1, char_obj.bbox.y1)
                    )
                    spans.append(RawSpan(
                        text=char_obj.get_text(),
                        font_name=self._safe_fontname(
                            char_obj.fontname
                        ),
                        font_size=char_obj.size,
                        bbox=(bx0, by0, bx1, by1),
                        confidence=1.0,
                    ))
                elif isinstance(char_obj, LTAnno) and spans:
                    spans[-1].text += char_obj.get_text()
            return spans

        def _emit_block(element, spans: List[RawSpan]):
            """Emit a RawBlock for the given element and its spans."""
            if not spans:
                return
            ex0, ey0, ex1, ey1 = (
                element.bbox if isinstance(element.bbox, (tuple, list))
                else (element.bbox.x0, element.bbox.y0,
                      element.bbox.x1, element.bbox.y1)
            )
            raw = RawBlock(
                block_type=RawBlockType.TEXT,
                bbox=(ex0, ey0, ex1, ey1),
                spans=spans,
                page_num=page_num,
            )
            blocks.append(raw)

        def _walk(element, depth=0):
            if isinstance(element, LTTextBoxHorizontal):
                # Standard text box — aggregate all lines into one block
                all_spans: List[RawSpan] = []
                for text_line in element:
                    all_spans.extend(_extract_spans_from_line(text_line))
                _emit_block(element, all_spans)
            elif isinstance(element, LTFigure):
                # Figure container — recurse into children to find text
                for child in element:
                    _walk(child, depth + 1)
            elif isinstance(element, LTChar):
                # Stand-alone character (not wrapped in a text line)
                bx0, by0, bx1, by1 = (
                    element.bbox if isinstance(element.bbox, (tuple, list))
                    else (element.bbox.x0, element.bbox.y0,
                          element.bbox.x1, element.bbox.y1)
                )
                spans = [RawSpan(
                    text=element.get_text(),
                    font_name=self._safe_fontname(element.fontname),
                    font_size=element.size,
                    bbox=(bx0, by0, bx1, by1),
                    confidence=1.0,
                )]
                _emit_block(element, spans)
            elif isinstance(element, LTAnno):
                # Stand-alone annotation text (e.g. spaces between chars)
                pass  # skip; will be merged by normalizer
            else:
                # Try to iterate as container (LTPage, etc.) or
                # treat as a stand-alone text line if it looks like one
                element_type_name = type(element).__name__
                if element_type_name == 'LTTextLineHorizontal':
                    spans = _extract_spans_from_line(element)
                    _emit_block(element, spans)
                else:
                    try:
                        for child in element:
                            _walk(child, depth + 1)
                    except TypeError:
                        pass  # skip non-iterable elements (e.g. LTLine, LTRect)

        _walk(lt_page)
        return blocks

    def _render_page_to_image(
        self, pdf_path: str, page_num: int, dpi: int
    ) -> Optional[np.ndarray]:
        """Render a PDF page to a numpy image for layout analysis."""
        try:
            import pymupdf as pm
        except ImportError:
            try:
                import pymupdf as pm
            except ImportError:
                logger.warning("pymupdf not available; layout disabled")
                return None
        try:
            doc = pm.open(pdf_path)
            page = doc[page_num]
            mat = pm.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, 3
            )
            doc.close()
            return img
        except Exception as exc:
            logger.warning("Failed to render page %d: %s", page_num, exc)
            return None

    @staticmethod
    def _safe_fontname(fontname) -> str:
        """Extract readable font name from pdfminer's font identifier."""
        if fontname is None:
            return ""
        name = str(fontname)
        if name.startswith("C") and "+" in name:
            name = name.split("+", 1)[1]
        return name

        return blocks
