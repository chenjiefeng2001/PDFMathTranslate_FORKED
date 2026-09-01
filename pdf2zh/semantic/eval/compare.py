"""Baseline + regression detection — Commit 7D.

A baseline JSON captures the metrics for a known-good output along with the
pipeline provenance (pdf2zh version/commit, model), so a later run can be
measured against it:

    compare_reports(baseline, current) -> {"status", "regressions", ...}

A ``regression`` is a metric that moved counter to its direction (accuracy
dropped, or an error/count grew) beyond the per-metric tolerance.  Regressions
are reported explicitly (e.g. ``toc_page_x_accuracy 1.0 -> 0.97``), never
silently averaged away.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

#: direction: False (higher is better) / True (lower is better)
_LOWER_BETTER = {"bbox_mean_delta", "bbox_max_delta", "overflow_count"}

#: per-metric regression tolerance (absolute).  Accuracy drops beyond this, or
#: error/count grows beyond this, count as a regression.
_DEFAULT_TOL = {
    "text_exactness": 0.03,
    "bbox_mean_delta": 0.5,
    "bbox_max_delta": 5.0,
    "font_match_rate": 0.03,
    "bold_accuracy": 0.03,
    "italic_accuracy": 0.03,
    "list_marker_x_accuracy": 0.05,
    "list_content_x_accuracy": 0.05,
    "list_continuation_x_accuracy": 0.05,
    "list_wrap_integrity": 0.05,
    "list_nested_geometry_accuracy": 0.05,
    "toc_title_x_accuracy": 0.05,
    "toc_page_x_accuracy": 0.05,
    "toc_page_number_accuracy": 0.05,
    "toc_level_accuracy": 0.05,
    "toc_leader_integrity": 0.05,
    "toc_continuation_x_accuracy": 0.05,
    "toc_page_column_stability": 0.05,
    "toc_adaptive_wrap_integrity": 0.05,
    "toc_adaptive_font_size": 0.05,
    "toc_adaptive_overflow": 0.05,
    "outline_destination_accuracy": 0.05,
    "code_preserved_bbox": 0.05,
    "overflow_count": 1,
}


def _flatten(report: dict) -> dict:
    """Accept either a flat metric dict or a ``{"metrics": {...}}`` report."""
    if isinstance(report, dict) and "metrics" in report:
        return report["metrics"]
    return report


def compare_reports(
    baseline: dict,
    current: dict,
    *,
    tolerances: dict | None = None,
) -> dict:
    """Detect metric regressions between a baseline and a current report.

    Args:
        baseline: saved baseline metrics (``load_baseline`` output or a flat
            metric dict / ``evaluate`` result).
        current: current metrics/``evaluate`` result.
        tolerances: optional per-metric override of the default regression
            tolerances.

    Returns:
        ``{"status", "checks", "regressions": [{metric, baseline, current,
        delta, worse_lower}], "improvements": [...]}`` where ``status`` is
        ``"pass"`` when there are zero regressions else ``"regression"``.
    """
    b = _flatten(baseline)
    c = _flatten(current)
    tol = dict(_DEFAULT_TOL)
    tol.update(tolerances or {})

    regressions = []
    improvements = []
    checks = 0
    for key, bval in b.items():
        if key.startswith("_") or key not in c:
            continue
        cval = c[key]
        if not (isinstance(bval, (int, float)) and isinstance(cval, (int, float))):
            continue
        checks += 1
        delta = cval - bval
        tol_v = tol.get(key, 0.05)
        lower_better = key in _LOWER_BETTER
        if lower_better:
            regressed = cval > bval + tol_v
            improved = cval < bval - tol_v
        else:
            regressed = cval < bval - tol_v
            improved = cval > bval + tol_v
        entry = {
            "metric": key,
            "baseline": float(bval),
            "current": float(cval),
            "delta": float(delta),
            "worse_lower": lower_better,
        }
        if regressed:
            regressions.append(entry)
        elif improved:
            improvements.append(entry)

    return {
        "status": "pass" if not regressions else "regression",
        "checks": checks,
        "regressions": regressions,
        "improvements": improvements,
    }


def _pdf2zh_version() -> str:
    try:
        import importlib.metadata as im

        return im.version("pdf2zh")
    except Exception:  # noqa: BLE001 -- version is informational
        return "unknown"


def save_baseline(
    metrics: dict,
    path: str,
    *,
    version: str | None = None,
    model: str = "unknown",
    commit: str = "unknown",
    extra: dict | None = None,
) -> dict:
    """Persist a metrics dict as a baseline JSON with provenance metadata.

    Args:
        metrics: flat metric dict (or ``evaluate`` result; the ``metrics``
            field is used when present).
        path: destination JSON file.
        version: pdf2zh version; defaults to the installed version.
        model: model / detector snapshot the baseline was produced with.
        commit: commit id / corpus id for traceability.
        extra: optional extra metadata merged into ``meta``.

    Returns:
        The written document ``{"meta", "metrics"}``.
    """
    flat = _flatten(metrics)
    doc = {
        "meta": {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pdf2zh_version": version or _pdf2zh_version(),
            "model": model,
            "commit": commit,
            **(extra or {}),
        },
        "metrics": flat,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    return doc


def load_baseline(path: str) -> dict:
    """Load a baseline JSON written by :func:`save_baseline`."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
