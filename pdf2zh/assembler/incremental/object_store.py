"""ObjectStore — 跟踪 PDF 对象状态。

Phase 3.6a 核心：记录哪些对象被修改，哪些保持原样。

PDF incremental update 原理：
    1. 读取原始 PDF（保留所有 xref 信息）
    2. 只修改需要改的对象
    3. 在文件末尾追加：新对象 + 新 xref + startxref 指针
    4. Reader 从后向前读取，最新 xref 优先

所以：
    未修改对象 → 零开销（不触碰）
    修改对象 → append 新版本
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

import pikepdf


class ObjectState(str, Enum):
    """对象状态。"""

    UNMODIFIED = "unmodified"  # 原样保留
    MODIFIED = "modified"  # 内容已变，需要 append
    NEW = "new"  # 新增对象


@dataclass
class ObjectEntry:
    """单个对象的跟踪信息。"""

    objgen: tuple  # (obj_number, generation)
    state: ObjectState
    original_offset: int = 0  # 原始文件中的字节偏移
    new_object: Optional[pikepdf.Object] = None  # 新对象（MODIFIED/NEW）


class ObjectStore:
    """跟踪所有 PDF 对象的修改状态。

    用途：
        1. open(source_pdf) 时记录所有原始对象
        2. 修改页面时标记对应对象为 MODIFIED
        3. commit() 时只写入 MODIFIED/NEW 对象

    用法：
        store = ObjectStore()
        store.open(source_pdf)

        # 修改 page 0
        store.mark_modified(page_0_objgen)

        # 写入
        writer = IncrementalWriter(store)
        writer.commit(modified_pages, output_pdf)
    """

    def __init__(self) -> None:
        self._entries: Dict[tuple, ObjectEntry] = {}
        self._next_objnum: int = 1

    def open(self, pdf: pikepdf.Pdf) -> None:
        """打开已有 PDF，记录所有原始对象。"""
        self._entries.clear()

        # 记录所有间接对象
        for objnum in range(1, len(pdf.objects)):
            try:
                obj = pdf.objects.get(objnum)
                gen = 0  # pikepdf 不暴露 generation，通常为 0
                objgen = (objnum, gen)

                # 尝试获取原始 offset
                offset = 0
                try:
                    if hasattr(obj, "objgen"):
                        objgen = obj.objgen
                except Exception:
                    pass

                self._entries[objgen] = ObjectEntry(
                    objgen=objgen,
                    state=ObjectState.UNMODIFIED,
                    original_offset=offset,
                )
                self._next_objnum = max(self._next_objnum, objnum + 1)
            except Exception:
                pass

    def mark_modified(self, objgen: tuple) -> None:
        """标记对象为已修改。"""
        if objgen in self._entries:
            self._entries[objgen].state = ObjectState.MODIFIED

    def add_new(self, obj: pikepdf.Object) -> tuple:
        """添加新对象，返回 objgen。"""
        objgen = (self._next_objnum, 0)
        self._next_objnum += 1
        self._entries[objgen] = ObjectEntry(
            objgen=objgen,
            state=ObjectState.NEW,
            new_object=obj,
        )
        return objgen

    def get_entry(self, objgen: tuple) -> Optional[ObjectEntry]:
        return self._entries.get(objgen)

    @property
    def total_objects(self) -> int:
        return len(self._entries)

    @property
    def modified_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.state == ObjectState.MODIFIED)

    @property
    def new_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.state == ObjectState.NEW)

    @property
    def unmodified_count(self) -> int:
        return sum(
            1 for e in self._entries.values() if e.state == ObjectState.UNMODIFIED
        )

    @property
    def written_ratio(self) -> float:
        """被写入对象的比例（modified + new）/ total。"""
        if self.total_objects == 0:
            return 0.0
        return (self.modified_count + self.new_count) / self.total_objects

    def summary(self) -> str:
        return (
            f"ObjectStore | total={self.total_objects} | "
            f"unmodified={self.unmodified_count} | "
            f"modified={self.modified_count} | "
            f"new={self.new_count} | "
            f"written_ratio={self.written_ratio:.1%}"
        )
