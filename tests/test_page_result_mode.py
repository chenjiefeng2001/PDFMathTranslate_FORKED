"""Phase 1.2 生产测试 — 验证 page_result 模式输出一致性。"""

import sys
import os
import pickle
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf
from pdf2zh.worker.process import process_page
from pdf2zh.worker.adapters import ObjPatchAdapter, PageResultAdapter
from pdf2zh.assembler.base import (
    SemanticPolicy,
    compare_page_semantics,
    summarize_semantic_diffs,
    generate_migration_report,
    PageResult,
)


def test_page_result_consistency():
    """验证 page_result 模式输出与 legacy 一致。"""
    pdf_path = os.path.join(
        os.path.dirname(__file__),
        "file",
        "2608.19903v1.pdf",  # 6 pages
    )

    if not os.path.exists(pdf_path):
        print(f"Test PDF not found: {pdf_path}")
        return False

    print(f"Testing with: {pdf_path}")

    doc = pymupdf.open(pdf_path)
    total_pages = min(5, len(doc))
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

    # 测试 page_result 模式
    print("\n--- PageResult Mode Test ---")
    page_results = []
    obj_patches_from_page_result = []

    for page_idx in range(total_pages):
        output = process_page(
            page_index=page_idx,
            doc_zh=doc,
            obj_patch=obj_patch,
            page_xref_map=page_xref_map,
        )

        # 转换为 PageResult
        page_result = PageResultAdapter.convert(output)
        page_results.append(page_result)

        # 从 PageResult 转换回 obj_patch (legacy bridge)
        obj_p = ObjPatchAdapter.convert(output, page_xref_map=page_xref_map)
        obj_patches_from_page_result.append(obj_p)

    # 验证 PageResult 可以 pickle (跨进程)
    print("\n--- Cross-Process Test ---")
    for i, pr in enumerate(page_results):
        data = pickle.dumps(pr)
        pr2 = pickle.loads(data)
        assert pr2.page_index == pr.page_index
        assert pr2.content_stream == pr.content_stream
    print("[OK] All PageResults pickle-safe")

    # 验证 legacy bridge 输出与原始 obj_patch 一致
    print("\n--- Legacy Bridge Consistency ---")
    for page_idx in range(total_pages):
        original = obj_patch[page_xref_map[page_idx]]
        bridged = obj_patches_from_page_result[page_idx].get(
            page_xref_map[page_idx], b""
        )
        if isinstance(original, str):
            original = original.encode()
        if isinstance(bridged, str):
            bridged = bridged.encode()
        assert original == bridged, f"Page {page_idx}: legacy bridge mismatch"
    print("[OK] Legacy bridge consistent")

    # 语义比较
    print("\n--- Semantic Comparison ---")
    policy = SemanticPolicy.worker_migration()
    diffs = []

    for page_idx in range(total_pages):
        diff = compare_page_semantics(
            obj_patches_from_page_result[page_idx],
            page_results[page_idx],
            policy=policy,
            page_index=page_idx,
        )
        diffs.append(diff)

    summary = summarize_semantic_diffs(diffs)
    print(summary)

    # 生成 migration report
    report = generate_migration_report(diffs)
    print(f"\nMigration Report: {report}")

    fatal_count = sum(1 for d in diffs if d.fatal)
    if fatal_count > 0:
        print(f"\nFAILED: {fatal_count} fatal diffs")
        doc.close()
        return False

    doc.close()
    print("\n=== PAGE_RESULT MODE VERIFIED ===")
    return True


if __name__ == "__main__":
    success = test_page_result_consistency()
    sys.exit(0 if success else 1)
