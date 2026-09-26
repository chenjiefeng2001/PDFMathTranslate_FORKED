"""PikepdfIncrementalAssembler — 对象级复用实验（第一版）。

重要：这不是"高性能实现"。

它的存在意义：
    验证 object reuse 是否成立：
    - 不做 insert_file（不复制所有对象）
    - 不做 move_page（直接构建 page tree）
    - 直接修改原 PDF 的 page 对象的 /Contents 指针

如果验证成功，性能提升来自：
    "减少 object copy"
    而不是：
    "pikepdf 比 MuPDF 快"

这是两个完全不同的概念。

第一版范围（极小）：
    ✓ 克隆原 PDF
    ✓ 替换被翻译页面的 /Contents
    ✓ 添加字体资源到 /Resources（简化实现）
    ✓ 写出

    ✗ 不处理：annotations / outline / metadata / form / encryption
    ✗ 不处理：字体子集化（回退到 MuPDF）
    ✗ 不处理：交叉引用流特殊格式

依赖：
    pikepdf（已声明为项目依赖）
"""

from __future__ import annotations

import io
import logging
from typing import List, Optional

from pdf2zh.assembler.base import (
    BackendCapabilities,
    FontRequirement,
    PDFAssembler,
    PageResult,
    TranslationArtifact,
)

logger = logging.getLogger(__name__)

try:
    import pikepdf

    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False


def _page_obj(page):
    """pikepdf>=9 的 pdf.pages[i] 返回 ObjectHelper 包装，统一取底层对象。"""
    return getattr(page, "obj", page)


class PikepdfIncrementalAssembler(PDFAssembler):
    """基于 pikepdf 的 PDF 组装器（对象级复用实验）。

    与 MuPDFAssembler 的关键区别：
    - 不做 insert_file（不复制所有对象）
    - 不做 move_page（直接构建 page tree）
    - 直接修改原 PDF 的 page 对象的 /Contents 指针

    第一版限制：
    - 仅支持文本型 PDF（无复杂 annotation / form）
    - 字体子集化回退到 MuPDF
    - 加密 PDF 需要先解密
    """

    @property
    def name(self) -> str:
        return "pikepdf-incremental"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            object_reuse=True,
            incremental_write=True,
            font_subsetting=False,  # 第一版不做字体子集化（回退 MuPDF）
            annotation_preserve=False,  # 第一版不复制 /Annots
            preserves=("fonts",),  # 第一版只保留字体
            supports=("stream_rewrite", "page_tree_edit"),
            # 第一版不支持
            # preserves: annotations, links, outlines, metadata, forms
        )

    def assemble(self, artifact: TranslationArtifact) -> bytes:
        if not HAS_PIKEPDF:
            raise ImportError(
                "pikepdf is required for PikepdfIncrementalAssembler. "
                "Install with: pip install pikepdf"
            )
        if not artifact.original_pdf:
            raise ValueError("TranslationArtifact.original_pdf is required")

        if artifact.mode == "mono":
            return self._assemble_mono(artifact)
        elif artifact.mode == "dual":
            return self._assemble_dual(artifact)
        else:
            raise ValueError(f"Unsupported mode: {artifact.mode}")

    def _assemble_mono(self, artifact: TranslationArtifact) -> bytes:
        """mono: 原 PDF 不做修改，直接重新序列化。"""
        pdf = pikepdf.open(io.BytesIO(artifact.original_pdf))
        try:
            out = io.BytesIO()
            pdf.save(
                out,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
            return out.getvalue()
        finally:
            pdf.close()

    def _assemble_dual(self, artifact: TranslationArtifact) -> bytes:
        """dual: 对象级复用，O(修改页数) 而非 O(全对象)。

        算法：
        1. 打开原 PDF
        2. 对每个被翻译页面：替换 /Contents
        3. 添加新字体到 /Resources/Font
        4. 构建交错 page tree [原0, 译0, 原1, 译1, ...]
        5. 保存
        """
        sorted_results = sorted(artifact.pages, key=lambda r: r.page_index)
        if not sorted_results:
            return artifact.original_pdf

        pdf = pikepdf.open(io.BytesIO(artifact.original_pdf))
        try:
            original_pages = [_page_obj(p) for p in pdf.pages]
            original_count = len(original_pages)

            # Phase 1: 替换被翻译页面的 content stream
            translated_map: dict[int, PageResult] = {
                r.page_index: r for r in sorted_results
            }

            for page_idx, result in translated_map.items():
                if result.content_stream and page_idx < original_count:
                    self._replace_page_content(pdf, page_idx, result)

            # Phase 2: 添加新字体（简化实现）
            self._embed_fonts(pdf, sorted_results)

            # Phase 3: 构建交错 page tree
            new_kids = pikepdf.Array()
            for i in range(original_count):
                # 原页
                new_kids.append(original_pages[i])
                # 译页
                if i in translated_map:
                    translated_page = self._create_translated_page(
                        pdf, original_pages[i], translated_map[i]
                    )
                    new_kids.append(translated_page)

            # 替换 page tree
            pages_obj = pdf.Root.get("/Pages")
            if pages_obj is not None:
                pages_obj["/Kids"] = new_kids
                pages_obj["/Count"] = len(new_kids)

            # Phase 4: 保存
            out = io.BytesIO()
            pdf.save(
                out,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                recompress_flate=True,
            )
            return out.getvalue()
        finally:
            pdf.close()

    def _replace_page_content(
        self, pdf: pikepdf.Pdf, page_idx: int, result: PageResult
    ):
        """替换指定页面的 /Contents。"""
        if page_idx >= len(pdf.pages):
            return

        page = _page_obj(pdf.pages[page_idx])

        # 创建新的 content stream
        new_stream = pikepdf.Stream(pdf, result.content_stream)
        new_ref = pdf.make_indirect(new_stream)

        # 替换 /Contents
        page["/Contents"] = new_ref

    def _create_translated_page(
        self, pdf: pikepdf.Pdf, original_page: pikepdf.Dictionary, result: PageResult
    ) -> pikepdf.Dictionary:
        """创建译文页面：基于原页结构，替换 /Contents。"""
        translated = pikepdf.Dictionary()

        # 复制不变的属性
        for key in [
            "/MediaBox",
            "/CropBox",
            "/BleedBox",
            "/TrimBox",
            "/ArtBox",
            "/Resources",
            "/Rotate",
            "/Group",
        ]:
            if key in original_page:
                translated[key] = original_page[key]

        # 替换 /Contents
        if result.content_stream:
            new_stream = pikepdf.Stream(pdf, result.content_stream)
            translated["/Contents"] = pdf.make_indirect(new_stream)

        # 注意：第一版不复制 /Annots（避免复杂 annotation 处理）

        return translated

    def _embed_fonts(self, pdf: pikepdf.Pdf, results: List[PageResult]):
        """将 FontRequirement 中的字体嵌入 PDF（简化实现）。

        第一版：记录警告，依赖 MuPDF fallback。
        完整实现需要处理 TrueType/CFF/Type1 字体格式。
        """
        all_fonts: dict[str, FontRequirement] = {}
        for result in results:
            for fr in result.resources.fonts:
                if fr.name and fr.name not in all_fonts and not fr.is_builtin:
                    all_fonts[fr.name] = fr

        if not all_fonts:
            return

        pages_obj = pdf.Root.get("/Pages")
        if pages_obj is None:
            return

        resources = pages_obj.get("/Resources")
        if resources is None:
            resources = pikepdf.Dictionary()
            pages_obj["/Resources"] = resources

        font_dict = resources.get("/Font")
        if font_dict is None:
            font_dict = pikepdf.Dictionary()
            resources["/Font"] = font_dict

        for fname, fr in all_fonts.items():
            if pikepdf.Name(f"/{fname}") in font_dict:
                continue

            # TODO: 完整字体嵌入
            logger.debug(
                "Font embedding for %s deferred to MuPDF fallback",
                fname,
            )
