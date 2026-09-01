"""Structural PDF fidelity evaluator — Commit 7D.

Renderer-independent, JSON-safe evaluation of how faithfully an *output* PDF
preserves an original *source* PDF's structure after translation/rendering.

Pipeline used by golden-corpus regression::

    source PDF  →  extract  →  normalize  →
                                compute metrics  →  EvaluationReport (JSON)
    output PDF →  extract  →  normalize  →               │
                                    ↓                     ↓
                            compare_reports(baseline, current) → pass / regressions

Primary metrics are structural (page geometry, text lines, font/style, word
bboxes, outline) — not pixels.  Semantic-aware checks (list indentation, TOC
columns/levels, preserved code geometry, outline destinations) are self-
contained heuristics that never import the semantic detectors.

Public API
----------
- :func:`extract` / :func:`normalize_pdf` — structural reading.
- :func:`compute_report` / :func:`evaluate` — metrics / end-to-end report.
- :func:`compare_reports` / :func:`save_baseline` / :func:`load_baseline` —
  regression detection + baseline persistence.
"""

from __future__ import annotations

from pdf2zh.semantic.eval.compare import (
    compare_reports,
    load_baseline,
    save_baseline,
)
from pdf2zh.semantic.eval.extract import extract
from pdf2zh.semantic.eval.metrics import compute_report
from pdf2zh.semantic.eval.normalize import normalize_doc, normalize_font, normalize_pdf

__all__ = [
    "extract",
    "normalize_doc",
    "normalize_font",
    "normalize_pdf",
    "compute_report",
    "evaluate",
    "compare_reports",
    "save_baseline",
    "load_baseline",
]


def evaluate(source_pdf: str, output_pdf: str) -> dict:
    """Produce the JSON-safe EvaluationReport for ``source_pdf -> output_pdf``.

    Args:
        source_pdf: path to the original PDF.
        output_pdf: path to the translated / rendered output PDF.

    Returns:
        ``{"metrics": {...flat metrics...}, "source_pdf":..., "output_pdf":...}``.
        The metrics field is what should be stored as the baseline.
    """
    src = normalize_pdf(source_pdf)
    out = normalize_pdf(output_pdf)
    return {
        "metrics": compute_report(src, out),
        "source_pdf": source_pdf,
        "output_pdf": output_pdf,
    }
