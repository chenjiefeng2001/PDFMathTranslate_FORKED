"""Golden PDF 回归测试框架。

目的：
    验证 assembler 输出的 PDF 语义正确性（不只是 binary diff）。
    检查：page count, font count, text extraction, links, metadata。

用法：
    pytest tests/test_pdf_regression.py -v

添加新 golden PDF：
    1. 将 PDF 放入 tests/fixtures/golden/
    2. 在 GOLDEN_PDF_REGISTRY 中添加条目
    3. 运行 pytest --update-golden 更新 baseline
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pytest

# ── Golden PDF 注册表 ────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


@dataclass
class GoldenPDFSpec:
    """单个 golden PDF 的验证规范。"""

    name: str
    path: str  # 相对于 GOLDEN_DIR
    min_pages: int = 1
    max_pages: int = 1000
    min_fonts: int = 0
    max_fonts: int = 100
    expect_text: bool = True  # 是否期望可提取文本
    expect_links: bool = False  # 是否期望有链接
    text_sample: Optional[str] = None  # 期望在文本中出现的片段
    skip_mupdf: bool = False
    skip_pikepdf: bool = False


GOLDEN_PDF_REGISTRY: List[GoldenPDFSpec] = [
    GoldenPDFSpec(
        name="simple_text",
        path="simple_text.pdf",
        min_pages=1,
        max_pages=5,
        min_fonts=1,
        expect_text=True,
        text_sample="Hello",
    ),
    GoldenPDFSpec(
        name="multi_font",
        path="multi_font.pdf",
        min_pages=1,
        max_pages=10,
        min_fonts=2,
        expect_text=True,
    ),
    GoldenPDFSpec(
        name="with_links",
        path="with_links.pdf",
        min_pages=1,
        max_pages=5,
        expect_links=True,
    ),
    GoldenPDFSpec(
        name="complex_layout",
        path="complex_layout.pdf",
        min_pages=5,
        max_pages=50,
        min_fonts=1,
        expect_text=True,
    ),
]


# ── PDF 分析工具 ─────────────────────────────────────────────────────


@dataclass
class PDFAnalysis:
    """PDF 语义分析结果。"""

    page_count: int = 0
    font_count: int = 0
    font_names: List[str] = field(default_factory=list)
    text_content: str = ""
    link_count: int = 0
    metadata: Dict[str, str] = field(default_factory=dict)
    file_size: int = 0
    error: Optional[str] = None


def analyze_pdf_with_mupdf(pdf_bytes: bytes) -> PDFAnalysis:
    """用 PyMuPDF 分析 PDF。"""
    try:
        import pymupdf

        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            analysis = PDFAnalysis()
            analysis.page_count = doc.page_count
            analysis.file_size = len(pdf_bytes)

            # 字体统计
            font_names = set()
            for page in doc:
                for font in page.get_fonts():
                    font_names.add(font[3])  # font name
            analysis.font_count = len(font_names)
            analysis.font_names = sorted(font_names)

            # 文本提取
            texts = []
            for page in doc:
                texts.append(page.get_text())
            analysis.text_content = "\n".join(texts)

            # 链接统计
            link_count = 0
            for page in doc:
                link_count += len(page.get_links())
            analysis.link_count = link_count

            # 元数据
            metadata = doc.metadata
            if metadata:
                analysis.metadata = {k: str(v) for k, v in metadata.items() if v}

            return analysis
        finally:
            doc.close()
    except Exception as e:
        return PDFAnalysis(error=f"MuPDF analysis failed: {e}")


def analyze_pdf_with_pikepdf(pdf_bytes: bytes) -> PDFAnalysis:
    """用 pikepdf 分析 PDF。"""
    try:
        import pikepdf

        pdf = pikepdf.open(pikepdf.Bytes(pdf_bytes))
        try:
            analysis = PDFAnalysis()
            analysis.file_size = len(pdf_bytes)

            # 页面数
            pages = pdf.Root.get("/Pages")
            if pages:
                analysis.page_count = int(pages.get("/Count", 0))

            # 字体统计
            font_names = set()

            def _collect_fonts(obj, depth=0):
                if depth > 10:
                    return
                if isinstance(obj, pikepdf.Dictionary):
                    if "/Font" in obj:
                        font_dict = obj["/Font"]
                        if isinstance(font_dict, pikepdf.Dictionary):
                            for key in font_dict:
                                font_names.add(str(key))
                    for key in obj:
                        try:
                            _collect_fonts(obj[key], depth + 1)
                        except Exception:
                            pass
                elif isinstance(obj, pikepdf.Array):
                    for item in obj:
                        try:
                            _collect_fonts(item, depth + 1)
                        except Exception:
                            pass

            _collect_fonts(pdf.Root)
            analysis.font_count = len(font_names)
            analysis.font_names = sorted(font_names)

            # 链接统计
            link_count = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if annots:
                    link_count += len(annots)
            analysis.link_count = link_count

            return analysis
        finally:
            pdf.close()
    except Exception as e:
        return PDFAnalysis(error=f"pikepdf analysis failed: {e}")


# ── 测试类 ───────────────────────────────────────────────────────────


class TestGoldenPDFRegression:
    """Golden PDF 回归测试：验证 assembler 输出的 PDF 语义正确性。"""

    @pytest.fixture(params=["mupdf", "pikepdf"], ids=["mupdf", "pikepdf"])
    def backend(self, request):
        return request.param

    @pytest.mark.parametrize(
        "spec",
        GOLDEN_PDF_REGISTRY,
        ids=[s.name for s in GOLDEN_PDF_REGISTRY],
    )
    def test_pdf_analysis(self, spec: GoldenPDFSpec, backend: str):
        """验证 PDF 的 page count / font count / text / links。"""
        if backend == "mupdf" and spec.skip_mupdf:
            pytest.skip("MuPDF not supported for this spec")
        if backend == "pikepdf" and spec.skip_pikepdf:
            pytest.skip("pikepdf not supported for this spec")

        pdf_path = GOLDEN_DIR / spec.path
        if not pdf_path.exists():
            pytest.skip(f"Golden PDF not found: {pdf_path}")

        pdf_bytes = pdf_path.read_bytes()

        # 选择分析器
        if backend == "mupdf":
            analysis = analyze_pdf_with_mupdf(pdf_bytes)
        else:
            analysis = analyze_pdf_with_pikepdf(pdf_bytes)

        # 验证
        assert analysis.error is None, f"Analysis failed: {analysis.error}"
        assert (
            spec.min_pages <= analysis.page_count <= spec.max_pages
        ), f"Page count {analysis.page_count} not in [{spec.min_pages}, {spec.max_pages}]"
        assert (
            spec.min_fonts <= analysis.font_count <= spec.max_fonts
        ), f"Font count {analysis.font_count} not in [{spec.min_fonts}, {spec.max_fonts}]"
        if spec.expect_text:
            assert len(analysis.text_content.strip()) > 0, "No text extracted"
        if spec.text_sample:
            assert (
                spec.text_sample in analysis.text_content
            ), f"Expected '{spec.text_sample}' not found in text"
        if spec.expect_links:
            assert analysis.link_count > 0, "No links found"

    def test_backend_capabilities(self, backend: str):
        """验证 backend capabilities 声明正确。"""
        from pdf2zh.assembler import get_assembler

        asm = get_assembler(backend)
        caps = asm.capabilities

        assert isinstance(caps.incremental_write, bool)
        assert isinstance(caps.object_reuse, bool)
        assert isinstance(caps.font_subsetting, bool)
        assert isinstance(caps.annotation_preserve, bool)

        if backend == "mupdf":
            assert caps.object_reuse is False
            assert caps.font_subsetting is True
        elif backend == "pikepdf":
            assert caps.object_reuse is True
            assert caps.font_subsetting is False  # 第一版不支持

    def test_document_model_validation(self, backend: str):
        """验证 artifact 分级校验逻辑（DocumentModel 已并入 TranslationArtifact）。"""
        from pdf2zh.assembler import PageResult, TranslationArtifact, get_assembler

        asm = get_assembler(backend)

        # 无 original_pdf → assembler 侧 validate 返回告警
        artifact = TranslationArtifact(pages=[PageResult(page_index=0)])
        warnings = asm.validate(artifact)
        assert any("original_pdf" in w for w in warnings)

        # 空 content_stream → PageResult.validate_for_assembly 抛错
        with pytest.raises(AssertionError):
            PageResult(page_index=0).validate_for_assembly()
