"""TranslationBatchScheduler — token budget 翻译批处理调度器。

把"页面级翻译"变成"全书级 translation scheduling"。

收益：
    730 pages × 1000 blocks = 730k blocks
    730k requests / 40 = ~18k requests
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class TranslationUnit:
    """翻译单元（合并后的 block 组）。"""

    blocks: List[Any] = field(default_factory=list)
    total_tokens: int = 0
    page_indices: List[int] = field(default_factory=list)
    block_types: List[str] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def source_text(self) -> str:
        return "\n\n".join(
            getattr(b, "text", str(b)) for b in self.blocks if getattr(b, "text", None)
        )


@dataclass
class TranslationBatch:
    """翻译批次。"""

    units: List[TranslationUnit] = field(default_factory=list)
    total_tokens: int = 0
    batch_index: int = 0

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def total_blocks(self) -> int:
        return sum(u.block_count for u in self.units)


class TranslationBatchScheduler:
    """Token budget 翻译批处理调度器。

    用法：
        scheduler = TranslationBatchScheduler(max_tokens=4096)

        for block in blocks:
            scheduler.push(block)

        for batch in scheduler.flush():
            translate(batch)
    """

    def __init__(
        self,
        max_tokens: int = 4096,
        max_blocks: int = 64,
        merge_same_page: bool = True,
    ) -> None:
        self._max_tokens = max(1, int(max_tokens))
        self._max_blocks = max(1, int(max_blocks))
        self._merge_same_page = merge_same_page

        self._pending: List[Any] = []
        self._current_tokens: int = 0
        self._batch_index: int = 0

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数。"""
        # 简单估算：英文 1 token ≈ 4 chars，中文 1 token ≈ 1.5 chars
        cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        en_chars = len(text) - cn_chars
        return int(cn_chars / 1.5 + en_chars / 4)

    def push(self, block: Any) -> bool:
        """push block，返回是否接受。"""
        text = getattr(block, "text", str(block))
        tokens = self.estimate_tokens(text)

        # 检查是否超出预算
        if self._pending and (
            self._current_tokens + tokens > self._max_tokens
            or len(self._pending) >= self._max_blocks
        ):
            # 先 flush
            return False

        self._pending.append(block)
        self._current_tokens += tokens
        return True

    def flush(self) -> Iterator[TranslationBatch]:
        """flush 所有待处理 block。"""
        if not self._pending:
            return

        batch = TranslationBatch(batch_index=self._batch_index)
        current_unit = TranslationUnit()

        for block in self._pending:
            text = getattr(block, "text", str(block))
            tokens = self.estimate_tokens(text)
            page_idx = getattr(block, "page_index", -1)
            block_type = getattr(block, "type", "unknown")

            # 合并条件：同页 + 同类型 + token 预算内
            can_merge = (
                self._merge_same_page
                and current_unit.block_count > 0
                and page_idx in current_unit.page_indices
                and block_type in current_unit.block_types
                and current_unit.total_tokens + tokens <= self._max_tokens
            )

            if can_merge:
                current_unit.blocks.append(block)
                current_unit.total_tokens += tokens
                current_unit.page_indices.append(page_idx)
                current_unit.block_types.append(block_type)
            else:
                # 保存当前 unit，开始新 unit
                if current_unit.block_count > 0:
                    batch.units.append(current_unit)
                    batch.total_tokens += current_unit.total_tokens

                current_unit = TranslationUnit(
                    blocks=[block],
                    total_tokens=tokens,
                    page_indices=[page_idx] if page_idx >= 0 else [],
                    block_types=[block_type],
                )

        # 添加最后一个 unit
        if current_unit.block_count > 0:
            batch.units.append(current_unit)
            batch.total_tokens += current_unit.total_tokens

        # 输出 batch
        if batch.unit_count > 0:
            yield batch
            self._batch_index += 1

        self._pending.clear()
        self._current_tokens = 0
