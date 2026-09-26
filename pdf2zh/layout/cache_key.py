"""LayoutCacheKey — layout 缓存 key。

layout 是最贵且和翻译无关的操作。
换语言不用重新跑 YOLO。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LayoutCacheKey:
    """Layout 缓存 key。

    source_page_hash: 页面内容 hash
    layout_model_version: 模型版本
    detector_version: 检测器版本
    """

    source_page_hash: str = ""
    layout_model_version: str = ""
    detector_version: str = ""
    width: float = 0.0
    height: float = 0.0

    def __str__(self) -> str:
        return (
            f"{self.source_page_hash[:8]}|{self.layout_model_version}|"
            f"{self.detector_version}|{self.width:g}x{self.height:g}"
        )
