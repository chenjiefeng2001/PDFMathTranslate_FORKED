"""TextSpanStore — columnar 存储。

将 TextSpan 列表存储为 column arrays，避免 Python object overhead。

用法：
    store = TextSpanStore()

    # 添加
    store.add("hello", (10, 20, 100, 30), font_id=0, flags=0)
    store.add("world", (20, 40, 200, 50), font_id=1, flags=0)

    # 访问
    span = store[0]
    # TextSpanView(text="hello", bbox=(10, 20, 100, 30))

    # 迭代
    for span in store:
        print(span.text)
"""

from __future__ import annotations

import array
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple


@dataclass
class TextSpanView:
    """TextSpan 的轻量级视图。"""

    text: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    font_id: int = 0
    font_size: float = 0.0
    flags: int = 0
    color: int = 0

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 2**4)

    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2**1)

    @property
    def font_name(self) -> str:
        return ""  # 需要 FontInterning resolve


class TextSpanStore:
    """Columnar 存储。

    内部：
        texts: List[str]
        bboxes: array('f') — 4 floats per span
        font_ids: array('i')
        font_sizes: array('f')
        flags: array('i')
        colors: array('i')

    外部：
        store[i] → TextSpanView
        for span in store → 迭代
    """

    def __init__(self, capacity: int = 0) -> None:
        self._texts: List[str] = []
        self._bboxes = array.array("f")
        self._font_ids = array.array("i")
        self._font_sizes = array.array("f")
        self._flags = array.array("i")
        self._colors = array.array("i")

        if capacity > 0:
            self._bboxes.extend([0.0] * (capacity * 4))
            self._font_ids.extend([0] * capacity)
            self._font_sizes.extend([0.0] * capacity)
            self._flags.extend([0] * capacity)
            self._colors.extend([0] * capacity)

    def add(
        self,
        text: str,
        bbox: Tuple[float, float, float, float],
        font_id: int = 0,
        font_size: float = 0.0,
        flags: int = 0,
        color: int = 0,
    ) -> int:
        """添加 text span，返回 index。"""
        idx = len(self._texts)
        self._texts.append(text)
        self._bboxes.extend(bbox)
        self._font_ids.append(font_id)
        self._font_sizes.append(font_size)
        self._flags.append(flags)
        self._colors.append(color)
        return idx

    def __getitem__(self, idx: int) -> TextSpanView:
        if idx < 0 or idx >= len(self._texts):
            raise IndexError(f"Index {idx} out of range")

        bbox_start = idx * 4
        return TextSpanView(
            text=self._texts[idx],
            bbox=(
                self._bboxes[bbox_start],
                self._bboxes[bbox_start + 1],
                self._bboxes[bbox_start + 2],
                self._bboxes[bbox_start + 3],
            ),
            font_id=self._font_ids[idx],
            font_size=self._font_sizes[idx],
            flags=self._flags[idx],
            color=self._colors[idx],
        )

    def __len__(self) -> int:
        return len(self._texts)

    def __iter__(self) -> Iterator[TextSpanView]:
        for i in range(len(self)):
            yield self[i]

    @property
    def memory_estimate(self) -> int:
        """估算内存使用（字节）。"""
        texts_mem = sum(len(t) for t in self._texts)
        bboxes_mem = len(self._bboxes) * 4
        font_ids_mem = len(self._font_ids) * 4
        font_sizes_mem = len(self._font_sizes) * 4
        flags_mem = len(self._flags) * 4
        colors_mem = len(self._colors) * 4
        return (
            texts_mem
            + bboxes_mem
            + font_ids_mem
            + font_sizes_mem
            + flags_mem
            + colors_mem
        )

    def summary(self) -> str:
        return (
            f"TextSpanStore | spans={len(self)} | "
            f"memory={self.memory_estimate / 1024:.1f}KB"
        )
