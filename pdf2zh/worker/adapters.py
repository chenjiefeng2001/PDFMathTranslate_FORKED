"""ObjPatchAdapter / PageResultAdapter — 两种输出格式。

Phase 1.1 核心：
    一次生成 PageProcessOutput，两种输出格式。

    ObjPatchAdapter: 输出 legacy obj_patch dict
    PageResultAdapter: 输出 PageResult (跨进程协议)

两个 adapter 不互相调用，不形成隐藏依赖。
"""

from __future__ import annotations

from typing import Dict, Optional

from pdf2zh.assembler.base import (
    FontIdentity,
    FontRequirement,
    PageMetrics,
    PageResult,
    PageSize,
    ResourceRequirement,
    SCHEMA_VERSION,
)
from pdf2zh.worker.page_output import PageProcessOutput


class ObjPatchAdapter:
    """将 PageProcessOutput 转换为 legacy obj_patch dict。

    obj_patch 格式：{xref: content_stream_bytes}
    这是现有 high_level.py merge 阶段需要的格式。

    注意：
        这个 adapter 不修复旧结构，只做格式转换。
        如果旧代码需要 {"stream": ..., "font": ...}，那就保持。
    """

    @staticmethod
    def convert(
        output: PageProcessOutput,
        page_xref_map: Optional[Dict[int, int]] = None,
    ) -> Dict[int, bytes]:
        """转换为 legacy obj_patch dict。

        Args:
            output: worker 内部产物
            page_xref_map: {page_number: xref} 映射（来自 task）

        Returns:
            {xref: content_stream_bytes} dict
        """
        if not output.content_stream:
            return {}

        # 从 page_xref_map 获取该页面的主 xref
        if page_xref_map and output.page_index in page_xref_map:
            main_xref = page_xref_map[output.page_index]
            return {main_xref: output.content_stream}

        # 如果没有 page_xref_map，返回空 dict
        # (Phase 1.1: 由调用方处理 xref 映射)
        return {}

    @staticmethod
    def extract_content_stream(output: PageProcessOutput) -> bytes:
        """提取 content stream（用于直接注入）。"""
        return output.content_stream


class PageResultAdapter:
    """将 PageProcessOutput 转换为 PageResult (跨进程协议)。

    这里做：
    - schema_version 设置
    - validate 调用
    - 序列化兼容性保证
    """

    @staticmethod
    def convert(output: PageProcessOutput) -> PageResult:
        """转换为 PageResult。

        Args:
            output: worker 内部产物

        Returns:
            PageResult (pickle-safe, 跨进程)
        """
        result = PageResult(
            schema_version=SCHEMA_VERSION,
            page_index=output.page_index,
            source_page_hash=output.source_page_hash,
            page_size=output.page_size,
            content_stream=output.content_stream,
            resources=output.resources,
            annotations=output.annotations,
            links=output.links,
            metrics=output.metrics,
            side_channel=output.side_channel,
        )
        # 验证
        result.validate_schema()
        return result
