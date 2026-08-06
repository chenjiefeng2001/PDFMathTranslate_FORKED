"""Module: Regression — Phase D9 快照语料回归（不靠人工开 PDF）。

用可观测快照（每次翻译的逐 Pass 快照）建回归基线：每个用例记
stage 集合 + 每 stage 的 sha256 —— CI 跑一遍新引擎，diff 旧基线，
任何 Pass 的输出漂移立即可见，且**根本不需要打开 PDF**。

    from pdf2zh.v3.regression import (
        snapshot_hash, build_baseline_dir, diff_baselines,
        run_snapshot_regression, record_session,
    )

    build_baseline_dir("baseline/", [("doc_a", [snap_parse, snap_render]), ...])
    report = run_snapshot_regression(cases, "baseline/")
    print(report.summary())

复用 ``corpus_regression.RegressionReport`` 的结果结构，保持报告口径一致。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.corpus_regression import (
    RegressionReport, RegressionResult,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def snapshot_hash(snapshot: Optional[Dict[str, Any]]) -> str:
    """快照内容哈希（doc_id/trace_id/timestamp 不入 —— 只对内容敏感）。"""
    if not snapshot:
        return "empty"
    content = {k: v for k, v in snapshot.items()
               if k not in ("doc_id", "timestamp", "trace_id")}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()[:16]


def _stage_hashes(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for snap in snapshots:
        if not snap:
            continue
        out[snap.get("stage", "unknown")] = snapshot_hash(snap)
    return out


def record_for(stem: str, snapshots: Sequence[Dict[str, Any]],
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    hashes = _stage_hashes(snapshots)
    return {"stem": stem, "stages": sorted(hashes),
            "hashes": hashes, "extra": extra or {}}


def build_baseline_dir(out_dir: str,
                       cases: Sequence[Tuple[str, Sequence[Dict[str, Any]]]],
                       extra_map: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把 (stem, [snapshots...]) 逐用例写成 <stem>.obs.json 基线。

    返回 manifest；同一 stem 重复记录只保留最后一次。
    """
    os.makedirs(out_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    for stem, snaps in cases:
        rec = record_for(stem, snaps, (extra_map or {}).get(stem))
        path = os.path.join(out_dir, f"{stem}.obs.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        manifest.append(rec)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def _load_record(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def diff_records(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """两条基线记录对比：stage 集合 + 每 stage hash。"""
    if a is None:
        return {"missing_a": True}
    if b is None:
        return {"missing_b": True}
    sa, sb = set(a.get("stages", [])), set(b.get("stages", []))
    changed: Dict[str, Any] = {}
    for s in sorted(sa | sb):
        ha, hb = a.get("hashes", {}).get(s), b.get("hashes", {}).get(s)
        if ha != hb:
            changed[s] = {"a": ha or None, "b": hb or None}
    return {"stem": a.get("stem"), "consistent": not changed and sa == sb,
            "changed_stages": changed,
            "stages_a": sorted(sa), "stages_b": sorted(sb)}


def diff_baselines(baseline_a: str, baseline_b: str) -> List[Dict[str, Any]]:
    files_a = {os.path.basename(p)
               for p in glob.glob(os.path.join(baseline_a, "*.obs.json"))}
    files_b = {os.path.basename(p)
               for p in glob.glob(os.path.join(baseline_b, "*.obs.json"))}
    out: List[Dict[str, Any]] = []
    for name in sorted(files_a | files_b):
        rec = diff_records(
            _load_record(os.path.join(baseline_a, name)),
            _load_record(os.path.join(baseline_b, name)))
        out.append(rec)
    return out


def run_snapshot_regression(
        cases: Sequence[Tuple[str, Sequence[Dict[str, Any]]]],
        baseline_dir: str) -> RegressionReport:
    """跑快照回归：每个用例与基线目录比对（缺失基线记为失败）。"""
    report = RegressionReport()
    if not os.path.isdir(baseline_dir):
        for stem, _ in cases:
            report.results.append(RegressionResult(
                stem, False, {"missing_baseline_dir": True}))
        return report
    for stem, snaps in cases:
        rec = record_for(stem, snaps)
        base = _load_record(os.path.join(baseline_dir, f"{stem}.obs.json"))
        if base is None:
            report.results.append(RegressionResult(
                stem, False, {"missing_expected": True}))
            continue
        diffs = diff_records(base, rec)
        changed = diffs.get("changed_stages") or {}
        report.results.append(RegressionResult(
            stem, bool(diffs.get("consistent")), changed))
    return report


def record_session(stem: str, session_bundle: Dict[str, Any],
                   out_dir: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 ObsSession.bundle() 落成基线记录（+ 全量快照 JSON）。"""
    os.makedirs(out_dir, exist_ok=True)
    snaps = ((session_bundle.get("snapshots") or {}).get("snapshots") or {})
    snap_list = [snaps[s] for s in sorted(snaps)]
    rec = record_for(stem, snap_list, extra)
    with open(os.path.join(out_dir, f"{stem}.obs.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, f"{stem}.snapshots.json"), "w",
              encoding="utf-8") as f:
        json.dump({s: snaps[s] for s in sorted(snaps)},
                  f, ensure_ascii=False, indent=2)
    return rec


__all__ = ["canonical_json", "snapshot_hash", "record_for",
           "build_baseline_dir", "diff_records", "diff_baselines",
           "run_snapshot_regression", "record_session"]