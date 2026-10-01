"""GeometryStore — 紧凑几何存储。

将 bbox 存储为连续 array，避免 Python tuple overhead。

用法：
    store = GeometryStore()

    idx = store.add((10, 20, 100, 30))
    bbox = store.get(idx)

    # 批量操作
    bboxes = store.get_batch([0, 1, 2])
"""

from __future__ import annotations

import array
from typing import List, Optional, Tuple


class GeometryStore:
    """紧凑几何存储。

    内部：
        coords: array('f') — [x0, y0, x1, y1, x0, y0, x1, y1, ...]

    每个 bbox = 4 floats = 16 bytes
    Python tuple = 4 objects + tuple overhead ≈ 120 bytes

    节省：~85% 内存
    """

    def __init__(self, capacity: int = 0) -> None:
        self._coords = array.array("f")
        if capacity > 0:
            self._coords.extend([0.0] * (capacity * 4))

    def add(self, bbox: Tuple[float, float, float, float]) -> int:
        """添加 bbox，返回 index。"""
        idx = len(self._coords) // 4
        self._coords.extend(bbox)
        return idx

    def get(self, idx: int) -> Tuple[float, float, float, float]:
        """获取 bbox。"""
        start = idx * 4
        if start + 4 > len(self._coords):
            raise IndexError(f"Index {idx} out of range")
        return (
            self._coords[start],
            self._coords[start + 1],
            self._coords[start + 2],
            self._coords[start + 3],
        )

    def get_batch(self, indices: List[int]) -> List[Tuple[float, float, float, float]]:
        """批量获取 bbox。"""
        return [self.get(i) for i in indices]

    def set(self, idx: int, bbox: Tuple[float, float, float, float]) -> None:
        """设置 bbox。"""
        start = idx * 4
        if start + 4 > len(self._coords):
            raise IndexError(f"Index {idx} out of range")
        self._coords[start : start + 4] = array.array("f", bbox)

    def __len__(self) -> int:
        return len(self._coords) // 4

    def __getitem__(self, idx: int) -> Tuple[float, float, float, float]:
        return self.get(idx)

    @property
    def memory_estimate(self) -> int:
        return len(self._coords) * 4

    def summary(self) -> str:
        return (
            f"GeometryStore | bboxes={len(self)} | "
            f"memory={self.memory_estimate / 1024:.1f}KB"
        )
