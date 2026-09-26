"""Golden Regression 测试框架。

三件套验证：
  1. input.pdf → PageResult artifact（worker 阶段）
  2. artifact → expected output.pdf（assembler 阶段）
  3. semantic.json 一致性检查

验证层级：
  - Artifact Diff: PageResult JSON 差异（定位 worker 问题）
  - Semantic Diff: 关键字段不变性（定位 assembler 问题）
  - PDF Diff: 输出 PDF 结构差异（最终验证）

用法：
  1. 生成 golden fixtures: python -m tests.test_golden.generate
  2. 运行 golden tests: pytest tests/test_golden.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from pdf2zh.assembler.base import PageResult, SCHEMA_VERSION

# ── Golden 目录结构 ─────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"

GOLDEN_FIXTURES = [
    "01_basic_article",
    "02_toc_heavy",
    "03_landscape",
    "04_formula",
    "05_table",
    "06_cjk",
    "07_large",
]


# ── Diff 结果 ───────────────────────────────────────────────────────


@dataclass
class ArtifactDiff:
    """PageResult JSON 差异。"""

    field: str
    expected: Any
    actual: Any
    severity: str = "warning"  # warning | error | fatal


@dataclass
class SemanticDiff:
    """语义一致性差异。"""

    check: str
    passed: bool
    expected: Optional[str] = None
    actual: Optional[str] = None
    message: str = ""


@dataclass
class GoldenResult:
    """单个 golden fixture 的验证结果。"""

    fixture_name: str
    artifact_diffs: List[ArtifactDiff]
    semantic_diffs: List[SemanticDiff]
    passed: bool

    @property
    def summary(self) -> str:
        n_artifact = len(
            [d for d in self.artifact_diffs if d.severity in ("error", "fatal")]
        )
        n_semantic = len([d for d in self.semantic_diffs if not d.passed])
        return (
            f"{self.fixture_name}: "
            f"artifact_errors={n_artifact}, semantic_errors={n_semantic}, "
            f"{'PASS' if self.passed else 'FAIL'}"
        )


# ── Artifact Diff ───────────────────────────────────────────────────

# 关键字段：golden regression 必须一致
CRITICAL_FIELDS = [
    "page_index",
    "page_size.width",
    "page_size.height",
    "geometry.rotation",
    "geometry.media_box",
]

# 重要字段：允许微小差异
IMPORTANT_FIELDS = [
    "content_stream",  # bytes hash
    "resources.fonts",
    "metrics.elapsed",
]

# 忽略字段：运行时变化
IGNORE_FIELDS = [
    "source_page_hash",
    "metrics.elapsed",
    "side_channel",
]


def _get_nested(obj: Any, path: str) -> Any:
    """获取嵌套字段值。"""
    parts = path.split(".")
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            return None
    return obj


def diff_artifact(
    expected: PageResult,
    actual: PageResult,
    critical_fields: Optional[List[str]] = None,
    important_fields: Optional[List[str]] = None,
    ignore_fields: Optional[List[str]] = None,
) -> List[ArtifactDiff]:
    """比较两个 PageResult 的差异。"""
    if critical_fields is None:
        critical_fields = CRITICAL_FIELDS
    if important_fields is None:
        important_fields = IMPORTANT_FIELDS
    if ignore_fields is None:
        ignore_fields = IGNORE_FIELDS

    diffs: List[ArtifactDiff] = []
    all_fields = set(critical_fields + important_fields)

    for field_path in all_fields:
        if any(field_path.startswith(ign) for ign in ignore_fields):
            continue

        exp_val = _get_nested(expected, field_path)
        act_val = _get_nested(actual, field_path)

        if field_path == "content_stream":
            # bytes 比较用 hash
            exp_hash = hashlib.sha256(exp_val or b"").hexdigest()[:16]
            act_hash = hashlib.sha256(act_val or b"").hexdigest()[:16]
            if exp_hash != act_hash:
                severity = "fatal" if field_path in critical_fields else "error"
                diffs.append(
                    ArtifactDiff(
                        field=field_path,
                        expected=exp_hash,
                        actual=act_hash,
                        severity=severity,
                    )
                )
        elif field_path.startswith("resources.fonts"):
            # 字体数量比较
            exp_count = len(exp_val) if exp_val else 0
            act_count = len(act_val) if act_val else 0
            if exp_count != act_count:
                diffs.append(
                    ArtifactDiff(
                        field=field_path,
                        expected=exp_count,
                        actual=act_count,
                        severity="warning",
                    )
                )
        else:
            if exp_val != act_val:
                severity = "fatal" if field_path in critical_fields else "error"
                diffs.append(
                    ArtifactDiff(
                        field=field_path,
                        expected=exp_val,
                        actual=act_val,
                        severity=severity,
                    )
                )

    return diffs


# ── Semantic Diff ───────────────────────────────────────────────────


def check_geometry_semantics(
    result: PageResult, fixture_name: str
) -> List[SemanticDiff]:
    """检查 geometry 语义正确性。"""
    checks: List[SemanticDiff] = []

    # rotation 必须是 0/90/180/270
    rot = result.rotation
    valid_rotations = {0, 90, 180, 270}
    checks.append(
        SemanticDiff(
            check="geometry.rotation_valid",
            passed=rot in valid_rotations,
            expected="0/90/180/270",
            actual=str(rot),
            message=f"rotation={rot} not in {valid_rotations}",
        )
    )

    # media_box 必须有效
    if result.geometry:
        mb = result.geometry.media_box
        valid_mb = mb[2] > mb[0] and mb[3] > mb[1]
        checks.append(
            SemanticDiff(
                check="geometry.media_box_valid",
                passed=valid_mb,
                expected="x1 > x0 and y1 > y0",
                actual=f"{mb}",
            )
        )

    # width/height 必须 > 0
    checks.append(
        SemanticDiff(
            check="page_size_positive",
            passed=result.page_width > 0 and result.page_height > 0,
            expected="width > 0 and height > 0",
            actual=f"{result.page_width}x{result.page_height}",
        )
    )

    return checks


def check_blocks_semantics(result: PageResult, fixture_name: str) -> List[SemanticDiff]:
    """检查 blocks 语义正确性。"""
    checks: List[SemanticDiff] = []

    for i, block in enumerate(result.blocks):
        prefix = f"block[{i}]"

        # bbox 必须有效
        x0, y0, x1, y1 = block.bbox
        valid_bbox = x1 > x0 and y1 > y0
        checks.append(
            SemanticDiff(
                check=f"{prefix}.bbox_valid",
                passed=valid_bbox,
                expected="x1 > x0 and y1 > y0",
                actual=f"({x0},{y0},{x1},{y1})",
            )
        )

        # confidence 范围
        valid_conf = 0.0 <= block.confidence <= 1.0
        checks.append(
            SemanticDiff(
                check=f"{prefix}.confidence_range",
                passed=valid_conf,
                expected="0.0 <= confidence <= 1.0",
                actual=str(block.confidence),
            )
        )

        # TOC block 必须有 toc_entries
        if block.is_toc:
            checks.append(
                SemanticDiff(
                    check=f"{prefix}.toc_has_entries",
                    passed=len(block.toc_entries) > 0,
                    expected="toc_entries not empty",
                    actual=f"{len(block.toc_entries)} entries",
                )
            )

            # TOC entries 必须有 confidence
            for j, entry in enumerate(block.toc_entries):
                checks.append(
                    SemanticDiff(
                        check=f"{prefix}.entry[{j}].confidence",
                        passed=0.0 <= entry.confidence <= 1.0,
                        expected="0.0 <= confidence <= 1.0",
                        actual=str(entry.confidence),
                    )
                )

    return checks


def check_semantic_consistency(
    result: PageResult,
    fixture_name: str,
) -> List[SemanticDiff]:
    """综合语义一致性检查。"""
    checks: List[SemanticDiff] = []
    checks.extend(check_geometry_semantics(result, fixture_name))
    checks.extend(check_blocks_semantics(result, fixture_name))
    return checks


# ── Golden Fixture 加载 ─────────────────────────────────────────────


def load_golden_artifact(
    fixture_name: str, page_index: int = 0
) -> Optional[PageResult]:
    """加载 golden fixture 的 artifact JSON。"""
    artifact_path = (
        GOLDEN_DIR / fixture_name / "artifact" / f"page_{page_index:03d}.json"
    )
    if not artifact_path.exists():
        return None

    with open(artifact_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return PageResult._from_dict(data)


def load_golden_expected(fixture_name: str) -> Optional[Dict]:
    """加载 golden fixture 的 expected semantic JSON。"""
    expected_path = GOLDEN_DIR / fixture_name / "expected" / "semantic.json"
    if not expected_path.exists():
        return None

    with open(expected_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_golden_input_path(fixture_name: str) -> Path:
    """获取 golden fixture 的 input PDF 路径。"""
    return GOLDEN_DIR / fixture_name / "input.pdf"


# ── Pytest Fixtures ─────────────────────────────────────────────────


@pytest.fixture(params=GOLDEN_FIXTURES, ids=GOLDEN_FIXTURES)
def golden_fixture_name(request) -> str:
    return request.param


@pytest.fixture
def golden_artifact(golden_fixture_name: str) -> Optional[PageResult]:
    return load_golden_artifact(golden_fixture_name)


@pytest.fixture
def golden_expected(golden_fixture_name: str) -> Optional[Dict]:
    return load_golden_expected(golden_fixture_name)


@pytest.fixture
def golden_input_pdf(golden_fixture_name: str) -> Optional[Path]:
    path = get_golden_input_path(golden_fixture_name)
    return path if path.exists() else None
