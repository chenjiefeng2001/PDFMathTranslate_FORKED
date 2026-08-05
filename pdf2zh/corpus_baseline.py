"""Real-PDF Corpus IR Baseline — 100 份真实 PDF 语料 + IR 快照基线（P2）。

对目录中的真实 PDF 逐个构建 IR 快照（geometry.py → structure.py →
document_ir → snapshot_ir），落盘为 ``<stem>.ir.json`` + ``manifest.json``，
形成可与未来引擎版本 diff 的确定性基线 —— 对应路线图「100 份 PDF 测试集」
验收标准（阶段零语料库 + V8.1 快照基线在真实文档上的落地）。

CLI::

    python -m pdf2zh.corpus_baseline build <pdf_dir> <out_dir> [--max-pages N]
    python -m pdf2zh.corpus_baseline synthetic <out_dir> [--count N] [--converged]
    python -m pdf2zh.corpus_baseline diff <baseline_a> <baseline_b>

Library::

    from pdf2zh.corpus_baseline import (
        build_corpus_baseline, build_synthetic_corpus, diff_corpora,
    )
    manifest = build_corpus_baseline("pdfs/", "baseline/")
    syn_manifest = build_synthetic_corpus("baseline_syn/", count=100)
    diffs = diff_corpora("baseline/", "baseline_new/")
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from pdf2zh.evaluate import build_profile
from pdf2zh.v3.ir_convergence import converged_snapshot
from pdf2zh.v3.migration_diff import snapshot_ir
from pdf2zh.v3.structure import StructureClassifier, to_document_ir

_IR_BUCKET_KEYS = ("paragraphs", "captions", "tables", "headings",
                   "formulas", "references", "others")


def _snapshot_for_pdf(path: str, max_pages: Optional[int],
                      target_lang: str) -> Dict[str, Any]:
    """单个真实 PDF → IR 快照（确定性、无参考依赖）。"""
    prof = build_profile(path, target_lang=target_lang, max_pages=max_pages)
    ir = to_document_ir(prof.pages, classifier=StructureClassifier(),
                        title=os.path.basename(path), target_lang=target_lang)
    return snapshot_ir(ir, title=os.path.basename(path))


def build_corpus_baseline(pdf_dir: str, out_dir: str,
                          max_pages: Optional[int] = None,
                          target_lang: str = "zh-CN") -> List[Dict[str, Any]]:
    """对 ``pdf_dir`` 下所有 *.pdf 生成 IR 快照基线，写入 ``out_dir``。

    返回 manifest（每文档：stem / 页数 / 字符数 / 节点数 / 快照文件路径）。
    """
    os.makedirs(out_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    if not pdfs:
        return manifest
    for path in pdfs:
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            prof = build_profile(path, target_lang=target_lang,
                                 max_pages=max_pages)
            snapshot = snapshot_ir(
                to_document_ir(prof.pages, classifier=StructureClassifier(),
                               title=os.path.basename(path),
                               target_lang=target_lang),
                title=os.path.basename(path),
            )
            out_path = os.path.join(out_dir, f"{stem}.ir.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            manifest.append({
                "file": os.path.basename(path),
                "stem": stem,
                "pages": prof.page_count,
                "chars": prof.char_count,
                "node_count": snapshot.get("node_count", 0),
                "snapshot": out_path,
            })
        except Exception as e:  # noqa: BLE001 — 单文档失败不中断语料构建
            manifest.append({
                "file": os.path.basename(path),
                "stem": stem,
                "error": str(e)[:200],
            })
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def build_synthetic_corpus(out_dir: str, count: int = 100, seed: int = 42,
                           title_prefix: str = "synthetic",
                           converged: bool = False) -> List[Dict[str, Any]]:
    """合成语料 IR 基线（V8.7 P2 扩展）：确定性、无需真实 PDF。

    ``converged=True`` 时经 V9.0 唯一视图出口（IRBuilder.from_graph）产出
    快照，与真实 PDF 基线同构，可直接 ``diff`` 对比。
    """
    os.makedirs(out_dir, exist_ok=True)
    from pdf2zh.v3.migration_diff import SyntheticCorpus
    corpus = SyntheticCorpus(count=count, seed=seed)
    manifest: List[Dict[str, Any]] = []
    for i in range(count):
        title = f"{title_prefix}_{i:03d}"
        snapshot = corpus.snapshot(i, title=title)
        if converged:
            g = corpus.make_document_graph(i, title=title)
            snapshot = converged_snapshot(g, title=title)
        out_path = os.path.join(out_dir, f"{title}.ir.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        manifest.append({
            "file": f"{title}.ir.json",
            "stem": title,
            "pages": 1,
            "chars": 0,
            "node_count": snapshot.get("node_count", 0),
            "snapshot": out_path,
            "source": "synthetic",
        })
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def _load_snapshot(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _bucket_counts(snapshot: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {"node_count": int(snapshot.get("node_count", 0))}
    for k in _IR_BUCKET_KEYS:
        counts[k] = len(snapshot.get(k, []) or [])
    return counts


def diff_corpora(baseline_a: str, baseline_b: str) -> List[Dict[str, Any]]:
    """对比两份基线目录的 IR 快照（同 stem 逐桶计数对比）。

    返回每文档的差异记录：桶计数变化 + 是否一致（node_count 与各桶
    计数全相等视为一致）。仅存在于一侧的文档标记 missing_a/missing_b。
    """
    diffs: List[Dict[str, Any]] = []
    files_a = {os.path.basename(p) for p in glob.glob(os.path.join(baseline_a, "*.ir.json"))}
    files_b = {os.path.basename(p) for p in glob.glob(os.path.join(baseline_b, "*.ir.json"))}
    for name in sorted(files_a | files_b):
        path_a = os.path.join(baseline_a, name)
        path_b = os.path.join(baseline_b, name)
        snap_a = _load_snapshot(path_a)
        snap_b = _load_snapshot(path_b)
        if snap_a is None:
            diffs.append({"stem": name, "missing_a": True})
            continue
        if snap_b is None:
            diffs.append({"stem": name, "missing_b": True})
            continue
        ca = _bucket_counts(snap_a)
        cb = _bucket_counts(snap_b)
        changed = {k: {"a": ca[k], "b": cb[k]}
                   for k in sorted(ca) if ca[k] != cb[k]}
        diffs.append({
            "stem": name,
            "consistent": not changed,
            "changed_buckets": changed,
        })
    return diffs


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf2zh.corpus_baseline",
        description="Real-PDF corpus IR snapshot baseline (build/diff).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build", help="build IR baseline for a PDF dir")
    p_build.add_argument("pdf_dir")
    p_build.add_argument("out_dir")
    p_build.add_argument("--max-pages", type=int, default=None)
    p_build.add_argument("--target-lang", default="zh-CN")
    p_syn = sub.add_parser("synthetic", help="build deterministic synthetic IR baseline")
    p_syn.add_argument("out_dir")
    p_syn.add_argument("--count", type=int, default=100)
    p_syn.add_argument("--seed", type=int, default=42)
    p_syn.add_argument("--prefix", default="synthetic")
    p_syn.add_argument("--converged", action="store_true",
                       help="经 V9.0 唯一视图出口（IRBuilder.from_graph）产出")
    p_diff = sub.add_parser("diff", help="compare two IR baselines")
    p_diff.add_argument("baseline_a")
    p_diff.add_argument("baseline_b")
    args = parser.parse_args(argv)

    if args.command == "build":
        manifest = build_corpus_baseline(
            args.pdf_dir, args.out_dir,
            max_pages=args.max_pages, target_lang=args.target_lang,
        )
        ok = [m for m in manifest if "error" not in m]
        failed = [m for m in manifest if "error" in m]
        print(f"built {len(ok)} snapshots into {args.out_dir}")
        if failed:
            print(f"failed: {len(failed)} ({[m['stem'] for m in failed]})")
        return 0
    if args.command == "synthetic":
        manifest = build_synthetic_corpus(
            args.out_dir, count=args.count, seed=args.seed,
            title_prefix=args.prefix, converged=args.converged,
        )
        ok = [m for m in manifest if "error" not in m]
        print(f"built {len(ok)} synthetic snapshots into {args.out_dir}")
        return 0
    diffs = diff_corpora(args.baseline_a, args.baseline_b)
    inconsistent = [d for d in diffs if not d.get("consistent", False)
                    or "missing_a" in d or "missing_b" in d]
    print(f"diffed {len(diffs)} documents: "
          f"{len(diffs) - len(inconsistent)} consistent, "
          f"{len(inconsistent)} changed")
    for d in inconsistent:
        print(json.dumps(d, ensure_ascii=False))
    return 0 if not inconsistent else 1


if __name__ == "__main__":
    sys.exit(main())
