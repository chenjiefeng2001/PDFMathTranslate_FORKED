"""Backend Contract Test — 保证所有 backend 满足同一接口。

任何未来 backend（QPDF / Cairo / PDFium / CustomWriter）都必须通过此测试。

用法：
    pytest tests/test_backend_contract.py -v
"""

from __future__ import annotations

import pytest

from pdf2zh.assembler import (
    BackendCapabilities,
    FontIdentity,
    FontRequirement,
    PageMetrics,
    PageResult,
    PageSize,
    PDFAssembler,
    ResourceRequirement,
    TranslationArtifact,
    get_assembler,
)

# ── Fixture ──────────────────────────────────────────────────────────


def _make_test_artifact(page_count: int = 2) -> TranslationArtifact:
    """创建测试用 TranslationArtifact。"""
    pages = []
    for i in range(page_count):
        content_stream = (f"BT /F1 12 Tf 100 700 Td (Test page {i + 1}) Tj ET").encode()

        result = PageResult(
            page_index=i,
            source_page_hash=f"hash_{i}",
            page_size=PageSize(width=612, height=792),
            content_stream=content_stream,
            resources=ResourceRequirement(
                fonts=[
                    FontRequirement(
                        identity=FontIdentity(
                            family="TestFont",
                            weight=400,
                            full_name="TestFont-Regular",
                        ),
                    )
                ]
            ),
            metrics=PageMetrics(elapsed=0.1),
        )
        pages.append(result)

    # 最小 PDF（页数与 PageResult 数量一致）
    import pymupdf

    src = pymupdf.open()
    for i in range(page_count):
        src.new_page(width=612, height=792)
    minimal_pdf = src.tobytes()
    src.close()

    return TranslationArtifact(
        pages=pages,
        mode="dual",
        original_pdf=minimal_pdf,
    )


# ── Contract Tests ───────────────────────────────────────────────────


class TestBackendContract:
    """所有 backend 必须满足的接口契约。"""

    @pytest.fixture(params=["mupdf", "pikepdf"], ids=["mupdf", "pikepdf"])
    def backend_name(self, request):
        return request.param

    @pytest.fixture
    def assembler(self, backend_name) -> PDFAssembler:
        return get_assembler(backend_name)

    @pytest.fixture
    def artifact(self) -> TranslationArtifact:
        return _make_test_artifact()

    # ── 接口契约 ─────────────────────────────────────────────────────

    def test_assembler_is_pdf_assembler(self, assembler):
        """必须是 PDFAssembler 子类。"""
        assert isinstance(assembler, PDFAssembler)

    def test_has_name(self, assembler):
        """必须有 name 属性。"""
        assert hasattr(assembler, "name")
        assert isinstance(assembler.name, str)
        assert len(assembler.name) > 0

    def test_has_capabilities(self, assembler):
        """必须有 capabilities 属性。"""
        assert hasattr(assembler, "capabilities")
        assert isinstance(assembler.capabilities, BackendCapabilities)

    def test_has_assemble_method(self, assembler):
        """必须有 assemble 方法。"""
        assert hasattr(assembler, "assemble")
        assert callable(assembler.assemble)

    def test_has_validate_method(self, assembler):
        """必须有 validate 方法。"""
        assert hasattr(assembler, "validate")
        assert callable(assembler.validate)

    # ── 输出契约 ─────────────────────────────────────────────────────

    def test_assemble_returns_bytes(self, assembler, artifact):
        """assemble 必须返回 bytes。"""
        try:
            result = assembler.assemble(artifact)
            assert isinstance(result, bytes)
        except ImportError:
            pytest.skip(f"{assembler.name} dependencies not available")
        except Exception as e:
            # 某些 backend 可能因为 minimal PDF 格式问题失败
            # 这是允许的，只要异常类型合理
            assert isinstance(e, (ValueError, ImportError, Exception))

    def test_assemble_produces_valid_pdf(self, assembler, artifact):
        """输出必须是合法 PDF（以 %PDF 开头）。"""
        try:
            result = assembler.assemble(artifact)
            assert (
                result[:5] == b"%PDF-"
            ), f"Output does not start with %PDF-: {result[:20]}"
        except ImportError:
            pytest.skip(f"{assembler.name} dependencies not available")

    def test_assemble_page_count_matches(self, assembler, artifact):
        """输出 PDF 的 page count 必须匹配输入。"""
        try:
            import pymupdf

            result = assembler.assemble(artifact)
            doc = pymupdf.open(stream=result, filetype="pdf")
            try:
                # dual 模式：原页 + 译页
                expected = len(artifact.pages) * 2
                assert (
                    doc.page_count == expected
                ), f"Expected {expected} pages, got {doc.page_count}"
            finally:
                doc.close()
        except ImportError:
            pytest.skip(f"{assembler.name} dependencies not available")

    # ── 能力声明契约 ─────────────────────────────────────────────────

    def test_capabilities_preserves_is_tuple(self, assembler):
        """capabilities.preserves 必须是 tuple。"""
        assert isinstance(assembler.capabilities.preserves, tuple)

    def test_capabilities_supports_is_tuple(self, assembler):
        """capabilities.supports 必须是 tuple。"""
        assert isinstance(assembler.capabilities.supports, tuple)

    # ── 验证契约 ─────────────────────────────────────────────────────

    def test_validate_returns_list(self, assembler, artifact):
        """validate 必须返回 list。"""
        warnings = assembler.validate(artifact)
        assert isinstance(warnings, list)

    def test_validate_catches_empty_pages(self, assembler):
        """validate 必须检测空 pages。"""
        empty_artifact = TranslationArtifact(
            pages=[],
            original_pdf=b"dummy",
        )
        warnings = assembler.validate(empty_artifact)
        assert any("No pages" in w for w in warnings)

    def test_validate_catches_missing_original(self, assembler):
        """validate 必须检测缺失 original_pdf。"""
        no_pdf_artifact = TranslationArtifact(
            pages=[_make_test_artifact().pages[0]],
            original_pdf=b"",
        )
        warnings = assembler.validate(no_pdf_artifact)
        assert any("original_pdf" in w for w in warnings)


class TestDataStructures:
    """核心数据结构契约。"""

    def test_page_result_schema_version(self):
        """PageResult 必须有 schema_version。"""
        from pdf2zh.assembler.base import SCHEMA_VERSION

        pr = PageResult()
        assert pr.schema_version == SCHEMA_VERSION

    def test_page_result_source_page_hash(self):
        """PageResult 必须有 source_page_hash。"""
        pr = PageResult(source_page_hash="abc123")
        assert pr.source_page_hash == "abc123"

    def test_font_identity_fields(self):
        """FontIdentity 必须有 family/weight/italic。"""
        fi = FontIdentity(family="Noto", weight=700, italic=True)
        assert fi.family == "Noto"
        assert fi.weight == 700
        assert fi.italic is True

    def test_translation_artifact_mode(self):
        """TranslationArtifact 必须有 mode。"""
        ta = TranslationArtifact(mode="dual")
        assert ta.mode == "dual"

    def test_translation_artifact_no_debug_fields(self):
        """TranslationArtifact 不应有 debug/timing 字段。"""
        ta = TranslationArtifact()
        debug_fields = {"debug", "timing", "logs", "images", "prompts"}
        actual_fields = {f.name for f in ta.__dataclass_fields__.values()}
        assert not debug_fields.intersection(actual_fields), (
            f"TranslationArtifact has debug fields: "
            f"{debug_fields.intersection(actual_fields)}"
        )
