"""Module: DocPasses — Phase 2 Pass 框架 + PassManager（编译式流水线）。

参考 LLVM/Blink/RenderGraph：所有能力都是对统一文档模型的 Pass，
只写 metadata，绝不重新解析页面：

    DocumentModel
      │
      ▼  PassManager.run(doc)
    NormalizePass        （2.2：Unicode/空白/阅读序/异常节点）
    SemanticPass         （2.3：Heading/TOC/Formula/Code/Table/角色）
    TranslationPolicyPass（2.4：每节点翻译策略）
    TypographyPass       （Phase 3：断行/对齐/溢出/孤立段）

Pass 语义：
- ``DocumentPass.run(doc) -> dict``（stats）；异常被 PassManager 容错捕获；
- **PassDiff**：每个 Pass 前后快照逐块对比（kind/translate/policy 变化），
  任何 Pass 改坏模型立即可见；
- 新增能力 = ``manager.add(Pass)``，不改 Parser。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.canonical_page import annotate_formulas, annotate_toc_scan
from pdf2zh.v3.document_model import DocumentModel, annotate_roles

log = logging.getLogger(__name__)


# ── Pass 框架 ────────────────────────────────────────────────────────────


class DocumentPass:
    """文档级 Pass：run(doc) 只写 metadata，返回 stats dict。"""

    name = "base"

    def run(self, doc: DocumentModel) -> dict:
        raise NotImplementedError


@dataclass
class PassDiffEntry:
    block_id: str = ""
    field: str = ""
    before: object = None
    after: object = None

    def to_dict(self) -> dict:
        return {"block_id": self.block_id, "field": self.field,
                "before": self.before, "after": self.after}


@dataclass
class PassResult:
    pass_name: str = ""
    ok: bool = True
    stats: Dict = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    diff: List[PassDiffEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"pass": self.pass_name, "ok": self.ok,
                "stats": dict(self.stats), "errors": list(self.errors),
                "diff": [d.to_dict() for d in self.diff]}


@dataclass
class PassRunReport:
    results: List[PassResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_count(self) -> int:
        return len(self.results) - self.ok_count

    def to_dict(self) -> dict:
        return {"passes": [r.to_dict() for r in self.results],
                "ok": self.ok_count, "failed": self.failed_count}

    def summary(self) -> str:
        parts = " | ".join(
            f"{r.pass_name}={'ok' if r.ok else 'FAIL'}"
            + (f"({len(r.diff)}Δ)" if r.diff else "")
            for r in self.results)
        return f"PassRun ok={self.ok_count}/{len(self.results)} :: {parts}"


def _snapshot(doc: DocumentModel) -> Dict[str, dict]:
    snap: Dict[str, dict] = {}
    for page in doc.pages:
        pno = page.page_num
        for i, b in enumerate(page.blocks):
            from pdf2zh.v3.document_model import block_id
            pol = b.metadata.get("translation_policy") or {}
            snap[block_id(pno, i)] = {
                "kind": b.kind,
                "translate": b.metadata.get("translate"),
                "policy_translate": pol.get("translate"),
            }
    return snap


def _diff_snapshots(before: Dict[str, dict], after: Dict[str, dict]) -> List[PassDiffEntry]:
    entries: List[PassDiffEntry] = []
    for bid, a in (after or {}).items():
        b = (before or {}).get(bid, {})
        for field in ("kind", "translate", "policy_translate"):
            if b.get(field) != a.get(field):
                entries.append(PassDiffEntry(bid, field, b.get(field),
                                             a.get(field)))
    return entries


class PassManager:
    """Pass 流水线：add(Pass) → run(doc) → PassRunReport（含 PassDiff）。"""

    def __init__(self, passes: Optional[Sequence[DocumentPass]] = None,
                 track_diff: bool = True) -> None:
        self.passes: List[DocumentPass] = list(passes or [])
        self.track_diff = track_diff

    def add(self, pass_obj: DocumentPass) -> "PassManager":
        self.passes.append(pass_obj)
        return self

    def run(self, doc: DocumentModel) -> PassRunReport:
        report = PassRunReport()
        for p in self.passes:
            before = _snapshot(doc) if self.track_diff else None
            stats: Dict = {}
            errors: List[str] = []
            try:
                stats = p.run(doc) or {}
            except Exception as e:  # noqa: BLE001 — Pass 容错
                errors.append(f"{type(e).__name__}: {str(e)[:160]}")
                log.debug("Pass %s failed: %s", p.name, errors[-1])
            after = _snapshot(doc) if self.track_diff else None
            report.results.append(PassResult(
                pass_name=p.name, ok=not errors, stats=stats,
                errors=errors,
                diff=_diff_snapshots(before, after)
                if before is not None else []))
        return report


# ── 2.2 NormalizePass ────────────────────────────────────────────────────

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u2028\u2029]")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def normalize_text(text: str) -> str:
    """Unicode NFC + 去零宽字符 + 折叠多空格（保留换行）。"""
    s = unicodedata.normalize("NFC", text or "")
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    return s


class NormalizePass(DocumentPass):
    """Phase 2.2：清理模型（Unicode/空白/阅读序/异常节点标记）。"""

    name = "normalize"

    def run(self, doc: DocumentModel) -> dict:
        stats = {"normalized_blocks": 0, "spaces_collapsed": 0,
                 "zero_width_removed": 0, "anomalies": 0,
                 "reading_order": 0}
        for page in doc.pages:
            for i, block in enumerate(page.blocks):
                block.metadata["reading_order"] = i
                stats["reading_order"] += 1
                orig = block.text or ""
                fixed = normalize_text(orig)
                if fixed != orig:
                    block.text = fixed
                    stats["normalized_blocks"] += 1
                    stats["spaces_collapsed"] += max(
                        0, len(_MULTI_SPACE_RE.findall(orig)))
                    stats["zero_width_removed"] += len(
                        _ZERO_WIDTH_RE.findall(orig))
                if not fixed.strip():
                    block.metadata["anomaly"] = "empty_text"
                    stats["anomalies"] += 1
                if page.unassigned_glyphs:
                    block.metadata["anomaly"] = \
                        f"orphan_glyphs:{len(page.unassigned_glyphs)}"
                    stats["anomalies"] += 1
        return stats


# ── 2.3 SemanticPass ─────────────────────────────────────────────────────

_RE_CODE_KW = re.compile(
    r"\b(?:def|class|function|import|export|return|if|else|elif|for|while|"
    r"try|except|catch|switch|case|break|continue|var|let|const|void|int|"
    r"float|double|char|struct|enum|lambda|yield|pass)\b", re.IGNORECASE)
_RE_CELL_SEP = re.compile(r"\s{2,}|\||\t")


def detect_code_block(block) -> bool:
    """代码块启发：代码关键字 + 短行数（整块样式）。"""
    text = (block.text or "").strip()
    if not text:
        return False
    if _RE_CODE_KW.search(text) and block.line_count <= 4:
        return True
    return False


def detect_table_block(block) -> Optional[int]:
    """表格启发：≥2 行且大多数行含 ≥3 个单元格（2+ 空格/竖线/制表分隔）。"""
    lines = [(l.text or "") for l in getattr(block, "lines", []) or []]
    if len(lines) < 2:
        return None
    cells_per_line = [len(_RE_CELL_SEP.split(l.strip())) for l in lines
                      if l.strip()]
    if not cells_per_line:
        return None
    wide = [c for c in cells_per_line if c >= 3]
    if len(wide) >= max(1, int(len(cells_per_line) * 0.6)):
        from statistics import median
        return int(median(cells_per_line))
    return None


class SemanticPass(DocumentPass):
    """Phase 2.3：统一语义识别（Heading/TOC/Formula/Code/Table + 角色）。"""

    name = "semantic"

    def __init__(self, classifier=None) -> None:
        self.classifier = classifier

    def run(self, doc: DocumentModel) -> dict:
        stats = {"roles": 0, "toc": 0, "formula_spans": 0,
                 "code": 0, "tables": 0}
        for page in doc.pages:
            # 先做 code/table：覆盖构建期 roles 的误判（如公式样代码），
            # 避免后续 roles/toc/formula 跳过已定型块
            for block in page.blocks:
                if block.kind in ("toc", "table", "code", "header", "footer"):
                    continue
                if detect_code_block(block):
                    block.kind = "code"
                    block.metadata["kind"] = "code"
                    block.metadata["code_confidence"] = 0.6
                    stats["code"] += 1
                    continue
                cols = detect_table_block(block)
                if cols is not None:
                    block.kind = "table"
                    block.metadata["kind"] = "table"
                    block.metadata["table_cols"] = cols
                    stats["tables"] += 1
            stats["roles"] += annotate_roles(page, classifier=self.classifier)
            stats["toc"] += annotate_toc_scan(page)
            stats["formula_spans"] += annotate_formulas(page)
        return stats


# ── 2.4 TranslationPolicyPass ────────────────────────────────────────────

_KEEP_KINDS = frozenset({"formula", "figure", "image", "table", "code",
                         "header", "footer", "page_number"})

# 题注编号提取（与 CaptionNodeProcessor 同源）
_RE_CAPTION_NUMBER = re.compile(
    r"^\s*(?:(?:fig(?:ure)?|tab(?:le)?|图|表|公式|equation)\.?\s*"
    r"\.?\s*)?([0-9]+(?:\.[0-9]+)*)\s*[.:、：）)\-–—]?\s*(.*)$",
    re.IGNORECASE)


def translation_policy_for(block) -> dict:
    """为单个块生成翻译策略（Phase 5 决策的 Pass 化）。"""
    kind = block.kind
    if kind in _KEEP_KINDS:
        return {"translate": False, "partial": False,
                "preserve_format": True, "preserve_case": True,
                "preserve_math": kind == "formula",
                "preserve_code": kind == "code",
                "preserve_number": False,
                "source_text": "",
                "reason": f"kind:{kind}"}
    if kind == "toc":
        title = block.metadata.get("toc_title") or ""
        return {"translate": True, "partial": True,
                "preserve_format": True, "preserve_case": False,
                "preserve_math": False, "preserve_code": False,
                "preserve_number": True,
                "source_text": title or (block.text or ""),
                "reason": "toc:title_only"}
    if kind == "caption":
        m = _RE_CAPTION_NUMBER.match(block.text or "")
        number = m.group(1) if m and m.group(1) else ""
        rest = (m.group(2) or "").strip() if m else (block.text or "")
        return {"translate": True, "partial": bool(number),
                "preserve_format": True, "preserve_case": False,
                "preserve_math": False, "preserve_code": False,
                "preserve_number": bool(number),
                "source_text": rest or (block.text or ""),
                "reason": "caption:keep_number" if number else "caption"}
    return {"translate": True, "partial": False,
            "preserve_format": True, "preserve_case": False,
            "preserve_math": False, "preserve_code": False,
            "preserve_number": False,
            "source_text": block.text or "",
            "reason": "body"}


class TranslationPolicyPass(DocumentPass):
    """Phase 2.4：每个节点生成翻译策略（不直接 Translate(node.text)）。"""

    name = "translation_policy"

    def run(self, doc: DocumentModel) -> dict:
        stats = {"translate": 0, "preserve": 0, "partial": 0}
        for page in doc.pages:
            for block in page.blocks:
                pol = translation_policy_for(block)
                block.metadata["translation_policy"] = pol
                if pol["translate"]:
                    stats["translate"] += 1
                    if pol["partial"]:
                        stats["partial"] += 1
                else:
                    stats["preserve"] += 1
        return stats


# ── Phase 3 TypographyPass ───────────────────────────────────────────────


class TypographyPass(DocumentPass):
    """Phase 3：排版预检 —— 断行/行宽溢出/孤立段（写 metadata.typography）。

    度量优先取自模型字形宽度表（build_width_map），缺省字宽 = 字号 × 0.5。
    输出不修改文本，只标注，供后续 Renderer/碰撞层消费。
    """

    name = "typography"

    def __init__(self, default_adv_ratio: float = 0.5,
                 overflow_ratio: float = 1.15) -> None:
        self.default_adv_ratio = default_adv_ratio
        self.overflow_ratio = overflow_ratio

    def run(self, doc: DocumentModel) -> dict:
        from pdf2zh.v3.typography_engine import (
            build_width_map, line_break, measure, widow_orphan_flag,
        )
        stats = {"measured": 0, "overflow_blocks": 0, "short_paragraphs": 0}
        for page in doc.pages:
            for i, block in enumerate(page.blocks):
                text = (block.metadata.get("translated")
                        or block.text or "").strip()
                if not text:
                    continue
                widths = build_width_map(block)
                default_adv = max(block.font_size * self.default_adv_ratio,
                                  1.0) if block.font_size else 5.0
                def mfn(s):  # noqa: E306
                    return measure(s, widths, default_adv)
                max_w = max(block.x1 - block.x0, 1.0)
                lines = line_break(text, max_w, mfn)
                overflow = any(mfn(l) > max_w * self.overflow_ratio
                               for l in lines)
                if overflow:
                    stats["overflow_blocks"] += 1
                short = widow_orphan_flag(len(lines))
                if short and block.kind not in ("header", "footer"):
                    stats["short_paragraphs"] += 1
                block.metadata["typography"] = {
                    "line_count": len(lines),
                    "lines": lines[:20],
                    "overflow": overflow,
                    "short_paragraph": short,
                }
                stats["measured"] += 1
        return stats


# ── 默认流水线 ───────────────────────────────────────────────────────────


def default_pass_manager(track_diff: bool = True) -> PassManager:
    """推荐流水线：Normalize → Semantic → TranslationPolicy → Typography。"""
    return PassManager([
        NormalizePass(),
        SemanticPass(),
        TranslationPolicyPass(),
        TypographyPass(),
    ], track_diff=track_diff)


__all__ = [
    "DocumentPass", "PassDiffEntry", "PassResult", "PassRunReport",
    "PassManager", "normalize_text", "NormalizePass",
    "SemanticPass", "detect_code_block", "detect_table_block",
    "TranslationPolicyPass", "translation_policy_for",
    "TypographyPass", "default_pass_manager",
]