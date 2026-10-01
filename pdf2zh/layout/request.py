"""LayoutRequest — 轻量级 layout 请求。

模型只需要：页面图像 + 尺寸 + id
不携带 text_spans/fonts/images 等重对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class LayoutRequest:
    """Layout 推理请求。"""

    page_index: int = 0
    source_page_hash: str = ""
    image: Optional[np.ndarray] = None  # 页面渲染图
    width: float = 0.0
    height: float = 0.0
    priority: int = 0

    @property
    def area(self) -> float:
        return self.width * self.height

    def __hash__(self) -> int:
        return hash((self.page_index, self.source_page_hash, self.width, self.height))
