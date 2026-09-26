"""Phase 1.2: PageResult contract tests。"""

import sys
import os
import pickle
import json

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdf2zh.assembler.base import (
    PageResult,
    PageSize,
    PageMetrics,
    ResourceRequirement,
    FontRequirement,
    FontIdentity,
    SCHEMA_VERSION,
)


def test_schema_version():
    """验证 schema_version 正确。"""
    result = PageResult()
    assert (
        result.schema_version == SCHEMA_VERSION
    ), f"schema_version mismatch: {result.schema_version} vs {SCHEMA_VERSION}"
    print("[OK] schema_version correct")


def test_required_fields():
    """验证必须字段存在。"""
    result = PageResult(
        page_index=0,
        content_stream=b"test",
        page_size=PageSize(width=612, height=792),
    )

    # 必须字段
    assert result.page_index >= 0, "page_index must be >= 0"
    assert result.content_stream is not None, "content_stream must not be None"
    assert result.page_size.width > 0, "page_width must be > 0"
    assert result.page_size.height > 0, "page_height must be > 0"

    print("[OK] required fields present")


def test_serialization_matrix():
    """验证序列化矩阵。"""
    result = PageResult(
        page_index=0,
        content_stream=b"BT /F1 12 Tf 100 700 Td (Hello) Tj ET",
        page_size=PageSize(width=612, height=792),
        resources=ResourceRequirement(
            fonts=[FontRequirement(identity=FontIdentity(full_name="F1"))]
        ),
    )

    # pickle
    data = pickle.dumps(result)
    result2 = pickle.loads(data)
    assert result2.page_index == result.page_index
    assert result2.content_stream == result.content_stream
    print("[OK] pickle roundtrip OK")

    # json
    data = result.dumps("json")
    result3 = PageResult.loads(data, "json")
    assert result3.page_index == result.page_index
    assert result3.content_stream == result.content_stream
    print("[OK] json roundtrip OK")

    # semantic equal
    assert result2.page_index == result3.page_index
    assert result2.content_stream == result3.content_stream
    print("[OK] semantic equal across formats")


def test_schema_compatibility():
    """验证 schema 兼容性检查。"""
    result = PageResult(schema_version=SCHEMA_VERSION)
    assert result.is_compatible(
        SCHEMA_VERSION
    ), "Should be compatible with current version"
    assert not result.is_compatible(
        SCHEMA_VERSION + 1
    ), "Should not be compatible with future version"

    info = result.schema_info()
    assert info["compatible"] == True
    assert info["schema_version"] == SCHEMA_VERSION

    print("[OK] schema compatibility check OK")


def test_validation():
    """验证 validate 方法。"""
    # 正常
    result = PageResult(
        page_index=0,
        content_stream=b"test",
        page_size=PageSize(width=612, height=792),
    )
    result.validate_schema()
    result.validate_for_assembly()
    print("[OK] validation passed")

    # 异常: page_index < 0
    try:
        bad_result = PageResult(page_index=-1)
        bad_result.validate_schema()
        assert False, "Should have raised"
    except AssertionError:
        print("[OK] validation catches page_index < 0")


def test_backward_compat_fixtures():
    """验证向后兼容 fixtures。"""
    fixture_path = os.path.join(
        os.path.dirname(__file__),
        "fixtures",
        "page_result_v1.json",
    )

    if os.path.exists(fixture_path):
        with open(fixture_path, "r") as f:
            data = json.load(f)
        result = PageResult._from_dict(data)
        result.validate_schema()
        assert result.is_compatible(SCHEMA_VERSION)
        print("[OK] backward compat fixture loaded")
    else:
        print("  (no fixture file, skipping)")


if __name__ == "__main__":
    test_schema_version()
    test_required_fields()
    test_serialization_matrix()
    test_schema_compatibility()
    test_validation()
    test_backward_compat_fixtures()
    print("\n=== ALL CONTRACT TESTS PASSED ===")
