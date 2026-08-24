"""
OCR-based text extraction for scanned PDF processing (pdf2zh 2.0).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from pdf2zh.layout_graph import LayoutGraph, TextNode

logger = logging.getLogger(__name__)


@dataclass
class TextSegment:
    """A recognized text segment with position metadata."""

    text: str
    bbox: Tuple[float, float, float, float]
    confidence: float = 1.0
    font_size_est: float = 12.0
    page_num: int = 0


@dataclass
class LayoutRegion:
    """A document layout region."""

    text: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    region_type: str = "text"
    confidence: float = 1.0


class ScanPDFProcessor:
    """Process scanned PDF pages to extract text with positions."""

    def __init__(self, lang="zh-cn", dpi=300, ocr_engine=None):
        self.lang = lang
        self.dpi = dpi
        self.ocr_engine = ocr_engine
        self.scale = dpi / 72.0

    def extract_text_with_positions(self, page_image, page_num=0):
        layout_regions = self.analyze_layout(page_image)
        all_segments = []
        for region in layout_regions:
            if region.region_type not in ("text", "title"):
                continue
            segments = self._ocr_region(page_image, region)
            for seg in segments:
                seg.page_num = page_num
            all_segments.extend(segments)
        return self._sort_by_reading_order(all_segments)

    def analyze_layout(self, page_image):
        h, w = page_image.shape[:2]
        gray = (page_image.mean(axis=2)).astype("uint8")
        binary = _threshold(gray, 200, 255, "binary_inv")
        v_proj = (binary == 255).sum(axis=0)
        v_proj_smooth = _smooth(v_proj, window=max(1, w // 32))
        th = v_proj_smooth.max() * 0.1
        in_gap = False
        columns = []
        col_start = 0
        for x in range(len(v_proj_smooth)):
            if v_proj_smooth[x] < th and not in_gap:
                in_gap = True
                if x - col_start > w * 0.05:
                    columns.append((col_start, x))
            elif v_proj_smooth[x] >= th and in_gap:
                in_gap = False
                col_start = x
        if col_start < w * 0.95:
            columns.append((col_start, w))
        if not columns:
            columns = [(0, w)]
        regions = []
        for col_x0, col_x1 in columns:
            col_mask = binary[:, int(col_x0) : int(col_x1)]
            col_fg = np.any(col_mask == 255, axis=1)
            if col_fg.any():
                text_top = h - int(np.argmax(col_fg))
                text_bot = h - int(len(col_fg) - int(np.argmax(col_fg[::-1])))
            else:
                text_top, text_bot = h, 0
            regions.append(
                LayoutRegion(
                    bbox=(
                        col_x0 / self.scale,
                        text_bot / self.scale,
                        col_x1 / self.scale,
                        text_top / self.scale,
                    ),
                    region_type="text",
                )
            )
        header_h = h * 0.08
        footer_h = h * 0.08
        for region in regions:
            y0, y1 = region.bbox[1], region.bbox[3]
            if y0 < header_h / self.scale:
                region.region_type = "header"
            elif y1 > (h - footer_h) / self.scale:
                region.region_type = "footer"
        return regions

    def _ocr_region(self, page_image, region):
        logger.warning("OCR engine not connected.")
        return []

    def pdf_to_pixel_coords(self, bbox, img_h):
        x0, y0, x1, y1 = bbox
        return (
            int(x0 * self.scale),
            int(img_h - y1 * self.scale),
            int(x1 * self.scale),
            int(img_h - y0 * self.scale),
        )

    def _sort_by_reading_order(self, segments):
        if not segments:
            return segments
        graph = LayoutGraph()
        for i, seg in enumerate(segments):
            graph.add_node(
                TextNode(
                    id=i,
                    x0=seg.bbox[0],
                    y0=seg.bbox[1],
                    x1=seg.bbox[2],
                    y1=seg.bbox[3],
                    text=seg.text,
                    page_num=seg.page_num,
                )
            )
        sorted_nodes = graph._spatial_sort()
        id_map = {n.id: i for i, n in enumerate(sorted_nodes)}
        seg_list = list(segments)
        return sorted(
            seg_list, key=lambda s: id_map.get(seg_list.index(s), float("inf"))
        )


def _smooth(arr, window=5):
    if window < 2:
        return arr
    kernel = np.ones(window) / window
    return np.convolve(arr, kernel, mode="same")


def _threshold(img, thresh, max_val, mode="binary"):
    if mode == "binary_inv":
        return np.where(img > thresh, 0, max_val).astype(np.uint8)
    return np.where(img > thresh, max_val, 0).astype(np.uint8)
