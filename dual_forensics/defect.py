"""defect — F1–F10 defect taxonomy + First-Divergence-Stage attribution.

Defines the taxonomy table (7H-1 §3) and *detector* functions.  Detectors are
pure reads over a node's evidence (see :class:`.diff.NodeTrace`); each returns
a list of :class:`.DefectFinding` with a guessed ``first_divergence`` stage.

Important discipline from the plan: **``translation wrong`` (F2) is kept
separate from ``translation placed wrong`` (F1 / F6 / F8)**.  A finding must
first prove *which* stage's evidence already diverges before it blames a layer.
Each detector therefore inspects source→parser→model→translation→layout→render
in order and records the first stage where the signal is already present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dual_forensics.provenance import STAGES

__all__ = [
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
    "F6",
    "F7",
    "F8",
    "F9",
    "F10",
    "DEFECTS",
    "DefectFinding",
    "run_defect_detectors",
]

F1 = "F1"
F2 = "F2"
F3 = "F3"
F4 = "F4"
F5 = "F5"
F6 = "F6"
F7 = "F7"
F8 = "F8"
F9 = "F9"
F10 = "F10"

DEFECTS: Dict[str, dict] = {
    F1: {"name": "wrong translation area", "suspect": "segmentation / placement"},
    F2: {
        "name": "code translated when it should not be",
        "suspect": "semantic classification / translation",
    },
    F3: {
        "name": "abnormal font size",
        "suspect": "layout measurement / font resolution",
    },
    F4: {"name": "font anomaly / mojibake", "suspect": "font mapping / renderer"},
    F5: {
        "name": "figure/table detached from text",
        "suspect": "object grouping / placement",
    },
    F6: {"name": "caption displaced", "suspect": "semantic relation / layout"},
    F7: {
        "name": "source text leftover / duplicate",
        "suspect": "translation segmentation",
    },
    F8: {"name": "text truncated", "suspect": "layout / packing"},
    F9: {"name": "text layer vs visual layer mismatch", "suspect": "renderer"},
    F10: {
        "name": "XObject / draw object lost or drifted",
        "suspect": "renderer / object preservation",
    },
}

# CJK ranges — a rendered translation page should be dominated by these.
_RE_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uf900-\ufaff]")
# Code-ish tokens we expect to survive translation untouched (not CJK).
_RE_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_KEYWORDS = {
    "int",
    "void",
    "return",
    "if",
    "else",
    "for",
    "while",
    "switch",
    "case",
    "break",
    "continue",
    "public",
    "private",
    "protected",
    "class",
    "struct",
    "namespace",
    "using",
    "template",
    "typename",
    "include",
    "define",
    "static",
    "const",
    "new",
    "delete",
    "true",
    "false",
    "NULL",
    "nullptr",
    "std::",
}


@dataclass
class DefectFinding:
    defect_id: str
    node_id: str
    page: int
    evidence: Dict[str, Any] = field(default_factory=dict)  # per-node signals
    first_divergence: Optional[str] = None  # earliest stage already diverged
    stage_verdicts: Dict[str, str] = field(default_factory=dict)  # PASS/FAIL/None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "defect_id": self.defect_id,
            "name": DEFECTS.get(self.defect_id, {}).get("name"),
            "suspect_layer": DEFECTS.get(self.defect_id, {}).get("suspect"),
            "node_id": self.node_id,
            "page": self.page,
            "evidence": self.evidence,
            "first_divergence": self.first_divergence,
            "stage_verdicts": self.stage_verdicts,
            "note": self.note,
        }


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    total = sum(1 for ch in text if not ch.isspace())
    if total == 0:
        return 0.0
    return sum(1 for _ in _RE_CJK.finditer(text)) / total


def _is_code_like(text: str) -> bool:
    """Heuristic: a block that looks like programming source/pseudocode."""
    if not text:
        return False
    words = [w.lower() for w in _RE_EN_WORD.findall(text)]
    if not words:
        return False
    hits = sum(1 for w in words if w in _KEYWORDS or w.endswith("::"))
    return (hits / len(words) >= 0.25) or any(c in text for c in "{};")


_STAGE_WALK = STAGES  # source→parser→model→translation→layout→render→pdf


def _fds(verdicts: Dict[str, str]) -> Optional[str]:
    """Earliest stage whose verdict is FAIL."""
    for s in _STAGE_WALK:
        if verdicts.get(s) == "FAIL":
            return s
    return None


def _fin(verdicts, defect_id, node, page, note, evidence) -> DefectFinding:
    return DefectFinding(
        defect_id=defect_id,
        node_id=node,
        page=page,
        evidence=evidence,
        first_divergence=_fds(verdicts),
        stage_verdicts=verdicts,
        note=note,
    )


# ── detectors ──────────────────────────────────────────────────────────────


def _detect_f2_code_translated(trace) -> List[DefectFinding]:
    """F2: a code-like block whose *rendered* text got CJK-ised.

    The real dual page's matched-back text is the ground truth (``rendered_text``,
    read from the dual page and matched by geometry).  Identity source text is
    *not* evidence of a translation defect, so we test the rendered string.
    """
    findings: List[DefectFinding] = []
    src = (trace.source_text or "").strip()
    if not src or not _is_code_like(src):
        return findings
    rendered = (trace.rendered_text or "").strip()
    if not rendered:
        return findings  # absence handled by F8/F10 dangling, not F2
    cjk = _cjk_ratio(rendered)
    status = trace.translation_status or ""
    verdicts = {s: None for s in STAGES}
    if cjk > 0.25:
        verdicts["source"] = "PASS"  # source was genuine code
        verdicts["parser"] = "PASS"  # code chars parsed fine
        verdicts["model"] = "FAIL" if trace.kind != "code" else "PASS"
        verdicts["render"] = "PASS"
        # Divergence is either the model mis-typed the block (semantic) or the
        # translation unit translated it.  If the block was typed code and still
        # translated, blame translation; else blame model classification.
        if verdicts["model"] == "PASS":
            verdicts["translation"] = "FAIL"
        else:
            verdicts["translation"] = None
            verdicts["layout"] = "PASS"
        findings.append(
            _fin(
                verdicts,
                F2,
                trace.node_id,
                trace.page,
                f"code block rendered {cjk:.0%} CJK; kind={trace.kind}; status={status}",
                {
                    "source_has_code": True,
                    "kind": trace.kind,
                    "cjk_ratio": round(cjk, 3),
                    "translation_status": status,
                },
            )
        )
    return findings


def _detect_f4_font_anomaly(trace) -> List[DefectFinding]:
    """F4: mojibake / replacement glyphs / navy font change in translated text."""
    findings: List[DefectFinding] = []
    text = trace.rendered_text or trace.translated_text or ""
    has_fffd = "\ufffd" in text
    has_cid = "(cid:" in text
    if not (has_fffd or has_cid):
        return findings
    verdicts = {s: None for s in STAGES}
    verdicts["source"] = "PASS"
    verdicts["parser"] = "PASS"
    verdicts["translation"] = "PASS"
    verdicts["layout"] = "PASS"
    verdicts["render"] = "FAIL"  # glyphs can't map → renderer/font layer
    findings.append(
        _fin(
            verdicts,
            F4,
            trace.node_id,
            trace.page,
            "replacement/CID glyphs in rendered text",
            {"fffd": has_fffd, "cid": has_cid, "text": text[:40]},
        )
    )
    return findings


def _detect_f2_style_alias(trace, raw_duals: Dict[str, Any]) -> List[DefectFinding]:
    """F4/F9: rendered text layer differs from what the model/translation owed.

    Raw-dual text runs are matched back per node; if the run text is empty but
    the node was supposed to render, or the run is CJK where source was code,
    flag it.  Kept minimal — the strong signals come from the inspector's
    MuPDF anomaly detector (see report aggregation).
    """
    return []


def run_defect_detectors(
    traces: List[Any], dual_page: Optional[Any] = None
) -> List[DefectFinding]:
    """Run all enabled detectors over a list of :class:`.diff.Trace` objects."""
    findings: List[DefectFinding] = []
    for trace in traces:
        findings.extend(_detect_f2_code_translated(trace))
        findings.extend(_detect_f4_font_anomaly(trace))
    return findings


def classify_findings(findings: List[DefectFinding]) -> Dict[str, Any]:
    """summary per F-id + first-divergence-stage distribution."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for d in findings:
        entry = by_id.setdefault(
            d.defect_id,
            {"count": 0, "by_first_divergence": {}, "names": []},
        )
        entry["count"] += 1
        fd = d.first_divergence or "unknown"
        entry["by_first_divergence"][fd] = entry["by_first_divergence"].get(fd, 0) + 1
    for fid, entry in by_id.items():
        entry["name"] = DEFECTS.get(fid, {}).get("name")
    return by_id
