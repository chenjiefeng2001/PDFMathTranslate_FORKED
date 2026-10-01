"""LayoutStage — layout stage 抽象。

每个 stage 实现：
    - can_handle(snapshot): 是否能处理
    - infer(snapshot): 推理
    - confidence(result): 置信度
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from pdf2zh.layout.result import LayoutResult
from pdf2zh.parser.page_snapshot import PageSnapshot


@dataclass
class LayoutConfidence:
    """多维度置信度。"""

    geometry_score: float = 0.0
    reading_order_score: float = 0.0
    block_consistency_score: float = 0.0
    semantic_score: float = 0.0

    @property
    def total(self) -> float:
        """加权总分。"""
        return (
            0.4 * self.geometry_score
            + 0.3 * self.reading_order_score
            + 0.2 * self.block_consistency_score
            + 0.1 * self.semantic_score
        )

    @property
    def is_confident(self) -> bool:
        return self.total >= 0.7

    def summary(self) -> str:
        return (
            f"Confidence | geo={self.geometry_score:.2f} "
            f"order={self.reading_order_score:.2f} "
            f"block={self.block_consistency_score:.2f} "
            f"semantic={self.semantic_score:.2f} "
            f"total={self.total:.2f}"
        )


class LayoutStage(ABC):
    """Layout stage 抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stage 名称。"""

    @abstractmethod
    def can_handle(self, snapshot: PageSnapshot) -> bool:
        """是否能处理该页面。"""

    @abstractmethod
    def infer(self, snapshot: PageSnapshot) -> LayoutResult:
        """推理，返回 LayoutResult。"""

    def confidence(
        self, snapshot: PageSnapshot, result: LayoutResult
    ) -> LayoutConfidence:
        """计算置信度。"""
        return LayoutConfidence()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
