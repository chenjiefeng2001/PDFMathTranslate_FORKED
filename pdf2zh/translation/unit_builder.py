"""TranslationUnitBuilder — block 合并优化。

把小 block 合并成适合翻译的大 unit。

规则：
    - 同页
    - 同 block 类型
    - 相近 bbox
    - token 预算内
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MergeConfig:
    """合并配置。"""

    max_tokens: int = 4096
    max_blocks: int = 64
    same_page: bool = True
    same_type: bool = True
    nearby_bbox: bool = True
    bbox_threshold: float = 100.0  # 像素


class TranslationUnitBuilder:
    """Block 合并构建器。

    用法：
        builder = TranslationUnitBuilder(config)

        for block in blocks:
            unit = builder.try_merge(block)
            if unit is not None:
                translate(unit)

        # flush remaining
        remaining = builder.flush()
    """

    def __init__(self, config: Optional[MergeConfig] = None) -> None:
        self._config = config or MergeConfig()
        self._pending: List[Any] = []
        self._current_tokens: int = 0

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数。"""
        cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        en_chars = len(text) - cn_chars
        return int(cn_chars / 1.5 + en_chars / 4)

    def _can_merge(self, block: Any) -> bool:
        """检查是否可以合并。"""
        if not self._pending:
            return True

        text = getattr(block, "text", str(block))
        tokens = self.estimate_tokens(text)

        # 检查 token 预算
        if self._current_tokens + tokens > self._config.max_tokens:
            return False

        # 检查 block 数量
        if len(self._pending) >= self._config.max_blocks:
            return False

        # 检查同页
        if self._config.same_page:
            block_page = getattr(block, "page_index", -1)
            last_page = getattr(self._pending[-1], "page_index", -2)
            if block_page != last_page:
                return False

        # 检查同类型
        if self._config.same_type:
            block_type = getattr(block, "type", "unknown")
            last_type = getattr(self._pending[-1], "type", "other")
            if block_type != last_type:
                return False

        # 检查 bbox 距离
        if self._config.nearby_bbox:
            block_bbox = getattr(block, "bbox", None)
            last_bbox = getattr(self._pending[-1], "bbox", None)
            if block_bbox and last_bbox:
                # 垂直距离
                dy = abs(block_bbox[1] - last_bbox[3])
                if dy > self._config.bbox_threshold:
                    return False

        return True

    def try_merge(self, block: Any) -> Optional[List[Any]]:
        """尝试合并，返回需要翻译的 block 列表。"""
        if self._can_merge(block):
            self._pending.append(block)
            text = getattr(block, "text", str(block))
            self._current_tokens += self.estimate_tokens(text)
            return None
        else:
            # 返回当前 pending，然后添加新 block
            result = list(self._pending)
            self._pending = [block]
            text = getattr(block, "text", str(block))
            self._current_tokens = self.estimate_tokens(text)
            return result

    def flush(self) -> List[Any]:
        """flush 所有 pending block。"""
        result = list(self._pending)
        self._pending.clear()
        self._current_tokens = 0
        return result
