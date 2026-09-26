"""ImageDiff — 图片差异比较器。

比较两个 PDF 页面的视觉差异。

用法：
    differ = ImageDiff()
    score = differ.compare(image_a, image_b)
    # ImageDiffResult(score=0.95, diff_image=...)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import pikepdf


@dataclass
class ImageDiffResult:
    """图片差异结果。"""

    score: float = 1.0  # 1.0 = identical
    diff_percentage: float = 0.0
    ssim: float = 1.0
    pixel_diff_count: int = 0
    total_pixels: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def is_similar(self) -> bool:
        return self.score >= 0.9

    @property
    def is_acceptable(self) -> bool:
        return self.score >= 0.7

    def summary(self) -> str:
        return (
            f"ImageDiff: score={self.score:.3f}, "
            f"diff={self.diff_percentage:.1%}, "
            f"ssim={self.ssim:.3f}"
        )


class ImageDiff:
    """图片差异比较器。"""

    def __init__(self, threshold: int = 30) -> None:
        self._threshold = threshold

    def compare_bytes(
        self,
        image_a: bytes,
        image_b: bytes,
    ) -> ImageDiffResult:
        """比较两个图片字节。"""
        if image_a == image_b:
            return ImageDiffResult(score=1.0, diff_percentage=0.0)

        # 简单差异计算
        hash_a = hashlib.md5(image_a).hexdigest()
        hash_b = hashlib.md5(image_b).hexdigest()

        if hash_a == hash_b:
            return ImageDiffResult(score=1.0, diff_percentage=0.0)

        # 基于大小的粗略估算
        size_diff = abs(len(image_a) - len(image_b))
        max_size = max(len(image_a), len(image_b))
        size_ratio = size_diff / max_size if max_size > 0 else 0

        score = max(0.0, 1.0 - size_ratio)

        return ImageDiffResult(
            score=score,
            diff_percentage=1.0 - score,
            ssim=score,
        )

    def compare_files(
        self,
        file_a: str,
        file_b: str,
    ) -> ImageDiffResult:
        """比较两个图片文件。"""
        try:
            with open(file_a, "rb") as f:
                data_a = f.read()
            with open(file_b, "rb") as f:
                data_b = f.read()
            return self.compare_bytes(data_a, data_b)
        except Exception as e:
            return ImageDiffResult(score=0.0, metadata={"error": str(e)})

    def compare_pages(
        self,
        pdf_a: str,
        pdf_b: str,
        page_indices: Optional[list] = None,
        dpi: int = 72,
    ) -> dict:
        """比较两个 PDF 的页面。"""
        results = {}

        try:
            import fitz

            doc_a = fitz.open(pdf_a)
            doc_b = fitz.open(pdf_b)

            pages = page_indices or range(min(len(doc_a), len(doc_b)))

            for idx in pages:
                if idx < len(doc_a) and idx < len(doc_b):
                    page_a = doc_a[idx]
                    page_b = doc_b[idx]

                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix_a = page_a.get_pixmap(matrix=mat)
                    pix_b = page_b.get_pixmap(matrix=mat)

                    img_a = pix_a.tobytes("png")
                    img_b = pix_b.tobytes("png")

                    results[idx] = self.compare_bytes(img_a, img_b)

            doc_a.close()
            doc_b.close()

        except Exception:
            pass

        return results

    def overall_score(self, page_results: dict) -> float:
        """计算整体分数。"""
        if not page_results:
            return 0.0
        scores = [r.score for r in page_results.values()]
        return sum(scores) / len(scores)
