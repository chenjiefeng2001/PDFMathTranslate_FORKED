"""Incremental PDF writing — 增量写入模块。

Phase 3.6a:
    - ObjectStore: 跟踪对象修改状态
    - IncrementalWriter: 只追加修改对象

数据流：
    source_pdf
        ↓
    IncrementalWriter.open()
        ├── ObjectStore (记录所有原始对象)
        └── source_bytes (保留原始字节)
        ↓
    replace_page_content()
        ├── ObjectStore.mark_modified()
        └── page_patches[page_idx] = new_stream
        ↓
    commit()
        ├── pikepdf.save(incremental=True)
        └── 输出：原始 + 追加修改
"""

from pdf2zh.assembler.incremental.object_store import (
    ObjectEntry,
    ObjectState,
    ObjectStore,
)
from pdf2zh.assembler.incremental.writer import IncrementalWriter

__all__ = [
    "IncrementalWriter",
    "ObjectStore",
    "ObjectEntry",
    "ObjectState",
]
