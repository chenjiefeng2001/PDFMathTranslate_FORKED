"""Module: CorpusRegression — Phase 5.5 文档测试框架（回归语料）。

编译器式回归：每个用例（PDF/模型）保存 Expected IR 桶计数，
CI/测试对比 Before/After —— 任何改动破坏旧文档立即可见。

    expected_from_model(model) → {pages, blocks, formulas, toc_entries,
                                  headings, tables, references}
    run_regression(cases, expected_map) → RegressionReport
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.document_model import DocumentModel, block_id

_KEYS = (
    "pages",
    "blocks",
    "formulas",
    "toc_entries",
    "headings",
    "tables",
    "references",
)


def expected_from_model(model: DocumentModel) -> Dict[str, int]:
    """从模型导出 Expected IR 桶计数（与诊断/报告口径一致）。"""
    counts: Dict[str, int] = {
        "pages": len(model.pages),
        "blocks": 0,
        "formulas": 0,
        "toc_entries": 0,
        "headings": 0,
        "tables": 0,
        "references": 0,
    }
    for page in model.pages:
        for block in page.blocks:
            counts["blocks"] += 1
            kind = block.kind
            if kind == "formula":
                counts["formulas"] += 1
            elif kind == "toc":
                counts["toc_entries"] += 1
            elif kind == "heading":
                counts["headings"] += 1
            elif kind == "table":
                counts["tables"] += 1
        for bid, refs in (model.metadata.get("references", {}) or {}).items():
            counts["references"] += len(refs)
    return counts


def compare_expected(
    expected: Dict[str, int], actual: Dict[str, int]
) -> Dict[str, Dict[str, int]]:
    changed = {}
    for key in _KEYS:
        e = expected.get(key, 0)
        a = actual.get(key, 0)
        if e != a:
            changed[key] = {"expected": e, "actual": a}
    return changed


@dataclass
class RegressionResult:
    name: str = ""
    passed: bool = True
    diffs: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "diffs": self.diffs}


@dataclass
class RegressionReport:
    results: List[RegressionResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return len(self.results) - self.passed_count

    def to_dict(self) -> dict:
        return {
            "passed": self.passed_count,
            "failed": self.failed_count,
            "results": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        return (
            f"Regression passed={self.passed_count}/"
            f"{len(self.results)} failed={self.failed_count}"
        )


def run_regression(
    cases: Sequence[tuple], expected_map: Dict[str, Dict[str, int]]
) -> RegressionReport:
    """``cases`` = [(name, DocumentModel), ...]；``expected_map`` = {name: 桶计数}。

    每个用例：expected_from_model(实际) vs expected_map[name]（Expected IR）。
    缺失 expected 的用例记为失败（未登记回归基线）。
    """
    report = RegressionReport()
    for name, model in cases:
        if name not in expected_map:
            report.results.append(
                RegressionResult(name, False, {"missing_expected": True})
            )
            continue
        actual = expected_from_model(model)
        diffs = compare_expected(expected_map[name], actual)
        report.results.append(RegressionResult(name, not diffs, diffs))
    return report


__all__ = [
    "_KEYS",
    "expected_from_model",
    "compare_expected",
    "RegressionResult",
    "RegressionReport",
    "run_regression",
]
