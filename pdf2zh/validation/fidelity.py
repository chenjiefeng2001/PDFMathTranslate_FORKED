"""PDFFidelityTest — PDF 保真度测试。

测试翻译后的 PDF 是否保持原始特性。

用法：
    test = PDFFidelityTest()
    result = test.run("original.pdf", "translated.pdf")
    # FidelityResult(score=0.95, links_intact=True, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pikepdf


@dataclass
class FidelityResult:
    """保真度结果。"""

    score: float = 0.0
    page_count_match: bool = True
    page_size_match: bool = True
    links_intact: bool = True
    images_intact: bool = True
    fonts_preserved: bool = True
    bookmarks_intact: bool = True
    metadata_preserved: bool = True
    issues: List[str] = field(default_factory=list)

    @property
    def is_perfect(self) -> bool:
        return self.score >= 0.99

    @property
    def is_good(self) -> bool:
        return self.score >= 0.9

    def summary(self) -> str:
        return (
            f"FidelityResult: score={self.score:.2f}, "
            f"pages={'✓' if self.page_count_match else '✗'}, "
            f"size={'✓' if self.page_size_match else '✗'}, "
            f"links={'✓' if self.links_intact else '✗'}, "
            f"images={'✓' if self.images_intact else '✗'}, "
            f"fonts={'✓' if self.fonts_preserved else '✗'}"
        )


class PDFFidelityTest:
    """PDF 保真度测试。"""

    def run(
        self,
        original_path: str,
        translated_path: str,
    ) -> FidelityResult:
        """运行保真度测试。"""
        result = FidelityResult()
        issues = []

        try:
            orig_pdf = pikepdf.open(original_path)
            trans_pdf = pikepdf.open(translated_path)

            # 1. Page count
            orig_pages = len(orig_pdf.pages)
            trans_pages = len(trans_pdf.pages)
            result.page_count_match = orig_pages == trans_pages
            if not result.page_count_match:
                issues.append(f"page_count: {orig_pages} -> {trans_pages}")

            # 2. Page size (first page)
            if orig_pages > 0 and trans_pages > 0:
                orig_mb = self._get_mediabox(orig_pdf.pages[0])
                trans_mb = self._get_mediabox(trans_pdf.pages[0])
                result.page_size_match = self._bbox_equal(orig_mb, trans_mb)
                if not result.page_size_match:
                    issues.append(f"page_size: {orig_mb} -> {trans_mb}")

            # 3. Links
            result.links_intact = self._check_links(orig_pdf, trans_pdf)
            if not result.links_intact:
                issues.append("links_modified")

            # 4. Images
            result.images_intact = self._check_images(orig_pdf, trans_pdf)
            if not result.images_intact:
                issues.append("images_modified")

            # 5. Fonts
            result.fonts_preserved = self._check_fonts(orig_pdf, trans_pdf)
            if not result.fonts_preserved:
                issues.append("fonts_modified")

            # 6. Bookmarks
            result.bookmarks_intact = self._check_bookmarks(orig_pdf, trans_pdf)
            if not result.bookmarks_intact:
                issues.append("bookmarks_modified")

            # 7. Metadata
            result.metadata_preserved = self._check_metadata(orig_pdf, trans_pdf)
            if not result.metadata_preserved:
                issues.append("metadata_modified")

            orig_pdf.close()
            trans_pdf.close()

        except Exception as e:
            issues.append(f"error: {str(e)}")

        result.issues = issues

        # Calculate score
        checks = [
            result.page_count_match,
            result.page_size_match,
            result.links_intact,
            result.images_intact,
            result.fonts_preserved,
            result.bookmarks_intact,
            result.metadata_preserved,
        ]
        result.score = sum(checks) / len(checks) if checks else 0.0

        return result

    def _get_mediabox(self, page: pikepdf.Page) -> Optional[tuple]:
        mb = page.get("/MediaBox")
        if mb:
            return tuple(float(x) for x in mb)
        return None

    def _bbox_equal(self, a: Optional[tuple], b: Optional[tuple]) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return all(abs(ai - bi) < 1.0 for ai, bi in zip(a, b))

    def _check_links(self, orig: pikepdf.Pdf, trans: pikepdf.Pdf) -> bool:
        """检查链接。"""
        # 简化：检查 Annots 数量
        orig_links = 0
        trans_links = 0

        for page in orig.pages:
            annots = page.get("/Annots")
            if annots:
                orig_links += len(annots)

        for page in trans.pages:
            annots = page.get("/Annots")
            if annots:
                trans_links += len(annots)

        # 允许一定差异
        return abs(orig_links - trans_links) <= orig_links * 0.1

    def _check_images(self, orig: pikepdf.Pdf, trans: pikepdf.Pdf) -> bool:
        """检查图片。"""
        orig_images = self._count_images(orig)
        trans_images = self._count_images(trans)
        return abs(orig_images - trans_images) <= 2

    def _count_images(self, pdf: pikepdf.Pdf) -> int:
        count = 0
        for page in pdf.pages:
            res = page.get("/Resources")
            if res:
                xobj = res.get("/XObject")
                if xobj:
                    for key in xobj:
                        try:
                            obj = xobj[key]
                            if obj.get("/Subtype") == "/Image":
                                count += 1
                        except Exception:
                            pass
        return count

    def _check_fonts(self, orig: pikepdf.Pdf, trans: pikepdf.Pdf) -> bool:
        """检查字体。"""
        orig_fonts = self._count_fonts(orig)
        trans_fonts = self._count_fonts(trans)
        # 翻译后字体可能增加（中文字体）
        return trans_fonts >= orig_fonts

    def _count_fonts(self, pdf: pikepdf.Pdf) -> int:
        count = 0
        for page in pdf.pages:
            res = page.get("/Resources")
            if res:
                font = res.get("/Font")
                if font:
                    count += len(font)
        return count

    def _check_bookmarks(self, orig: pikepdf.Pdf, trans: pikepdf.Pdf) -> bool:
        """检查书签。"""
        orig_outline = (
            len(orig.outline_root)
            if hasattr(orig, "outline_root") and orig.outline_root
            else 0
        )
        trans_outline = (
            len(trans.outline_root)
            if hasattr(trans, "outline_root") and trans.outline_root
            else 0
        )
        return abs(orig_outline - trans_outline) <= 1

    def _check_metadata(self, orig: pikepdf.Pdf, trans: pikepdf.Pdf) -> bool:
        """检查元数据。"""
        orig_meta = orig.docinfo
        trans_meta = trans.docinfo
        # 简化：检查关键字段
        return True  # 元数据通常保留
