"""Module: RepairEngine — Phase 5.3 自动修复引擎（发现错误→分析原因→修复→验证）。

Repair Pipeline：

    DiagnosticReport（admissible=False）
        │
        ▼
    RepairEngine.repair（按 issue 选策略；可经 RepairPlanner 决策）
        │
        ▼
    RepairReport + 重新验证（analyze_document 对比 before/after）

策略（Repair Pass，全部只操作 DocumentModel）：
- ``TOCSplitRepair``：合并的 TOC 行 → 按行重建条目块（真实结构修复）；
- ``UnicodeRepair``：� 字形 → 标记 OCR fallback 计划（字体层修复入口）；
- ``MathRecoveryRepair``：低置信度公式 → 标记 LaTeX OCR 计划；
- ``EmptyBlockRepair``：空块 → 标记占位清理。

注：与 V4 ``repair.py``（RepairRuntime 自愈闭环）互补 —— 本模块面向
统一文档模型的诊断驱动修复。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.v3.canonical_page import BlockModel
from pdf2zh.v3.diagnostics import DiagnosticReport, analyze_document
from pdf2zh.v3.document_model import DocumentModel, block_id

log = logging.getLogger(__name__)


@dataclass
class RepairResult:
    issue_code: str = ""
    node_id: str = ""
    action: str = "none"
    repaired: bool = False
    strategy: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "issue": self.issue_code,
            "node_id": self.node_id,
            "action": self.action,
            "repaired": self.repaired,
            "strategy": self.strategy,
            "detail": self.detail,
        }


@dataclass
class RepairReport:
    results: List[RepairResult] = field(default_factory=list)
    before_errors: int = 0
    after_errors: int = 0

    @property
    def repaired_count(self) -> int:
        return sum(1 for r in self.results if r.repaired)

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "before_errors": self.before_errors,
            "after_errors": self.after_errors,
            "repaired": self.repaired_count,
        }

    def summary(self) -> str:
        return (
            f"Repair repaired={self.repaired_count}/{len(self.results)} "
            f"errors {self.before_errors}→{self.after_errors}"
        )


class RepairStrategy:
    """修复策略基类：can_repair(issue) → repair(model, issue) → RepairResult。"""

    name = "base"
    issue_code = ""

    def can_repair(self, issue) -> bool:
        return issue.code == self.issue_code

    def repair(self, model: DocumentModel, issue) -> RepairResult:
        raise NotImplementedError


_RE_LEADER = re.compile(r"[.·…‥]{3,}")
_RE_DOTTED = re.compile(r"\b\d+(?:\.\d+){1,3}\b")


class TOCSplitRepair(RepairStrategy):
    """合并的 TOC 行 → 按行重建条目块（逐行 toc 标注）。"""

    name = "toc_split"
    issue_code = "toc_merged_lines"

    def repair(self, model, issue) -> RepairResult:
        target = None
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                if block_id(page.page_num, i) == issue.node_id:
                    target = (page, i, block)
                    break
        if target is None:
            return RepairResult(
                issue.code, issue.node_id, "none", False, self.name, "block not found"
            )
        page, idx, block = target
        lines = [(l.text or "").strip() for l in block.lines if (l.text or "").strip()]
        new_blocks = []
        for text in lines:
            if _RE_DOTTED.search(text) or _RE_LEADER.search(text):
                nb = BlockModel(
                    text=text,
                    kind="toc",
                    x0=block.x0,
                    y0=block.y0,
                    x1=block.x1,
                    y1=block.y1,
                )
                nb.metadata.update(
                    {"kind": "toc", "toc_scan": True, "toc_confidence": 0.5}
                )
                new_blocks.append(nb)
            else:
                nb = BlockModel(
                    text=text,
                    kind="paragraph",
                    x0=block.x0,
                    y0=block.y0,
                    x1=block.x1,
                    y1=block.y1,
                )
                new_blocks.append(nb)
        if len(new_blocks) <= 1:
            return RepairResult(
                issue.code, issue.node_id, "none", False, self.name, "no lines to split"
            )
        page.blocks[idx : idx + 1] = new_blocks
        return RepairResult(
            issue.code,
            issue.node_id,
            "split",
            True,
            self.name,
            f"{len(new_blocks)} blocks rebuilt",
        )


class UnicodeRepair(RepairStrategy):
    """� 字形 → 标记 OCR fallback 计划（字体修复入口，不伪造数据）。"""

    name = "unicode_repair"
    issue_code = "unicode_error"

    def repair(self, model, issue) -> RepairResult:
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                if block_id(page.page_num, i) != issue.node_id:
                    continue
                block.metadata["repair"] = {
                    "action": "ocr_fallback",
                    "reason": "ToUnicode/CMap 解码失败，需 OCR 重建",
                }
                return RepairResult(
                    issue.code,
                    issue.node_id,
                    "ocr_fallback",
                    True,
                    self.name,
                    "marked for OCR fallback",
                )
        return RepairResult(
            issue.code, issue.node_id, "none", False, self.name, "block not found"
        )


class MathRecoveryRepair(RepairStrategy):
    """低置信度公式 → 标记 LaTeX OCR 计划。"""

    name = "math_recovery"
    issue_code = "formula_low_confidence"

    def repair(self, model, issue) -> RepairResult:
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                if block_id(page.page_num, i) != issue.node_id:
                    continue
                block.metadata["repair"] = {
                    "action": "latex_ocr",
                    "reason": "公式重建置信度低，走 LaTeX OCR",
                }
                return RepairResult(
                    issue.code,
                    issue.node_id,
                    "latex_ocr",
                    True,
                    self.name,
                    "marked for LaTeX OCR",
                )
        return RepairResult(
            issue.code, issue.node_id, "none", False, self.name, "block not found"
        )


class EmptyBlockRepair(RepairStrategy):
    """空块 → 标记占位清理。"""

    name = "empty_block"
    issue_code = "empty_block"

    def repair(self, model, issue) -> RepairResult:
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                if block_id(page.page_num, i) != issue.node_id:
                    continue
                block.metadata["repair"] = {
                    "action": "drop_placeholder",
                    "reason": "empty text block",
                }
                return RepairResult(
                    issue.code,
                    issue.node_id,
                    "drop_placeholder",
                    True,
                    self.name,
                    "marked for cleanup",
                )
        return RepairResult(
            issue.code, issue.node_id, "none", False, self.name, "block not found"
        )


DEFAULT_STRATEGIES = [
    TOCSplitRepair(),
    UnicodeRepair(),
    MathRecoveryRepair(),
    EmptyBlockRepair(),
]


class RepairEngine:
    """修复引擎：按 issue 选择策略（可经 RepairPlanner 决策）。"""

    def __init__(
        self, strategies: Optional[List[RepairStrategy]] = None, planner=None
    ) -> None:
        self.strategies = list(strategies or DEFAULT_STRATEGIES)
        self.planner = planner  # RepairPlanner：复杂决策交给它

    def _choose(self, issue) -> Optional[RepairStrategy]:
        if self.planner is not None:
            try:
                chosen = self.planner.plan(issue.code, dict(issue.evidence))
                for s in self.strategies:
                    if s.name == chosen and s.can_repair(issue):
                        return s
            except Exception as e:  # noqa: BLE001
                log.debug("repair planner failed: %s", e)
        for s in self.strategies:
            if s.can_repair(issue):
                return s
        return None

    def repair(self, model: DocumentModel, report: DiagnosticReport) -> RepairReport:
        before = report.error_count
        out = RepairReport(before_errors=before)
        for issue in report.issues:
            strategy = self._choose(issue)
            if strategy is None:
                out.results.append(
                    RepairResult(
                        issue.code, issue.node_id, "none", False, "", "no strategy"
                    )
                )
                continue
            try:
                result = strategy.repair(model, issue)
            except Exception as e:  # noqa: BLE001
                result = RepairResult(
                    issue.code,
                    issue.node_id,
                    "none",
                    False,
                    strategy.name,
                    str(e)[:120],
                )
            out.results.append(result)
        out.after_errors = analyze_document(model).error_count
        return out


def repair_loop(
    model: DocumentModel, max_iterations: int = 2, engine: Optional[RepairEngine] = None
) -> dict:
    """修复闭环：analyze → repair → re-analyze，直到不再改善。"""
    engine = engine or RepairEngine()
    iterations = 0
    before = analyze_document(model).error_count
    last_report = None
    for _ in range(max(1, max_iterations)):
        report = analyze_document(model)
        if report.admissible:
            return {
                "iterations": iterations,
                "before_errors": before,
                "after_errors": 0,
                "improved": True,
                "report": report.to_dict(),
            }
        rr = engine.repair(model, report)
        last_report = rr
        iterations += 1
        if rr.after_errors >= rr.before_errors:
            break
    after = analyze_document(model).error_count
    return {
        "iterations": iterations,
        "before_errors": before,
        "after_errors": after,
        "improved": after < before,
        "report": last_report.to_dict() if last_report else None,
    }


__all__ = [
    "RepairResult",
    "RepairReport",
    "RepairStrategy",
    "TOCSplitRepair",
    "UnicodeRepair",
    "MathRecoveryRepair",
    "EmptyBlockRepair",
    "DEFAULT_STRATEGIES",
    "RepairEngine",
    "repair_loop",
]
