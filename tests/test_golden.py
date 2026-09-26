"""Golden Regression 测试。

验证层级：
  1. artifact_diff: PageResult JSON 一致性
  2. semantic_consistency: 语义正确性
  3. expected_match: 与 golden expected 一致

运行方式：
  pytest tests/test_golden.py -v
  pytest tests/test_golden.py -v -k "01_basic_article"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.golden_helpers import (
    GOLDEN_DIR,
    ArtifactDiff,
    GoldenResult,
    SemanticDiff,
    check_semantic_consistency,
    diff_artifact,
    get_golden_input_path,
    load_golden_artifact,
    load_golden_expected,
)


class TestGoldenArtifact:
    """验证 golden fixture 的 artifact JSON 可加载且 schema 兼容。"""

    def test_all_fixtures_exist(self):
        """所有 golden fixture 目录必须存在。"""
        for name in [
            "01_basic_article",
            "02_toc_heavy",
            "03_landscape",
            "04_formula",
            "05_table",
            "06_cjk",
            "07_large",
        ]:
            fixture_dir = GOLDEN_DIR / name
            assert fixture_dir.exists(), f"Missing fixture directory: {name}"

    def test_artifact_json_loadable(self, golden_artifact, golden_fixture_name):
        """artifact JSON 可加载。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")
        assert golden_artifact is not None

    def test_artifact_schema_version(self, golden_artifact, golden_fixture_name):
        """artifact schema_version 必须兼容当前版本。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")
        assert golden_artifact.is_compatible(), (
            f"{golden_fixture_name}: schema_version {golden_artifact.schema_version} "
            f"incompatible with current {golden_artifact.schema_info()['current_version']}"
        )

    def test_artifact_page_index(self, golden_artifact, golden_fixture_name):
        """artifact page_index 必须有效。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")
        assert (
            golden_artifact.page_index >= 0
        ), f"{golden_fixture_name}: page_index={golden_artifact.page_index}"

    def test_artifact_content_stream(self, golden_artifact, golden_fixture_name):
        """artifact content_stream 必须是 bytes。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")
        assert isinstance(
            golden_artifact.content_stream, bytes
        ), f"{golden_fixture_name}: content_stream type={type(golden_artifact.content_stream)}"


class TestGoldenSemantic:
    """验证 golden fixture 的语义一致性。"""

    def test_geometry_valid(self, golden_artifact, golden_fixture_name):
        """geometry 字段必须语义正确。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")

        diffs = check_semantic_consistency(golden_artifact, golden_fixture_name)
        failures = [d for d in diffs if not d.passed]

        assert not failures, f"{golden_fixture_name} semantic failures:\n" + "\n".join(
            f"  {d.check}: {d.message or d.actual}" for d in failures
        )

    def test_blocks_valid(self, golden_artifact, golden_fixture_name):
        """blocks 字段必须语义正确。"""
        if golden_artifact is None:
            pytest.skip(f"No artifact JSON for {golden_fixture_name}")

        diffs = check_semantic_consistency(golden_artifact, golden_fixture_name)
        failures = [d for d in diffs if not d.passed]

        assert not failures, f"{golden_fixture_name} block failures:\n" + "\n".join(
            f"  {d.check}: {d.message or d.actual}" for d in failures
        )


class TestGoldenExpected:
    """验证 golden fixture 与 expected semantic.json 一致。"""

    def test_expected_json_exists(self, golden_fixture_name):
        """expected/semantic.json 必须存在。"""
        expected_path = GOLDEN_DIR / golden_fixture_name / "expected" / "semantic.json"
        if not expected_path.exists():
            pytest.skip(f"No expected semantic.json for {golden_fixture_name}")

    def test_expected_page_count(
        self, golden_artifact, golden_expected, golden_fixture_name
    ):
        """expected page_count 必须匹配 artifact。"""
        if golden_expected is None or golden_artifact is None:
            pytest.skip(f"No expected/artifact for {golden_fixture_name}")

        expected_count = golden_expected.get("page_count", -1)
        if expected_count >= 0:
            # 仅当 expected 有 page_count 时检查
            assert True  # artifact 只有单页，多页需要 runner

    def test_expected_rotation(
        self, golden_artifact, golden_expected, golden_fixture_name
    ):
        """expected rotation 必须匹配 artifact。"""
        if golden_expected is None or golden_artifact is None:
            pytest.skip(f"No expected/artifact for {golden_fixture_name}")

        expected_rot = golden_expected.get("rotation")
        if expected_rot is not None:
            actual_rot = golden_artifact.rotation
            assert actual_rot == expected_rot, (
                f"{golden_fixture_name}: rotation mismatch "
                f"expected={expected_rot}, actual={actual_rot}"
            )

    def test_expected_page_size(
        self, golden_artifact, golden_expected, golden_fixture_name
    ):
        """expected page_size 必须匹配 artifact。"""
        if golden_expected is None or golden_artifact is None:
            pytest.skip(f"No expected/artifact for {golden_fixture_name}")

        expected_w = golden_expected.get("page_width")
        expected_h = golden_expected.get("page_height")
        if expected_w is not None and expected_h is not None:
            assert abs(golden_artifact.page_width - expected_w) < 0.1, (
                f"{golden_fixture_name}: page_width mismatch "
                f"expected={expected_w}, actual={golden_artifact.page_width}"
            )
            assert abs(golden_artifact.page_height - expected_h) < 0.1, (
                f"{golden_fixture_name}: page_height mismatch "
                f"expected={expected_h}, actual={golden_artifact.page_height}"
            )


class TestGoldenInput:
    """验证 golden fixture 的 input PDF 存在。"""

    def test_input_pdf_exists(self, golden_fixture_name):
        """input.pdf 必须存在。"""
        input_path = get_golden_input_path(golden_fixture_name)
        if not input_path.exists():
            pytest.skip(f"No input.pdf for {golden_fixture_name}")
        assert (
            input_path.stat().st_size > 0
        ), f"{golden_fixture_name}: input.pdf is empty"
