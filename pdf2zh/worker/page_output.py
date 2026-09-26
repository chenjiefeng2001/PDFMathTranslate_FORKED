"""PageProcessOutput — worker 内部私有 ABI。

这是 Phase 1.1 的核心数据结构。
它不是公共协议（assembler 不知道它）。
它是 worker 内部的真实产物，两个 adapter 负责转换。

注意：
    不要放 fitz.Page / fitz.Document / xref。
    如果需要加入这些字段，说明还有隐藏耦合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from pdf2zh.assembler.base import (
    FontRequirement,
    PageMetrics,
    PageSize,
    ResourceRequirement,
    SCHEMA_VERSION,
)


@dataclass
class PageProcessOutput:
    """页面处理结果：worker 内部 ABI。

    process_page(task) -> PageProcessOutput
    ObjPatchAdapter.convert(output) -> obj_patch
    PageResultAdapter.convert(output) -> PageResult

    不要放：
    - fitz.Page / fitz.Document
    - xref
    - PyMuPDF 对象引用
    """

    page_index: int = -1
    source_page_hash: str = ""
    content_stream: bytes = b""
    resources: ResourceRequirement = field(default_factory=ResourceRequirement)
    annotations: List[dict] = field(default_factory=list)
    links: List[dict] = field(default_factory=list)
    page_size: PageSize = field(default_factory=PageSize)
    metrics: PageMetrics = field(default_factory=PageMetrics)
    side_channel: Optional[dict] = None

    def validate(self) -> None:
        """验证数据完整性（worker exit 前调用）。"""
        assert self.page_index >= 0, f"page_index must be >= 0, got {self.page_index}"
        assert isinstance(
            self.content_stream, bytes
        ), f"content_stream must be bytes, got {type(self.content_stream)}"
        assert self.page_size.width > 0, f"page_width must be > 0"
        assert self.page_size.height > 0, f"page_height must be > 0"
