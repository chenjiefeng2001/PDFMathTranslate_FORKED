"""LayoutResult — 统一 layout 输出。

Fast path 和 YOLO 路径都输出 LayoutResult。
后续 assembler 不知道来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LayoutBlock:
    """单个 layout block。"""

    block_type: str = "text"  # text, image, table, formula, title, etc.
    bbox: tuple = (0, 0, 0, 0)  # x0, y0, x1, y1
    confidence: float = 0.0
    text: str = ""
    column: int = 0
    order: int = 0


@dataclass
class LayoutResult:
    """Layout 推理结果。"""

    page_index: int = 0
    blocks: List[LayoutBlock] = field(default_factory=list)
    model_version: str = "rule_v1"
    inference_ms: float = 0.0
    confidence: float = 0.0
    source: str = "fast_path"  # fast_path | model | cache

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def text_blocks(self) -> List[LayoutBlock]:
        return [b for b in self.blocks if b.block_type == "text"]

    @property
    def image_blocks(self) -> List[LayoutBlock]:
        return [b for b in self.blocks if b.block_type == "image"]

    @property
    def table_blocks(self) -> List[LayoutBlock]:
        return [b for b in self.blocks if b.block_type == "table"]

    @property
    def title_blocks(self) -> List[LayoutBlock]:
        return [b for b in self.blocks if b.block_type == "title"]

    def summary(self) -> str:
        types = {}
        for b in self.blocks:
            types[b.block_type] = types.get(b.block_type, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(types.items())]
        return (
            f"LayoutResult(page={self.page_index}) | "
            f"blocks={self.block_count} | {', '.join(parts)} | "
            f"source={self.source} | {self.inference_ms:.1f}ms"
        )
