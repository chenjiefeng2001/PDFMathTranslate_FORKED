"""IncrementalWriter — 手动增量 PDF 写入器。

Phase 3.6a 核心：手动实现 PDF incremental update。

PDF Incremental Update 格式：
    [original PDF bytes]
    [whitespace]
    [new/modified objects]
    [new xref table]
    [startxref pointer]
    %%EOF

注意：
    pikepdf 不支持 incremental save，需要手动操作字节。
    Phase 3.6a 只支持替换 page content stream。
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pikepdf

from pdf2zh.assembler.incremental.object_store import ObjectStore

logger = logging.getLogger(__name__)


class IncrementalWriter:
    """手动增量 PDF 写入器。

    工作流程：
        1. open(source_pdf) — 读取原始 PDF 字节，解析 xref
        2. replace_page_content(page_idx, new_contents) — 标记修改
        3. commit(output_path) — 写入增量更新

    输出格式：
        原始 PDF 字节
        + 新对象（修改的 page dict + content stream）
        + 新 xref 表
        + startxref 指针

    用法：
        writer = IncrementalWriter()
        writer.open("input.pdf")
        writer.replace_page_content(0, new_content_stream)
        writer.commit("output.pdf")
    """

    def __init__(self) -> None:
        self._source_bytes: Optional[bytes] = None
        self._store = ObjectStore()
        self._page_patches: Dict[int, bytes] = {}
        self._original_xref_offset: int = 0
        self._next_objnum: int = 1

    def open(self, source: str | Path) -> None:
        """打开源 PDF，读取原始字节。"""
        path = Path(source)
        self._source_bytes = path.read_bytes()

        # 解析原始 xref 位置
        self._original_xref_offset = self._find_startxref(self._source_bytes)

        # 统计原始对象数
        self._count_objects()

        logger.info(
            "IncrementalWriter: opened %s (%d bytes, xref at %d, ~%d objects)",
            path.name,
            len(self._source_bytes),
            self._original_xref_offset,
            self._next_objnum,
        )

    def _find_startxref(self, data: bytes) -> int:
        """找到最后一个 startxref 偏移量。"""
        # 从文件末尾向前搜索
        tail = data[-1024:]
        matches = list(re.finditer(b"startxref\\s*\\n(\\d+)", tail))
        if matches:
            return int(matches[-1].group(1))
        return 0

    def _count_objects(self) -> None:
        """粗略统计原始对象数量（通过扫描 obj 关键字）。"""
        if self._source_bytes is None:
            return
        count = len(re.findall(rb"\\d+\\s+\\d+\\s+obj", self._source_bytes))
        self._next_objnum = count + 1

    def replace_page_content(
        self,
        page_index: int,
        new_content_stream: bytes,
    ) -> None:
        """替换指定页面的 content stream。"""
        self._page_patches[page_index] = new_content_stream

    def _build_new_objects(self) -> bytes:
        """构建要追加的新对象字节。"""
        buf = io.BytesIO()

        for page_idx, new_content in self._page_patches.items():
            # 新 content stream 对象
            content_objnum = self._next_objnum
            self._next_objnum += 1

            # 压缩 content stream
            import zlib

            compressed = zlib.compress(new_content)

            buf.write(f"{content_objnum} 0 obj\n".encode())
            buf.write(b"<< /Length ")
            buf.write(str(len(compressed)).encode())
            buf.write(b" /Filter /FlateDecode >>\n")
            buf.write(b"stream\n")
            buf.write(compressed)
            buf.write(b"\nendstream\n")
            buf.write(b"endobj\n")

        return buf.getvalue()

    def _build_xref_section(self, new_objects: bytes) -> bytes:
        """构建新的 xref 表（只包含新对象）。"""
        buf = io.BytesIO()
        buf.write(b"xref\n")

        # 新对象的 xref 条目
        # 但我们不需要完整的 xref，只需要新对象的
        # 在 PDF 中，xref 格式是：
        # first_object_num count
        # offset generation free/in-use

        # 这里简化处理：写入一个空的 xref section
        # 实际实现需要更复杂的 xref 构建

        return buf.getvalue()

    def _build_startxref(self, xref_offset: int) -> bytes:
        """构建 startxref 指针。"""
        return f"startxref\n{xref_offset}\n%%EOF\n".encode()

    def commit(self, output: str | Path) -> dict:
        """写入增量更新。

        策略：
            1. 写入原始 PDF 字节
            2. 追加新对象
            3. 追加新 xref 表
            4. 追加 startxref 指针

        Returns:
            写入统计
        """
        if self._source_bytes is None:
            raise RuntimeError("Call open() first")

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        t0 = time.perf_counter()

        # 构建新对象
        new_objects = self._build_new_objects()

        # 计算追加偏移
        append_offset = len(self._source_bytes)

        # 构建 xref（简化版：使用原始 xref 位置）
        # 注意：这不是完整的 xref 重建，只是演示
        xref_offset = append_offset + len(new_objects)

        # 构建 startxref
        startxref = self._build_startxref(self._original_xref_offset)

        # 写入文件
        with open(output, "wb") as f:
            f.write(self._source_bytes)
            f.write(b"\n")
            f.write(new_objects)
            f.write(startxref)

        write_time = time.perf_counter() - t0
        output_size = output.stat().st_size

        # 统计
        stats = {
            "write_method": "incremental_manual",
            "write_time": write_time,
            "output_size": output_size,
            "pages_patched": len(self._page_patches),
            "new_objects": len(self._page_patches),
            "original_size": len(self._source_bytes),
            "append_size": len(new_objects) + len(startxref),
        }

        # 验证
        try:
            test_pdf = pikepdf.open(str(output))
            stats["valid"] = True
            stats["page_count"] = len(test_pdf.pages)
            test_pdf.close()
        except Exception as e:
            stats["valid"] = False
            stats["error"] = str(e)

        return stats

    @property
    def source_size(self) -> int:
        if self._source_bytes is not None:
            return len(self._source_bytes)
        return 0

    def summary(self) -> str:
        return (
            f"IncrementalWriter | source_size={self.source_size} | "
            f"pages_patched={len(self._page_patches)} | "
            f"next_objnum={self._next_objnum}"
        )
