"""
Module: V4 PDF Renderer — VisualTree to PDF output.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List, Optional

from pdf2zh.v3.visual_tree import VisualTree
from pdf2zh.overlay_renderer import OverlaySegment

logger = logging.getLogger(__name__)


@dataclass
class RenderStats:
    pages_rendered: int = 0
    nodes_rendered: int = 0
    text_runs_rendered: int = 0
    total_glyphs: int = 0
    errors: int = 0

    def merge(self, other: "RenderStats") -> "RenderStats":
        return RenderStats(
            pages_rendered=self.pages_rendered + other.pages_rendered,
            nodes_rendered=self.nodes_rendered + other.nodes_rendered,
            text_runs_rendered=self.text_runs_rendered + other.text_runs_rendered,
            total_glyphs=self.total_glyphs + other.total_glyphs,
            errors=self.errors + other.errors,
        )


class V4PDFRenderer:
    """VisualTree to PDF byte stream using pymupdf Document."""

    def __init__(self):
        self._stats = RenderStats()
        # Default page dimensions (US Letter)
        self._page_width = 612.0
        self._page_height = 792.0

    @property
    def stats(self) -> RenderStats:
        return self._stats

    def reset_stats(self) -> None:
        self._stats = RenderStats()

    def render(self, tree: VisualTree, output_path: Optional[str] = None) -> bytes:
        if not tree.is_layout_frozen:
            raise ValueError("VisualTree must be layout-frozen before rendering.")
        self.reset_stats()
        from pymupdf import Document

        doc = Document()
        # Group display entries by page index
        page_entries = self._group_by_page(tree.display_list)
        for page_idx, entries in page_entries.items():
            page = doc.new_page(width=self._page_width, height=self._page_height)
            for entry in entries:
                text = entry.get("text", "")
                if not text.strip():
                    continue
                bbox = entry.get("bbox", (0, 0, 0, 0))
                font_size = entry.get("font_size", 12.0)
                x0, y0, x1, y1 = bbox
                page.insert_text(
                    point=(x0, y0 + font_size * 0.85),
                    text=text,
                    fontsize=font_size,
                    color=(0, 0, 0),
                    overlay=True,
                )
                self._stats.total_glyphs += len(text)
            self._stats.pages_rendered += 1
        result = doc.write(deflate=True, garbage=3)
        doc.close()
        if output_path:
            with open(output_path, "wb") as f:
                f.write(result)
        return result

    def render_to_path(self, tree: VisualTree, output_path: str) -> bytes:
        return self.render(tree, output_path=output_path)

    def _build_overlay_segments(self, tree: VisualTree) -> List[OverlaySegment]:
        """Build OverlaySegments from VisualTree display list (for external use)."""
        from pdf2zh.overlay_renderer import OverlaySegment

        return [
            OverlaySegment(
                text=e.get("text", ""),
                bbox=e.get("bbox", (0, 0, 0, 0)),
                font_size=e.get("font_size", 12.0),
            )
            for e in tree.display_list
        ]

    def _group_by_page(self, display_list: list) -> dict:
        """Group display list entries by page index."""
        pages = {}
        for entry in display_list:
            page_idx = entry.get("page", 0)
            if page_idx not in pages:
                pages[page_idx] = []
            pages[page_idx].append(entry)
        if not pages:
            pages[0] = []
        return pages


def render_visual_tree(tree: VisualTree, output_path: Optional[str] = None) -> bytes:
    renderer = V4PDFRenderer()
    return renderer.render(tree, output_path=output_path)


__all__ = ["RenderStats", "V4PDFRenderer", "render_visual_tree"]
