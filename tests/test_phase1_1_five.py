"""Phase 1.1 五页测试 — 验证 ordering + cache key + ProcessPool。"""

import sys
import os
import pickle
import hashlib

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf
from pdf2zh.worker.process import process_page
from pdf2zh.worker.adapters import ObjPatchAdapter, PageResultAdapter
from pdf2zh.assembler.base import (
    SemanticPolicy,
    compare_page_semantics,
    summarize_semantic_diffs,
    PageResult,
    SemanticDiff,
)


def test_five_pages():
    """测试五页 PageProcessOutput 生成。"""
    # 使用 6 页的测试 PDF
    pdf_path = os.path.join(
        os.path.dirname(__file__),
        "file",
        "2608.19903v1.pdf",  # 6 pages
    )

    if not os.path.exists(pdf_path):
        print(f"Test PDF not found: {pdf_path}")
        return False

    print(f"Testing with: {pdf_path}")

    # 打开 PDF
    doc = pymupdf.open(pdf_path)
    total_pages = min(5, len(doc))  # 只测试前 5 页
    print(f"PDF pages: {len(doc)}, testing: {total_pages}")

    # 模拟 translate_patch 的结果
    obj_patch = {}
    page_xref_map = {}

    for page_idx in range(total_pages):
        page = doc[page_idx]
        # 模拟一个 content stream
        mock_content = f"q 1 0 0 1 0 0 cm Q  % page {page_idx}"
        xref = page_idx + 1  # 简单映射
        obj_patch[xref] = mock_content
        page_xref_map[page_idx] = xref

    print(f"Generated obj_patch with {len(obj_patch)} entries")

    # 为每个页面生成 PageProcessOutput
    outputs = []
    page_results = []
    obj_patches = []

    for page_idx in range(total_pages):
        try:
            output = process_page(
                page_index=page_idx,
                doc_zh=doc,
                obj_patch=obj_patch,
                page_xref_map=page_xref_map,
            )
            outputs.append(output)

            # 转换为 PageResult
            page_result = PageResultAdapter.convert(output)
            page_results.append(page_result)

            # 转换为 obj_patch
            obj_patch_result = ObjPatchAdapter.convert(
                output, page_xref_map=page_xref_map
            )
            obj_patches.append(obj_patch_result)

            print(f"Page {page_idx}: OK (hash={output.source_page_hash[:8]})")

        except Exception as e:
            print(f"Page {page_idx}: FAILED - {e}")
            import traceback

            traceback.print_exc()
            doc.close()
            return False

    # 验证 ordering
    print("\n--- Ordering Tests ---")
    for i, (output, page_result) in enumerate(zip(outputs, page_results)):
        assert (
            output.page_index == i
        ), f"Output page_index mismatch: {i} vs {output.page_index}"
        assert (
            page_result.page_index == i
        ), f"PageResult page_index mismatch: {i} vs {page_result.page_index}"
    print("  Page ordering: OK")

    # 验证 cache key 稳定性
    print("\n--- Cache Key Tests ---")
    for i, output in enumerate(outputs):
        # 同一页面两次生成应该有相同的 source_page_hash
        output2 = process_page(
            page_index=i,
            doc_zh=doc,
            obj_patch=obj_patch,
            page_xref_map=page_xref_map,
        )
        assert (
            output.source_page_hash == output2.source_page_hash
        ), f"Cache key unstable for page {i}: {output.source_page_hash} vs {output2.source_page_hash}"
    print("  Cache key stability: OK")

    # 验证 pickle 序列化
    print("\n--- Serialization Tests ---")
    for i, (output, page_result, obj_p) in enumerate(
        zip(outputs, page_results, obj_patches)
    ):
        # pickle roundtrip
        data = pickle.dumps(output)
        output2 = pickle.loads(data)
        assert output2.page_index == output.page_index

        data = pickle.dumps(page_result)
        pr2 = pickle.loads(data)
        assert pr2.page_index == page_result.page_index

        data = pickle.dumps(obj_p)
        op2 = pickle.loads(data)
        assert op2 == obj_p

    print("  Pickle roundtrip: OK")

    # 语义比较
    print("\n--- Semantic Comparison ---")
    policy = SemanticPolicy.worker_migration()
    diffs = []

    for i, (obj_p, page_result) in enumerate(zip(obj_patches, page_results)):
        diff = compare_page_semantics(obj_p, page_result, policy=policy, page_index=i)
        diffs.append(diff)

    # 打印汇总
    summary = summarize_semantic_diffs(diffs)
    print(summary)

    # 检查是否有 fatal
    fatal_count = sum(1 for d in diffs if d.fatal)
    if fatal_count > 0:
        print(f"\nFAILED: {fatal_count} fatal diffs")
        doc.close()
        return False

    doc.close()
    print("\n=== ALL TESTS PASSED ===")
    return True


if __name__ == "__main__":
    success = test_five_pages()
    sys.exit(0 if success else 1)
