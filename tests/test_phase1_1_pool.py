"""Phase 1.1 十页 ProcessPool 测试 — 验证 worker isolation + ordering。"""

import sys
import os
import pickle
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed

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
)


def process_page_worker(args):
    """Worker 函数（在子进程中运行）。"""
    page_idx, pdf_bytes, obj_patch_dict, page_xref_map = args

    # 在子进程中打开 PDF
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    output = process_page(
        page_index=page_idx,
        doc_zh=doc,
        obj_patch=obj_patch_dict,
        page_xref_map=page_xref_map,
    )

    # 转换为 PageResult (pickle-safe)
    page_result = PageResultAdapter.convert(output)

    # 转换为 obj_patch
    obj_patch_result = ObjPatchAdapter.convert(output, page_xref_map=page_xref_map)

    doc.close()

    return {
        "page_index": page_idx,
        "page_result": page_result,
        "obj_patch": obj_patch_result,
        "source_page_hash": output.source_page_hash,
    }


def test_ten_pages_processpool():
    """测试十页 ProcessPool。"""
    # 使用 12 页的测试 PDF
    pdf_path = os.path.join(
        os.path.dirname(__file__),
        "file",
        "1808.08763v3.pdf",  # 15 pages
    )

    if not os.path.exists(pdf_path):
        print(f"Test PDF not found: {pdf_path}")
        return False

    print(f"Testing with: {pdf_path}")

    # 读取 PDF 字节
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    total_pages = min(10, len(doc))  # 只测试前 10 页
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

    doc.close()

    print(f"Generated obj_patch with {len(obj_patch)} entries")

    # 准备 worker 参数
    worker_args = [
        (page_idx, pdf_bytes, obj_patch, page_xref_map)
        for page_idx in range(total_pages)
    ]

    # 使用 ProcessPoolExecutor
    print(f"\n--- ProcessPool Test ({total_pages} pages) ---")
    results = []

    try:
        with ProcessPoolExecutor(max_workers=4) as executor:
            # 提交所有任务
            future_to_page = {
                executor.submit(process_page_worker, args): args[0]
                for args in worker_args
            }

            # 收集结果
            for future in as_completed(future_to_page):
                page_idx = future_to_page[future]
                try:
                    result = future.result()
                    results.append(result)
                    print(
                        f"  Page {page_idx}: OK (hash={result['source_page_hash'][:8]})"
                    )
                except Exception as e:
                    print(f"  Page {page_idx}: FAILED - {e}")
                    import traceback

                    traceback.print_exc()
                    return False

    except Exception as e:
        print(f"ProcessPool failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    # 按 page_index 排序
    results.sort(key=lambda r: r["page_index"])

    # 验证 ordering
    print("\n--- Ordering Tests ---")
    for i, result in enumerate(results):
        assert (
            result["page_index"] == i
        ), f"Ordering mismatch: expected {i}, got {result['page_index']}"
    print("  Page ordering: OK")

    # 验证 worker isolation (无 fitz object 泄漏)
    print("\n--- Worker Isolation Tests ---")
    for result in results:
        page_result = result["page_result"]
        # PageResult 应该是 pickle-safe 的
        data = pickle.dumps(page_result)
        pr2 = pickle.loads(data)
        assert pr2.page_index == page_result.page_index

        # obj_patch 应该是简单的 dict
        obj_p = result["obj_patch"]
        assert isinstance(obj_p, dict)
        for key, val in obj_p.items():
            assert isinstance(key, int)
            assert isinstance(val, bytes)

    print("  Worker isolation: OK (no fitz objects)")

    # 语义比较
    print("\n--- Semantic Comparison ---")
    policy = SemanticPolicy.worker_migration()
    diffs = []

    for result in results:
        diff = compare_page_semantics(
            result["obj_patch"],
            result["page_result"],
            policy=policy,
            page_index=result["page_index"],
        )
        diffs.append(diff)

    # 打印汇总
    summary = summarize_semantic_diffs(diffs)
    print(summary)

    # 检查是否有 fatal
    fatal_count = sum(1 for d in diffs if d.fatal)
    if fatal_count > 0:
        print(f"\nFAILED: {fatal_count} fatal diffs")
        return False

    print("\n=== ALL TESTS PASSED ===")
    return True


if __name__ == "__main__":
    # Windows 需要 this
    mp.freeze_support()
    success = test_ten_pages_processpool()
    sys.exit(0 if success else 1)
