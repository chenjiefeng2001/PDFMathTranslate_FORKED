"""PDFRenderer — PDF 页面渲染器。

将 PDF 页面渲染为图片，用于视觉对比。

用法：
    renderer = PDFRenderer()
    images = renderer.render("input.pdf", pages=[0, 1, 2])
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pikepdf


@dataclass
class RenderOptions:
    """渲染选项。"""

    dpi: int = 150
    page_indices: Optional[List[int]] = None
    max_pages: int = 50


class PDFRenderer:
    """PDF 渲染器。"""

    def __init__(self) -> None:
        self._available = self._check_availability()

    def _check_availability(self) -> bool:
        """检查渲染器可用性。"""
        try:
            import fitz

            return True
        except ImportError:
            return False

    def render(
        self,
        pdf_path: str,
        options: Optional[RenderOptions] = None,
    ) -> Dict[int, bytes]:
        """渲染 PDF 页面为 PNG。"""
        options = options or RenderOptions()
        result = {}

        if not self._available:
            return result

        try:
            import fitz

            doc = fitz.open(pdf_path)
            pages = options.page_indices or list(
                range(min(len(doc), options.max_pages))
            )

            for idx in pages:
                if idx < len(doc):
                    page = doc[idx]
                    mat = fitz.Matrix(options.dpi / 72, options.dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    result[idx] = pix.tobytes("png")

            doc.close()
        except Exception:
            pass

        return result

    def render_to_file(
        self,
        pdf_path: str,
        output_dir: str,
        options: Optional[RenderOptions] = None,
    ) -> Dict[int, str]:
        """渲染并保存为文件。"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        images = self.render(pdf_path, options)
        file_paths = {}

        for idx, img_bytes in images.items():
            file_path = output_path / f"page_{idx:04d}.png"
            file_path.write_bytes(img_bytes)
            file_paths[idx] = str(file_path)

        return file_paths

    def get_page_count(self, pdf_path: str) -> int:
        """获取页数。"""
        try:
            pdf = pikepdf.open(pdf_path)
            count = len(pdf.pages)
            pdf.close()
            return count
        except Exception:
            return 0
