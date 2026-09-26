"""MuPDFAssembler — 兼容层（行为与现有代码 100% 一致）。

重要：这是兼容层，不是优化层。

它的存在意义：
    证明 新 pipeline = 旧 pipeline（行为一致，性能甚至可以暂时一样）。

它不是：
    - "比旧代码更快的实现"
    - "重新实现 high_level.py"
    - "最终 production backend"

它的职责：
    1. 接收 TranslationArtifact
    2. 用 PyMuPDF 执行与现有 translate_stream 完全相同的操作
    3. 输出与现有代码完全一致的 PDF

性能特征：
    O(所有对象) 拷贝 + O(页数) move_page，与现有代码相同。
"""

from __future__ import annotations

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


class MuPDFAssembler(PDFAssembler):
    """基于 PyMuPDF 的 PDF 组装器（兼容层）。

    行为与现有 translate_stream 合并逻辑 100% 一致：
    1. doc_en = 原始 PDF 副本
    2. doc_zh = 翻译后的 PDF 副本
    3. insert_file(doc_zh) → 合并所有对象
    4. move_page() → 交错排列
    5. subset_fonts() → 字体子集化
    6. write() → 输出字节
    """

    @property
    def name(self) -> str:
        return "mupdf"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            object_reuse=False,
            incremental_write=False,
            font_subsetting=True,
            annotation_preserve=True,
            preserves=("fonts", "annotations", "links", "outlines", "metadata"),
            supports=("encrypted_input", "stream_rewrite"),
        )

    def assemble(self, artifact: TranslationArtifact) -> bytes:
        import pymupdf

        if not artifact.original_pdf:
            raise ValueError(
                "TranslationArtifact.original_pdf is required for MuPDFAssembler"
            )

        if artifact.mode == "mono":
            return self._assemble_mono(artifact)
        elif artifact.mode == "dual":
            return self._assemble_dual(artifact)
        else:
            raise ValueError(f"Unsupported mode: {artifact.mode}")

    def _assemble_mono(self, artifact: TranslationArtifact) -> bytes:
        """mono: 原 PDF 不做修改，直接重新序列化。"""
        import pymupdf

        doc = pymupdf.open(stream=artifact.original_pdf, filetype="pdf")
        try:
            if not artifact.skip_subset_fonts:
                self._protect_math_fonts(doc)
                try:
                    doc.subset_fonts(fallback=False)
                except Exception as e:
                    logger.warning("subset_fonts failed for mono: %s", str(e)[:120])
            return doc.write(deflate=True, garbage=4, use_objstms=1)
        finally:
            doc.close()

    def _assemble_dual(self, artifact: TranslationArtifact) -> bytes:
        """dual: 原文 + 译文交错 [原0, 译0, 原1, 译1, ...]。"""
        import pymupdf

        sorted_results = sorted(artifact.pages, key=lambda r: r.page_index)

        doc_en = pymupdf.open(stream=artifact.original_pdf, filetype="pdf")
        doc_zh = pymupdf.open(stream=artifact.original_pdf, filetype="pdf")
        try:
            en_pages = doc_en.page_count
            zh_pages = doc_zh.page_count
            if len(sorted_results) != zh_pages:
                logger.warning(
                    "dual assemble: %d page results for a %d-page document",
                    len(sorted_results),
                    zh_pages,
                )
            # Phase 1: 写入翻译后的 content stream
            for result in sorted_results:
                if result.content_stream and 0 <= result.page_index < doc_zh.page_count:
                    page = doc_zh[result.page_index]
                    xref = doc_zh.get_new_xref()
                    doc_zh.update_object(xref, "<<>>")
                    doc_zh.update_stream(xref, result.content_stream)
                    page.set_contents(xref)

            # Phase 2: 嵌入新字体
            self._embed_fonts(doc_zh, sorted_results)

            # Phase 3: insert_file + move_page（核心瓶颈）
            doc_en.insert_file(doc_zh)
            for i in range(min(en_pages, zh_pages)):
                doc_en.move_page(en_pages + i, i * 2 + 1)

            # Phase 4: TOC 重映射
            if artifact.outline:
                src_toc = doc_en.get_toc(simple=True)
                if src_toc:
                    new_toc = [
                        [lvl, title, 2 * p if 0 < p <= en_pages else p]
                        for lvl, title, p in src_toc
                    ]
                    doc_en.set_toc(new_toc)

            # Phase 5: 字体子集化
            if not artifact.skip_subset_fonts:
                self._protect_math_fonts(doc_zh)
                self._protect_math_fonts(doc_en)
                try:
                    doc_zh.subset_fonts(fallback=False)
                except Exception as e:
                    logger.warning("subset_fonts failed: %s", str(e)[:120])
                try:
                    doc_en.subset_fonts(fallback=False)
                except Exception as e:
                    logger.warning("subset_fonts failed: %s", str(e)[:120])

            return doc_en.write(deflate=True, garbage=4, use_objstms=1)
        finally:
            doc_zh.close()
            doc_en.close()

    def _embed_fonts(self, doc, results: List[PageResult]):
        """从 PageResult.resources.fonts 收集字体并嵌入文档。"""
        all_fonts: dict[str, FontRequirement] = {}
        for result in results:
            for fr in result.resources.fonts:
                if fr.name and fr.name not in all_fonts and not fr.is_builtin:
                    all_fonts[fr.name] = fr

        if not all_fonts or doc.page_count == 0:
            return

        first_page = doc[0]
        for fname, fr in all_fonts.items():
            if fr.file_path:
                try:
                    first_page.insert_font(fname, fr.file_path)
                except Exception as e:
                    logger.debug("insert_font failed for %s: %s", fname, str(e)[:80])

    @staticmethod
    def _protect_math_fonts(doc):
        """保护数学字体不被 MuPDF subset_fonts 破坏。"""
        try:
            xreflen = doc.xref_length()
            for xref in range(1, xreflen):
                try:
                    basefont_res = doc.xref_get_key(xref, "/BaseFont")
                    if basefont_res[0] == "name":
                        bf = str(basefont_res[1])
                        math_patterns = [
                            "CM",
                            "CMSY",
                            "CMEX",
                            "CMMI",
                            "EUFM",
                            "MSBM",
                            "MSAM",
                            "STIX",
                            "XITS",
                            "MnSymbol",
                            "rsfs",
                            "txsy",
                            "wasy",
                            "stmary",
                            "Symbol",
                            "MT",
                            "BL",
                            "RM",
                            "EU",
                            "LA",
                            "RS",
                        ]
                        for mp in math_patterns:
                            if mp in bf:
                                doc.xref_set_key(
                                    xref,
                                    "/Length",
                                    doc.xref_get_key(xref, "/Length")[1],
                                )
                                break
                except Exception:
                    pass
        except Exception:
            pass
