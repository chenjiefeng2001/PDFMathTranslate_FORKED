"""ObjectStore — PDF object identity management.

Phase 4.1 核心：基于 identity 的对象查找和复用。

PDF 对象有两类 identity：
    1. xref identity — (obj_number, generation) — 位置标识
    2. content identity — 对象内容的 hash — 语义标识

翻译场景下，我们需要的是 content identity：
    - 同一字体在不同 PDF 中，content identity 相同
    - 同一图片被多次引用，content identity 相同
    - 未修改的对象，content identity 不变

用法：
    store = ObjectStore()

    # 注册原始对象
    store.register(pdf, page_obj, identity="page_0")

    # 查找可复用对象
    existing = store.find_by_content(font_bytes)
    if existing:
        reuse(existing)
    else:
        store.register(pdf, new_font, identity="font_noto")
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Set

import pikepdf


class ObjectType(str, Enum):
    """PDF 对象类型。"""

    PAGE = "page"
    FONT = "font"
    IMAGE = "image"
    XOBJECT = "xobject"
    CONTENT_STREAM = "content_stream"
    RESOURCE_DICT = "resource_dict"
    ANNOTATION = "annotation"
    OTHER = "other"


@dataclass
class ObjectIdentity:
    """对象的语义 identity。

    content_hash: 对象内容的 hash（用于查找可复用对象）
    object_type: 对象类型
    label: 可读标签（用于调试）
    """

    content_hash: str = ""
    object_type: ObjectType = ObjectType.OTHER
    label: str = ""

    def __hash__(self) -> int:
        return hash(self.content_hash)

    def __eq__(self, other) -> bool:
        if not isinstance(other, ObjectIdentity):
            return False
        return self.content_hash == other.content_hash


@dataclass
class ObjectEntry:
    """ObjectStore 中的条目。"""

    identity: ObjectIdentity
    objgen: tuple  # (obj_number, generation)
    is_modified: bool = False
    is_new: bool = False
    reference_count: int = 0


class ObjectStore:
    """基于 content identity 的对象存储。

    核心机制：
        1. register() — 注册对象，计算 content hash
        2. find_by_content() — 按 content hash 查找可复用对象
        3. mark_modified() — 标记对象为已修改

    用途：
        - 字体去重：相同 content hash 的字体共享
        - 图片去重：相同 content hash 的图片共享
        - 增量更新：只输出 modified/new 对象

    用法：
        store = ObjectStore()

        # 字体去重
        for font in fonts:
            identity = store.compute_identity(font, ObjectType.FONT)
            existing = store.find_by_content(identity.content_hash)
            if existing:
                reuse(existing)
            else:
                store.register(pdf, font, identity)
    """

    def __init__(self) -> None:
        # content_hash → ObjectEntry
        self._by_content: Dict[str, ObjectEntry] = {}
        # objgen → ObjectEntry
        self._by_objgen: Dict[tuple, ObjectEntry] = {}
        self._next_objnum: int = 1

    def compute_identity(
        self,
        obj: pikepdf.Object,
        obj_type: ObjectType = ObjectType.OTHER,
        label: str = "",
    ) -> ObjectIdentity:
        """计算对象的 content identity。

        对于 Stream 对象，hash 内容字节。
        对于 Dictionary 对象，hash 序列化字节。
        """
        content_bytes = self._extract_content_bytes(obj)
        content_hash = hashlib.sha256(content_bytes).hexdigest()[:16]

        return ObjectIdentity(
            content_hash=content_hash,
            object_type=obj_type,
            label=label,
        )

    def _extract_content_bytes(self, obj: pikepdf.Object) -> bytes:
        """提取对象的内容字节（用于 hash）。"""
        if isinstance(obj, pikepdf.Stream):
            try:
                return obj.read_bytes()
            except Exception:
                return b""
        elif isinstance(obj, pikepdf.Dictionary):
            parts = []
            for key in sorted(obj.keys()):
                val = obj[key]
                parts.append(f"{key}=".encode())
                parts.append(str(val).encode())
            return b"".join(parts)
        elif isinstance(obj, pikepdf.Array):
            return b"".join(str(v).encode() for v in obj)
        else:
            return str(obj).encode()

    def register(
        self,
        obj: pikepdf.Object,
        identity: Optional[ObjectIdentity] = None,
        objgen: Optional[tuple] = None,
    ) -> ObjectEntry:
        """注册对象到 store。

        如果已有相同 content_hash 的对象，返回已有的（去重）。
        """
        if identity is None:
            identity = self.compute_identity(obj)

        # 检查是否已有相同 content
        if identity.content_hash in self._by_content:
            existing = self._by_content[identity.content_hash]
            existing.reference_count += 1
            return existing

        # 新对象
        if objgen is None:
            objgen = (self._next_objnum, 0)
            self._next_objnum += 1

        entry = ObjectEntry(
            identity=identity,
            objgen=objgen,
            is_new=True,
            reference_count=1,
        )

        self._by_content[identity.content_hash] = entry
        self._by_objgen[objgen] = entry

        return entry

    def find_by_content(self, content_hash: str) -> Optional[ObjectEntry]:
        """按 content hash 查找可复用对象。"""
        return self._by_content.get(content_hash)

    def find_by_objgen(self, objgen: tuple) -> Optional[ObjectEntry]:
        """按 objgen 查找对象。"""
        return self._by_objgen.get(objgen)

    def mark_modified(self, objgen: tuple) -> None:
        """标记对象为已修改。"""
        entry = self._by_objgen.get(objgen)
        if entry:
            entry.is_modified = True

    @property
    def total_objects(self) -> int:
        return len(self._by_content)

    @property
    def modified_count(self) -> int:
        return sum(1 for e in self._by_content.values() if e.is_modified)

    @property
    def new_count(self) -> int:
        return sum(1 for e in self._by_content.values() if e.is_new)

    @property
    def shared_count(self) -> int:
        """被多次引用的对象数。"""
        return sum(1 for e in self._by_content.values() if e.reference_count > 1)

    @property
    def dedup_ratio(self) -> float:
        """去重率 = shared / total。"""
        if self.total_objects == 0:
            return 0.0
        return self.shared_count / self.total_objects

    def summary(self) -> str:
        return (
            f"ObjectStore | total={self.total_objects} | "
            f"modified={self.modified_count} | new={self.new_count} | "
            f"shared={self.shared_count} | dedup={self.dedup_ratio:.1%}"
        )

    def type_summary(self) -> str:
        """按类型统计。"""
        by_type: dict[ObjectType, int] = {}
        for entry in self._by_content.values():
            t = entry.identity.object_type
            by_type[t] = by_type.get(t, 0) + 1

        parts = [f"{t.value}={c}" for t, c in sorted(by_type.items())]
        return " | ".join(parts) if parts else "empty"
