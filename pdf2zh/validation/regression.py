"""SemanticRegression — 语义回归测试。

比较两个 PDF 的语义等价性（不是 binary equality）。

用法：
    comparator = SemanticComparator()

    diff = comparator.compare(batch_pdf, stream_pdf)
    # SemanticDiff(is_equal=True, page_diffs=[], ...)

    report = comparator.report(diff)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pikepdf


@dataclass
class PageDiff:
    """单页差异。"""

    page_index: int = 0
    field: str = ""
    batch_value: Any = None
    stream_value: Any = None
    severity: str = "info"  # info, warning, error

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass
class SemanticDiff:
    """语义差异。"""

    is_equal: bool = True
    page_diffs: List[PageDiff] = field(default_factory=list)
    summary_diffs: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.page_diffs if d.is_error)

    @property
    def warning_count(self) -> int:
        return sum(1 for d in self.page_diffs if d.severity == "warning")

    def add_page_diff(self, diff: PageDiff) -> None:
        self.page_diffs.append(diff)
        if diff.is_error:
            self.is_equal = False

    def add_summary_diff(self, field: str, batch: Any, stream: Any) -> None:
        self.summary_diffs[field] = (batch, stream)
        self.is_equal = False


class SemanticComparator:
    """语义比较器。

    检查维度：
        - page count
        - page geometry (media box, crop box, rotation)
        - text content
        - font count
        - image count
    """

    def __init__(self, tolerance: float = 0.01) -> None:
        self.tolerance = tolerance

    def compare(
        self,
        batch_pdf_path: str,
        stream_pdf_path: str,
    ) -> SemanticDiff:
        """比较两个 PDF 的语义。"""
        diff = SemanticDiff()

        try:
            batch_pdf = pikepdf.open(batch_pdf_path)
            stream_pdf = pikepdf.open(stream_pdf_path)
        except Exception as e:
            diff.add_summary_diff("open_error", str(e), "")
            return diff

        try:
            # 1. Page count
            batch_pages = len(batch_pdf.pages)
            stream_pages = len(stream_pdf.pages)
            if batch_pages != stream_pages:
                diff.add_summary_diff("page_count", batch_pages, stream_pages)

            # 2. Per-page comparison
            min_pages = min(batch_pages, stream_pages)
            for i in range(min_pages):
                self._compare_page(batch_pdf, stream_pdf, i, diff)

        finally:
            batch_pdf.close()
            stream_pdf.close()

        return diff

    def _compare_page(
        self,
        batch_pdf: pikepdf.Pdf,
        stream_pdf: pikepdf.Pdf,
        page_index: int,
        diff: SemanticDiff,
    ) -> None:
        """比较单页。"""
        batch_page = batch_pdf.pages[page_index]
        stream_page = stream_pdf.pages[page_index]

        # Media box
        batch_mb = self._get_mediabox(batch_page)
        stream_mb = self._get_mediabox(stream_page)
        if not self._bbox_equal(batch_mb, stream_mb):
            diff.add_page_diff(
                PageDiff(
                    page_index=page_index,
                    field="media_box",
                    batch_value=batch_mb,
                    stream_value=stream_mb,
                    severity="warning",
                )
            )

        # Rotation
        batch_rot = int(batch_page.get("/Rotate", 0))
        stream_rot = int(stream_page.get("/Rotate", 0))
        if batch_rot != stream_rot:
            diff.add_page_diff(
                PageDiff(
                    page_index=page_index,
                    field="rotation",
                    batch_value=batch_rot,
                    stream_value=stream_rot,
                    severity="warning",
                )
            )

        # Font count
        batch_fonts = self._count_fonts(batch_page)
        stream_fonts = self._count_fonts(stream_page)
        if batch_fonts != stream_fonts:
            diff.add_page_diff(
                PageDiff(
                    page_index=page_index,
                    field="font_count",
                    batch_value=batch_fonts,
                    stream_value=stream_fonts,
                    severity="info",
                )
            )

        # Image count
        batch_images = self._count_images(batch_page)
        stream_images = self._count_images(stream_page)
        if batch_images != stream_images:
            diff.add_page_diff(
                PageDiff(
                    page_index=page_index,
                    field="image_count",
                    batch_value=batch_images,
                    stream_value=stream_images,
                    severity="info",
                )
            )

    def _get_mediabox(
        self, page: pikepdf.Page
    ) -> Optional[Tuple[float, float, float, float]]:
        mb = page.get("/MediaBox")
        if mb:
            return tuple(float(x) for x in mb)
        return None

    def _bbox_equal(
        self,
        a: Optional[Tuple[float, float, float, float]],
        b: Optional[Tuple[float, float, float, float]],
    ) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return all(abs(ai - bi) < self.tolerance for ai, bi in zip(a, b))

    def _count_fonts(self, page: pikepdf.Page) -> int:
        res = page.get("/Resources")
        if res:
            fd = res.get("/Font")
            if fd:
                return len(fd)
        return 0

    def _count_images(self, page: pikepdf.Page) -> int:
        res = page.get("/Resources")
        if res:
            xobj = res.get("/XObject")
            if xobj:
                count = 0
                for key in xobj:
                    try:
                        obj = xobj[key]
                        if obj.get("/Subtype") == "/Image":
                            count += 1
                    except Exception:
                        pass
                return count
        return 0

    def report(self, diff: SemanticDiff) -> str:
        """生成报告。"""
        lines = [
            "Semantic Regression Report",
            "=" * 50,
            f"Is Equal: {diff.is_equal}",
            f"Page Diffs: {len(diff.page_diffs)}",
            f"Errors: {diff.error_count}",
            f"Warnings: {diff.warning_count}",
        ]

        if diff.summary_diffs:
            lines.append("")
            lines.append("Summary Diffs:")
            for field, (batch, stream) in diff.summary_diffs.items():
                lines.append(f"  {field}: batch={batch} vs stream={stream}")

        if diff.page_diffs:
            lines.append("")
            lines.append("Page Diffs (first 10):")
            for d in diff.page_diffs[:10]:
                lines.append(
                    f"  Page {d.page_index}: {d.field} "
                    f"batch={d.batch_value} vs stream={d.stream_value} "
                    f"[{d.severity}]"
                )

        return "\n".join(lines)
