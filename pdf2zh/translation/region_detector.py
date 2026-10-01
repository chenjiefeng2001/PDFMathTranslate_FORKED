"""RepeatedRegionDetector — 重复区域检测。

检测 header/footer/page number 等重复元素。
一次翻译，全书复用。

用法：
    detector = RepeatedRegionDetector()

    for snap in snapshots:
        regions = detector.detect(snap)

    # 获取重复模板
    templates = detector.get_templates()
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from pdf2zh.parser.page_snapshot import PageSnapshot, TextSpan


@dataclass
class RegionFingerprint:
    """区域指纹。"""

    text: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    font_name: str = ""
    font_size: float = 0.0
    page_indices: List[int] = field(default_factory=list)

    @property
    def fingerprint_hash(self) -> str:
        """区域指纹 hash。"""
        parts = [
            self.font_name,
            str(self.font_size),
            self.text.strip()[:50],
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]

    @property
    def count(self) -> int:
        return len(self.page_indices)


@dataclass
class RepeatedRegion:
    """重复区域模板。"""

    fingerprint: RegionFingerprint
    region_type: str = "unknown"  # header, footer, page_number, title, etc.
    translated_text: str = ""
    confidence: float = 0.0

    @property
    def should_translate_once(self) -> bool:
        """是否应该只翻译一次。"""
        return self.region_type in ("header", "footer", "page_number", "title")

    @property
    def should_preserve(self) -> bool:
        """是否应该保留原文。"""
        return self.region_type in ("page_number",)


class RepeatedRegionDetector:
    """重复区域检测器。

    检测逻辑：
        1. 提取每页的 header/footer 区域
        2. 计算 fingerprint
        3. 出现 >=3 次的标记为 repeated
        4. 翻译一次，全书复用

    用法：
        detector = RepeatedRegionDetector(
            header_bbox=(50, 760, 550, 790),
            footer_bbox=(50, 10, 550, 40),
        )

        for snap in snapshots:
            detector.analyze(snap)

        templates = detector.get_templates()
    """

    def __init__(
        self,
        header_ratio: float = 0.9,
        footer_ratio: float = 0.1,
        min_occurrences: int = 3,
    ) -> None:
        self.header_ratio = header_ratio  # header 在页面顶部的比例
        self.footer_ratio = footer_ratio  # footer 在页面底部的比例
        self.min_occurrences = min_occurrences
        self._fingerprints: Dict[str, RegionFingerprint] = {}
        self._page_count = 0

    def analyze(self, snapshot: PageSnapshot) -> List[RepeatedRegion]:
        """分析页面，检测重复区域。"""
        self._page_count += 1
        regions = []

        page_height = snapshot.height
        if page_height <= 0:
            return regions

        for span in snapshot.text_spans:
            if not span.text.strip():
                continue

            # 判断区域类型
            region_type = self._classify_region(span, page_height)
            if region_type == "unknown":
                continue

            # 创建 fingerprint
            fp = RegionFingerprint(
                text=span.text,
                bbox=span.bbox,
                font_name=span.font_name,
                font_size=span.font_size,
            )
            fp_hash = fp.fingerprint_hash

            # 记录
            if fp_hash not in self._fingerprints:
                self._fingerprints[fp_hash] = fp
            self._fingerprints[fp_hash].page_indices.append(snapshot.page_index)

            # 检查是否重复
            fp = self._fingerprints[fp_hash]
            if fp.count >= self.min_occurrences:
                region = RepeatedRegion(
                    fingerprint=fp,
                    region_type=region_type,
                    confidence=min(1.0, fp.count / self._page_count),
                )
                regions.append(region)

        return regions

    def _classify_region(self, span: TextSpan, page_height: float) -> str:
        """分类区域类型。"""
        y_center = (span.bbox[1] + span.bbox[3]) / 2
        y_ratio = y_center / page_height if page_height > 0 else 0.5

        # Header: 页面顶部
        if y_ratio <= self.header_ratio:
            # 检查是否像 chapter title
            text = span.text.strip()
            if any(
                text.startswith(prefix) for prefix in ["Chapter", "CHAPTER", "第", "§"]
            ):
                return "title"
            return "header"

        # Footer: 页面底部
        if y_ratio >= (1 - self.footer_ratio):
            # 检查是否纯数字（页码）
            text = span.text.strip()
            if text.isdigit():
                return "page_number"
            return "footer"

        return "unknown"

    def get_templates(self) -> List[RepeatedRegion]:
        """获取所有重复区域模板。"""
        templates = []
        seen_hashes: Set[str] = set()

        for fp_hash, fp in self._fingerprints.items():
            if fp.count < self.min_occurrences:
                continue
            if fp_hash in seen_hashes:
                continue
            seen_hashes.add(fp_hash)

            # 确定类型
            region_type = "header"
            y_ratio = (fp.bbox[1] + fp.bbox[3]) / 2 / (fp.bbox[3] + 100)
            if y_ratio > 0.5:
                region_type = "footer"

            templates.append(
                RepeatedRegion(
                    fingerprint=fp,
                    region_type=region_type,
                    confidence=min(1.0, fp.count / max(self._page_count, 1)),
                )
            )

        return sorted(templates, key=lambda r: r.fingerprint.count, reverse=True)

    @property
    def repeated_count(self) -> int:
        """重复区域总数。"""
        return sum(
            1 for fp in self._fingerprints.values() if fp.count >= self.min_occurrences
        )

    @property
    def saved_translations(self) -> int:
        """可节省的翻译次数。"""
        return sum(
            fp.count - 1
            for fp in self._fingerprints.values()
            if fp.count >= self.min_occurrences
        )

    def summary(self) -> str:
        return (
            f"RepeatedRegionDetector | pages={self._page_count} | "
            f"regions={self.repeated_count} | "
            f"saved_translations={self.saved_translations}"
        )
