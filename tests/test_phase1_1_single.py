"""Phase 1.1 单页测试 — 验证 PageProcessOutput 生成。"""

import sys
import os
import pickle

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf
from pdf2zh.worker.process import process_page
from pdf2zh.worker.adapters import ObjPatchAdapter, PageResultAdapter
from pdf2zh.assembler.base import SemanticPolicy, compare_page_semantics, PageResult


def assert_serializable(obj, name: str):
    """验证对象可以被 pickle 序列化（跨进程安全）。"""
    try:
        data = pickle.dumps(obj)
        obj2 = pickle.loads(data)
        print(f"  {name}: pickle roundtrip OK ({len(data)} bytes)")
        return True
    except Exception as e:
        print(f"  {name}: pickle FAILED - {e}")
        return False


def test_single_page():
    """测试单页 PageProcessOutput 生成。"""
    # 使用最简单的测试 PDF
    pdf_path = os.path.join(
        os.path.dirname(__file__),
        "file",
        "translate.cli.plain.text.pdf",
    )

    if not os.path.exists(pdf_path):
        print(f"Test PDF not found: {pdf_path}")
        return False

    print(f"Testing with: {pdf_path}")

    # 打开 PDF
    doc = pymupdf.open(pdf_path)
    print(f"PDF pages: {len(doc)}")

    # 模拟 translate_patch 的结果
    # 在实际 Phase 1.1 中，这会由 translate_patch 生成
    # 这里我们创建一个模拟的 obj_patch
    obj_patch = {}

    # 为第一页创建一个简单的 content stream
    page = doc[0]
    rect = page.rect
    print(f"Page 0 size: {rect.width} x {rect.height}")

    # 模拟一个 content stream (PDF drawing operators)
    # q ... Q 包裹原始内容，cm 平移译文
    mock_content = f"q 1 0 0 1 0 0 cm Q"
    obj_patch[1] = mock_content  # xref 1 是页面主内容流

    # 生成 PageProcessOutput
    try:
        output = process_page(
            page_index=0,
            doc_zh=doc,
            obj_patch=obj_patch,
            page_xref_map={0: 1},  # page 0 -> xref 1
        )
        print(f"PageProcessOutput generated successfully!")
        print(f"  page_index: {output.page_index}")
        print(f"  source_page_hash: {output.source_page_hash}")
        print(f"  content_stream length: {len(output.content_stream)}")
        print(f"  page_size: {output.page_size.width} x {output.page_size.height}")

        # 验证
        output.validate()
        print(f"  validate() passed!")

        # 测试 ObjPatchAdapter
        obj_patch_result = ObjPatchAdapter.convert(output, page_xref_map={0: 1})
        print(f"ObjPatchAdapter.convert() returned: {obj_patch_result}")

        # 测试 PageResultAdapter
        page_result = PageResultAdapter.convert(output)
        print(
            f"PageResultAdapter.convert() returned: PageResult(schema_version={page_result.schema_version})"
        )

        # 验证 PageResult
        page_result.validate_schema()
        print(f"PageResult.validate_schema() passed!")

        # 测试序列化
        data = page_result.dumps("json")
        print(f"PageResult.dumps('json') returned {len(data)} bytes")

        # 测试反序列化
        page_result2 = PageResult.loads(data, "json")
        page_result2.validate_schema()
        print(f"PageResult.loads('json') + validate_schema() passed!")

        # 测试 pickle 序列化 (跨进程安全)
        print("\n--- Serialization Tests ---")
        assert_serializable(output, "PageProcessOutput")
        assert_serializable(page_result, "PageResult")
        assert_serializable(obj_patch_result, "obj_patch")

        # 测试语义比较
        policy = SemanticPolicy.worker_migration()
        diff = compare_page_semantics(
            obj_patch, page_result, policy=policy, page_index=0
        )
        print(
            f"compare_page_semantics() returned: match={diff.match}, fatal={diff.fatal}"
        )
        print(diff.summary())

        doc.close()
        return True

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        doc.close()
        return False


if __name__ == "__main__":
    success = test_single_page()
    sys.exit(0 if success else 1)
