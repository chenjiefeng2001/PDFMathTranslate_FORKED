"""Phase 2 测试 — 验证 PageResult → TranslationArtifact → MuPDFAssembler → PDF。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf
from pdf2zh.worker.process import process_page
from pdf2zh.worker.adapters import PageResultAdapter
from pdf2zh.assembler.base import (
    TranslationArtifact,
    PageSize,
    SourceInfo,
)
from pdf2zh.assembler.factory import get_assembler


def test_mupdf_assembler():
    """测试 MuPDFAssembler 完整流程。"""
    pdf_path = os.path.join(
        os.path.dirname(__file__),
        "file",
        "2608.19903v1.pdf",  # 6 pages
    )

    if not os.path.exists(pdf_path):
        print(f"Test PDF not found: {pdf_path}")
        return False

    print(f"Testing with: {pdf_path}")

    # 读取原始 PDF
    with open(pdf_path, "rb") as f:
        original_pdf = f.read()

    doc = pymupdf.open(stream=original_pdf, filetype="pdf")
    total_pages = min(3, len(doc))  # 只测试前 3 页
    print(f"PDF pages: {len(doc)}, testing: {total_pages}")

    # 模拟 translate_patch 的结果
    obj_patch = {}
    page_xref_map = {}

    for page_idx in range(total_pages):
        page = doc[page_idx]
        mock_content = f"q 1 0 0 1 0 0 cm Q  % page {page_idx}"
        xref = page_idx + 1
        obj_patch[xref] = mock_content
        page_xref_map[page_idx] = xref

    # 生成 PageResults
    page_results = []
    for page_idx in range(total_pages):
        output = process_page(
            page_index=page_idx,
            doc_zh=doc,
            obj_patch=obj_patch,
            page_xref_map=page_xref_map,
        )
        page_result = PageResultAdapter.convert(output)
        page_results.append(page_result)

    doc.close()

    # 构建 TranslationArtifact
    artifact = TranslationArtifact(
        original_pdf=original_pdf,
        pages=page_results,
        mode="dual",
        source_info=SourceInfo(
            page_count=len(page_results),
            pdf_hash="test_hash",
        ),
    )

    print(f"TranslationArtifact: {len(artifact.pages)} pages, mode={artifact.mode}")

    # 使用 MuPDFAssembler
    assembler = get_assembler("mupdf")
    print(f"Assembler: {assembler.name}")

    try:
        output_pdf = assembler.assemble(artifact)
        print(f"Output PDF: {len(output_pdf)} bytes")

        # 验证输出 PDF
        doc_out = pymupdf.open(stream=output_pdf, filetype="pdf")
        print(f"Output pages: {doc_out.page_count}")

        # dual 模式: original_pdf 有 6 页，输出应该是 2 * 6 = 12 页
        # MuPDFAssembler 使用 insert_file 合并整个 doc_zh
        original_page_count = pymupdf.open(
            stream=original_pdf, filetype="pdf"
        ).page_count
        expected_pages = original_page_count * 2  # 6 * 2 = 12
        assert (
            doc_out.page_count == expected_pages
        ), f"Expected {expected_pages} pages, got {doc_out.page_count}"

        doc_out.close()
        print("[OK] MuPDFAssembler produced valid PDF")

        # 保存测试输出
        output_path = os.path.join(
            os.path.dirname(__file__),
            "output_phase2_test.pdf",
        )
        with open(output_path, "wb") as f:
            f.write(output_pdf)
        print(f"[OK] Output saved to {output_path}")

        return True

    except Exception as e:
        print(f"[FAIL] MuPDFAssembler error: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_mupdf_assembler()
    sys.exit(0 if success else 1)
