"""Module: Diagnostics — Phase 5.1/5.2 文档质量分析器（编译器 Warning 系统）。

对统一文档模型跑全量检测，产出 DiagnosticReport：

    warning: Page 53: TOC entry confidence low
    error:   Page 12: Unicode 损坏（� 字形 ×7）
    warning: Page 89: Translation overflow detected

每个问题带 node_id/page/code/severity/evidence；``analyze_document``
同时给出节点级 Confidence Model（confidence/source/uncertainty）——
修复与否由 RepairEngine 依据报告决策。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.v3.document_model import DocumentModel, block_id

_RE_DOTTED = re.compile(r"\b\d+(?:\.\d+){1,3}\b")
_RE_LEADER = re.compile(r"[.·…‥]{3,}")

# 检测项 → 严重度/代码
CODE_SEVERITY = {
    "unicode_error": "error",
    "toc_merged_lines": "error",
    "toc_low_confidence": "warning",
    "formula_low_confidence": "warning",
    "translation_overflow": "warning",
    "font_uncertain": "warning",
    "empty_block": "warning",
}


@dataclass
class DiagnosticIssue:
    code: str = ""
    node_id: str = ""
    page: int = 0
    message: str = ""
    severity: str = "warning"
    evidence: Dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "node_id": self.node_id,
            "page": self.page,
            "message": self.message,
            "severity": self.severity,
            "evidence": dict(self.evidence),
        }


@dataclass
class DiagnosticReport:
    issues: List[DiagnosticIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.is_error)

    @property
    def warning_count(self) -> int:
        return len(self.issues) - self.error_count

    @property
    def admissible(self) -> bool:
        """无 error 级问题才允许直接输出（否则进 Repair Pipeline）。"""
        return self.error_count == 0

    def issues_for(self, node_id: str) -> List[DiagnosticIssue]:
        return [i for i in self.issues if i.node_id == node_id]

    def to_dict(self) -> dict:
        return {
            "errors": self.error_count,
            "warnings": self.warning_count,
            "admissible": self.admissible,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        return (
            f"Diagnostics errors={self.error_count} "
            f"warnings={self.warning_count} admissible={self.admissible}"
        )


# ── 检测器 ────────────────────────────────────────────────────────────────


def _unicode_issues(model, issue_list: List[DiagnosticIssue]) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            bad = [
                (g.decode, g.char)
                for l in block.lines
                for s in l.spans
                for g in s.glyphs
                if g.decode != "ok"
            ]
            if not bad and not any("\ufffd" in (l.text or "") for l in block.lines):
                continue
            issue_list.append(
                DiagnosticIssue(
                    code="unicode_error",
                    node_id=block_id(pno, i),
                    page=pno,
                    message=f"Unicode 损坏（{'�' if bad else 'fffd'} 字形 ×{len(bad) or 1}）",
                    severity=CODE_SEVERITY["unicode_error"],
                    evidence={
                        "bad_glyphs": len(bad),
                        "fonts": block.metadata.get("fonts", {}),
                    },
                )
            )


def _toc_issues(model, issue_list, toc_low_threshold: float = 0.6) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            md = block.metadata
            if md.get("kind") == "toc":
                conf = float(md.get("toc_confidence", 0.0) or 0.0)
                if conf < toc_low_threshold:
                    issue_list.append(
                        DiagnosticIssue(
                            code="toc_low_confidence",
                            node_id=block_id(pno, i),
                            page=pno,
                            message=f"TOC entry confidence low ({conf:.2f})",
                            severity=CODE_SEVERITY["toc_low_confidence"],
                            evidence={
                                "confidence": round(conf, 3),
                                "scan": md.get("toc_scan", False),
                            },
                        )
                    )
                continue
            # 合并行：多行块 + ≥2 点号编号（+ leader 或 ≥3 编号）→ TOC 被压成一段
            text = block.text or ""
            dotted = _RE_DOTTED.findall(text)
            if (
                block.line_count >= 2
                and len(dotted) >= 2
                and (_RE_LEADER.search(text) or len(dotted) >= 3)
            ):
                issue_list.append(
                    DiagnosticIssue(
                        code="toc_merged_lines",
                        node_id=block_id(pno, i),
                        page=pno,
                        message="TOC 多行被合并成一段（Line Builder 阈值过大）",
                        severity=CODE_SEVERITY["toc_merged_lines"],
                        evidence={"lines": block.line_count, "numbers": dotted[:5]},
                    )
                )


def _formula_issues(model, issue_list, formula_threshold: float = 0.5) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            if block.kind != "formula":
                continue
            density = float(block.metadata.get("formula_density", 1.0) or 1.0)
            if density < formula_threshold:
                issue_list.append(
                    DiagnosticIssue(
                        code="formula_low_confidence",
                        node_id=block_id(pno, i),
                        page=pno,
                        message=f"Formula reconstruction failed "
                        f"(density {density:.2f})",
                        severity=CODE_SEVERITY["formula_low_confidence"],
                        evidence={"formula_density": round(density, 3)},
                    )
                )


def _overflow_issues(model, issue_list) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            typo = block.metadata.get("typography") or {}
            if typo.get("overflow"):
                issue_list.append(
                    DiagnosticIssue(
                        code="translation_overflow",
                        node_id=block_id(pno, i),
                        page=pno,
                        message="Translation overflow detected",
                        severity=CODE_SEVERITY["translation_overflow"],
                        evidence={"lines": typo.get("line_count", 0)},
                    )
                )


def _font_uncertain(model, issue_list) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            fonts = block.metadata.get("fonts") or {}
            if (
                len(fonts) > 1
                and block.metadata.get("anomaly")
                == f"orphan_glyphs:{len(page.unassigned_glyphs)}"
            ):
                issue_list.append(
                    DiagnosticIssue(
                        code="font_uncertain",
                        node_id=block_id(pno, i),
                        page=pno,
                        message="Font mapping uncertain (multifont)",
                        severity=CODE_SEVERITY["font_uncertain"],
                        evidence={"fonts": list(fonts)},
                    )
                )


def _empty_issues(model, issue_list) -> None:
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            if block.metadata.get("anomaly") == "empty_text":
                issue_list.append(
                    DiagnosticIssue(
                        code="empty_block",
                        node_id=block_id(pno, i),
                        page=pno,
                        message="Empty text block detected",
                        severity=CODE_SEVERITY["empty_block"],
                        evidence={"bbox": list(block.bbox)},
                    )
                )


def analyze_document(
    model: DocumentModel, toc_low_threshold: float = 0.6, formula_threshold: float = 0.5
) -> DiagnosticReport:
    """全量检测 → DiagnosticReport（admissible=False 时进 Repair Pipeline）。"""
    issues: List[DiagnosticIssue] = []
    _unicode_issues(model, issues)
    _toc_issues(model, issues, toc_low_threshold)
    _formula_issues(model, issues, formula_threshold)
    _overflow_issues(model, issues)
    _font_uncertain(model, issues)
    _empty_issues(model, issues)
    return DiagnosticReport(issues)


# ── Confidence Model（Step 2：每个节点 confidence/source/uncertainty） ────

_KIND_BASE = {
    "toc": 0.55,
    "heading": 0.65,
    "caption": 0.6,
    "formula": 0.6,
    "footnote": 0.6,
    "paragraph": 0.85,
    "code": 0.7,
    "table": 0.7,
    "figure": 0.9,
    "header": 0.8,
    "footer": 0.8,
}


def node_confidence(block) -> tuple:
    """块级置信度：(confidence, source, uncertainty)。

    基础分按 kind；角色置信度上调；替换字符/溢出/空块强惩罚。
    """
    md = block.metadata or {}
    base = _KIND_BASE.get(block.kind, 0.7)
    conf = base
    source = "geometry"
    role_conf = float(md.get("role_confidence", 0.0) or 0.0)
    if role_conf:
        conf = 0.4 + 0.6 * role_conf
        source = "roles"
    if block.kind == "toc":
        toc_conf = float(md.get("toc_confidence", 0.0) or 0.0)
        conf = 0.5 + 0.5 * toc_conf
        source = "toc_scan" if md.get("toc_scan") else "toc_gate"
    if any(g.decode != "ok" for l in block.lines for s in l.spans for g in s.glyphs):
        conf *= 0.3
        source = f"{source}+unicode"
    if (md.get("typography") or {}).get("overflow"):
        conf *= 0.8
    if md.get("anomaly") == "empty_text":
        conf = 0.1
    conf = max(0.05, min(conf, 0.99))
    return round(conf, 3), source, round(1.0 - conf, 3)


def annotate_confidence(model: DocumentModel) -> dict:
    """为每个块写 metadata.confidence/confidence_source/uncertainty。"""
    stats = {"annotated": 0}
    for page in model.pages:
        for block in page.blocks:
            conf, source, unc = node_confidence(block)
            block.metadata["confidence"] = conf
            block.metadata["confidence_source"] = source
            block.metadata["uncertainty"] = unc
            stats["annotated"] += 1
    return stats


__all__ = [
    "CODE_SEVERITY",
    "DiagnosticIssue",
    "DiagnosticReport",
    "analyze_document",
    "node_confidence",
    "annotate_confidence",
]
