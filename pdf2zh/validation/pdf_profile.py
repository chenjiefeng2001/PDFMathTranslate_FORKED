"""PDFProfile — PDF 难度分类器。

分析 PDF 特征，预测翻译难度。

用法：
    profile = PDFProfile.from_pdf("input.pdf")
    print(profile.summary())
    # PDFProfile(text_density=0.82, formula_ratio=0.15, difficulty="medium")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pikepdf


@dataclass
class PDFProfile:
    """PDF 特征画像。"""

    # 基本信息
    page_count: int = 0
    file_size_mb: float = 0.0

    # 内容特征
    text_density: float = 0.0  # 文本密度 (0-1)
    image_ratio: float = 0.0  # 图片占比
    formula_ratio: float = 0.0  # 公式占比
    table_ratio: float = 0.0  # 表格占比
    toc_pages: int = 0  # 目录页数

    # 结构特征
    column_count: int = 1  # 平均栏数
    font_count: int = 0  # 字体数量
    has_toc: bool = False  # 是否有目录
    has_index: bool = False  # 是否有索引

    # 难度评估
    difficulty: str = "unknown"  # easy, medium, hard, extreme
    difficulty_score: float = 0.0  # 0-1

    # 预估
    estimated_translation_time_ms: float = 0.0
    estimated_api_calls: int = 0
    estimated_cost_usd: float = 0.0

    @classmethod
    def from_pdf(cls, pdf_path: str) -> PDFProfile:
        """从 PDF 创建画像。"""
        profile = cls()

        try:
            pdf = pikepdf.open(pdf_path)
            profile.page_count = len(pdf.pages)

            # 分析前 10 页
            sample_pages = min(10, profile.page_count)
            total_images = 0
            total_fonts = set()

            for i in range(sample_pages):
                page = pdf.pages[i]

                # 图片计数
                res = page.get("/Resources")
                if res:
                    xobj = res.get("/XObject")
                    if xobj:
                        for key in xobj:
                            try:
                                obj = xobj[key]
                                if obj.get("/Subtype") == "/Image":
                                    total_images += 1
                            except Exception:
                                pass

                    # 字体计数
                    font = res.get("/Font")
                    if font:
                        for key in font:
                            total_fonts.add(str(key))

            profile.image_ratio = total_images / sample_pages if sample_pages > 0 else 0
            profile.font_count = len(total_fonts)

            # 检测目录
            profile.has_toc = cls._detect_toc(pdf)
            if profile.has_toc:
                profile.toc_pages = cls._estimate_toc_pages(pdf)

            pdf.close()

        except Exception:
            pass

        # 计算难度
        profile._calculate_difficulty()

        return profile

    @classmethod
    def _detect_toc(cls, pdf: pikepdf.Pdf) -> bool:
        """检测目录。"""
        # 简化：检查前 5 页是否有目录特征
        for i in range(min(5, len(pdf.pages))):
            page = pdf.pages[i]
            # 检查是否有连续数字（页码）
            # 这是简化检测
            pass
        return False

    @classmethod
    def _estimate_toc_pages(cls, pdf: pikepdf.Pdf) -> int:
        """估算目录页数。"""
        return 0  # 需要更复杂的检测

    def _calculate_difficulty(self) -> None:
        """计算难度分数。"""
        score = 0.0

        # 页数
        if self.page_count > 500:
            score += 0.3
        elif self.page_count > 100:
            score += 0.2
        elif self.page_count > 50:
            score += 0.1

        # 图片密度
        if self.image_ratio > 5:
            score += 0.2
        elif self.image_ratio > 2:
            score += 0.1

        # 字体数量
        if self.font_count > 10:
            score += 0.15
        elif self.font_count > 5:
            score += 0.1

        # 公式/表格
        if self.formula_ratio > 0.2:
            score += 0.15
        if self.table_ratio > 0.2:
            score += 0.1

        # 目录
        if self.has_toc:
            score += 0.1

        self.difficulty_score = min(1.0, score)

        # 难度等级
        if self.difficulty_score >= 0.7:
            self.difficulty = "extreme"
        elif self.difficulty_score >= 0.5:
            self.difficulty = "hard"
        elif self.difficulty_score >= 0.3:
            self.difficulty = "medium"
        else:
            self.difficulty = "easy"

        # 预估翻译时间
        base_time_per_page = 500  # ms
        self.estimated_translation_time_ms = (
            self.page_count * base_time_per_page * (1 + self.difficulty_score)
        )

        # 预估 API 调用
        avg_blocks_per_page = 10
        self.estimated_api_calls = self.page_count * avg_blocks_per_page

        # 预估成本
        avg_cost_per_call = 0.0001  # USD
        self.estimated_cost_usd = self.estimated_api_calls * avg_cost_per_call

    def summary(self) -> str:
        lines = [
            f"PDF Profile:",
            f"  Pages: {self.page_count}",
            f"  Size: {self.file_size_mb:.2f} MB",
            f"  Difficulty: {self.difficulty} ({self.difficulty_score:.2f})",
            f"  Image ratio: {self.image_ratio:.1f} images/page",
            f"  Fonts: {self.font_count}",
            f"  Has TOC: {self.has_toc}",
            f"  Estimated time: {self.estimated_translation_time_ms/1000:.1f}s",
            f"  Estimated API calls: {self.estimated_api_calls}",
            f"  Estimated cost: ${self.estimated_cost_usd:.4f}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_count": self.page_count,
            "file_size_mb": self.file_size_mb,
            "difficulty": self.difficulty,
            "difficulty_score": self.difficulty_score,
            "image_ratio": self.image_ratio,
            "font_count": self.font_count,
            "has_toc": self.has_toc,
            "estimated_translation_time_ms": self.estimated_translation_time_ms,
            "estimated_api_calls": self.estimated_api_calls,
            "estimated_cost_usd": self.estimated_cost_usd,
        }
