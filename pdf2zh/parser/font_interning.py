"""FontInterning — 字体名去重。

730 页 PDF 可能有 500000+ spans，但字体名可能只有 10-50 个。
Font Interning 将重复字符串存储一次，用 integer ID 引用。

用法：
    interning = FontInterning()

    font_id = interning.intern("Times-Roman")  # 0
    font_id = interning.intern("Arial")        # 1
    font_id = interning.intern("Times-Roman")  # 0 (复用)

    name = interning.resolve(0)  # "Times-Roman"
"""

from __future__ import annotations

from typing import Dict, List, Optional


class FontInterning:
    """字体名去重池。

    内部：
        name → id 映射
        id → name 列表

    外部：
        只暴露 integer ID
    """

    def __init__(self) -> None:
        self._name_to_id: Dict[str, int] = {}
        self._id_to_name: List[str] = []

    def intern(self, name: str) -> int:
        """注册字体名，返回 ID。"""
        if name in self._name_to_id:
            return self._name_to_id[name]

        font_id = len(self._id_to_name)
        self._name_to_id[name] = font_id
        self._id_to_name.append(name)
        return font_id

    def resolve(self, font_id: int) -> str:
        """ID → 字体名。"""
        if 0 <= font_id < len(self._id_to_name):
            return self._id_to_name[font_id]
        return ""

    @property
    def count(self) -> int:
        return len(self._id_to_name)

    @property
    def total_refs(self) -> int:
        return len(self._name_to_id)

    def summary(self) -> str:
        return f"FontInterning | fonts={self.count}"
