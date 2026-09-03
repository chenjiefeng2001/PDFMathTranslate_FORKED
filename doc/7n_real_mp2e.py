"""7N-REAL — production-like reproduction harness (evidence-only; zero prod change).

Purpose: run the REAL v1.9.16 translation of
``tests/file/The Art of Multiprocessor Programming, 2e.pdf`` exactly as the
user would, save every artifact, and then align the real output against the
7N forensic mechanisms (MECH-1 CLIP line-collapse, MECH-2 fixup decoupling).

This script NEVER imports the forensic stack to influence the run — the run
itself is the untouched production CLI.  Alignment (step 2) is read-only
post-analysis of the artifacts the production run left on disk.

Two subcommands
---------------
``run``   Execute the production CLI (pdf2zh.pdf2zh:main) in-process with the
          agreed production-like config, tee-ing all logs.
``align`` Align artifacts against the 7N trace: per-block MECH-1 (layout CLIP)
          and MECH-2 (fixup shift vs settled-command decoupling) audit lists.

Usage
-----
    python doc/7n_real_mp2e.py run  --out doc/7n-real --pages 5,8,12,20,40
    python doc/7n_real_mp2e.py align --out doc/7n-real --pages 5,8,12,20,40
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOOK = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"

# The two production engines a user can be on.  We record both configs in
# environment.txt; run one (or both) manually via the commands in the report.
CONFIGS = {
    "legacy": [
        "tests/file/The Art of Multiprocessor Programming, 2e.pdf",
        "--lang-in",
        "en",
        "--lang-out",
        "zh",
        "--service",
        "google",
        "--output",
        None,  # filled at runtime
        "--no-parallel",
        "--thread",
        "1",
    ],
    "magicpdf": [
        "tests/file/The Art of Multiprocessor Programming, 2e.pdf",
        "--parse-engine",
        "magicpdf",
        "--lang-in",
        "en",
        "--lang-out",
        "zh",
        "--service",
        "google",
        "--output",
        None,
        "--no-parallel",
        "--thread",
        "1",
        "--magicpdf-ocr-mode",
        "off",
    ],
}


def _env_snapshot() -> dict:
    import platform

    return {
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "pdf2zh_version": _pkg_version(),
        "git_head": _git_head(),
        "PDF2ZH_AUTO_SWITCH_MAGICPDF": os.environ.get(
            "PDF2ZH_AUTO_SWITCH_MAGICPDF", ""
        ),
        "PDF2ZH_PARALLEL_WORKERS": os.environ.get("PDF2ZH_PARALLEL_WORKERS", ""),
        "PDF2ZH_NO_PARALLEL": os.environ.get("PDF2ZH_NO_PARALLEL", ""),
    }


def _pkg_version() -> str:
    try:
        from pdf2zh import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


def _git_head() -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _parse_pages(spec: str) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def cmd_run(args) -> int:
    """Execute the production CLI in-process with tee'd logs."""
    import io
    import logging
    from contextlib import redirect_stdout

    os.makedirs(args.out, exist_ok=True)
    engine = args.engine
    if engine not in CONFIGS:
        print(f"unknown engine {engine}; choose from {sorted(CONFIGS)}")
        return 2

    # Production-like env: no auto engine switching, serial, temp on same drive.
    os.environ["PDF2ZH_AUTO_SWITCH_MAGICPDF"] = "0"
    os.environ.setdefault("PDF2ZH_NO_PARALLEL", "1")

    out_dir = os.path.abspath(os.path.join(args.out, f"output-{engine}"))
    os.makedirs(out_dir, exist_ok=True)

    argv = [a if a is not None else out_dir for a in CONFIGS[engine]]
    if args.pages:
        argv += ["--pages", args.pages]
    if args.debug:
        argv += ["--debug"]

    # 1) environment + config record (7N-0 contract)
    with open(os.path.join(args.out, "environment.txt"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_env_snapshot(), indent=1, ensure_ascii=False))
    with open(
        os.path.join(args.out, f"config-{engine}.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"argv": argv, "pages": args.pages or "ALL"}, fh, indent=1)

    # 2) tee all logging into run-<engine>.log (production logger is logging-)
    log_path = os.path.join(args.out, f"run-{engine}.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    tee = _Tee(sys.stderr, log_fh)

    class _TeeHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
                log_fh.write(msg + "\n")
                log_fh.flush()
            except Exception:  # noqa: BLE001
                pass

    root = logging.getLogger()
    handler = _TeeHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if args.debug else logging.INFO)

    import pdf2zh.pdf2zh as cli

    stdout_buf = io.StringIO()
    code = 1
    try:
        with redirect_stdout(stdout_buf):
            code = cli.main(argv)
    except SystemExit as e:  # argparse --help etc.
        code = int(e.code or 0)
    finally:
        log_fh.write(stdout_buf.getvalue())
        root.removeHandler(handler)
        log_fh.close()
        tee.close()

    print(f"[7N-REAL] engine={engine} exit={code}")
    print(f"[7N-REAL] log     -> {log_path}")
    print(f"[7N-REAL] output  -> {out_dir}")
    return code


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        pass


def _load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_align(args) -> int:
    """Align real-run artifacts with the 7N forensic mechanisms."""
    out = args.out
    engine = args.engine
    magic_dir = os.path.join(out, f"output-{engine}", "magicpdf")
    stem = "The Art of Multiprocessor Programming, 2e"

    plan_path = os.path.join(magic_dir, f"{stem}_render_plan.json")
    doc_path = os.path.join(magic_dir, f"{stem}_document.json")
    mono_path = os.path.join(out, f"output-{engine}", "magicpdf", f"{stem}_mono.pdf")
    for p in (plan_path, doc_path, mono_path):
        if not os.path.exists(p):
            print(f"[7N-REAL] missing artifact: {p}")
            print(
                "[7N-REAL] run the production translation first "
                "(see doc/7n0_mp2e_forensic_report.md §7N-REAL commands)."
            )
            return 2

    plan = _load_json(plan_path)
    doc = _load_json(doc_path)  # cross-check only: block counts / translated

    want_pages = set(_parse_pages(args.pages)) if args.pages else None

    # ── MECH-1 audit: layout CLIP decisions in the real plan ────────────
    mech1 = []
    decisions = Counter()
    steps_hist = Counter()
    for e in plan:
        if want_pages and int(e.get("page") or 0) not in want_pages:
            continue
        rp = e.get("render_payload") or {}
        rec = rp.get("recovery") or {}
        d = rec.get("decision")
        if d:
            decisions[d] += 1
            if rec.get("steps"):
                steps_hist["->".join(rec["steps"])] += 1
        if d == "clip":
            mech1.append(
                {
                    "block_id": e.get("block_id"),
                    "page": e.get("page"),
                    "steps": rec.get("steps"),
                    "trace": rp.get("trace"),
                    "final_font": rec.get("final_font_size"),
                    "overflow": rp.get("overflow"),
                }
            )

    # ── MECH-2 audit: fixup shift vs settled commands (decoupling) ──────
    # document.json model blocks give the settled flow payload per block;
    # the plan entry carries the fixup-modified dst_box + render_fixup.
    # Document dump sanity cross-check (pages use key "page"; blocks carry
    # metadata.translated after the real translation pass).
    doc_blocks = 0
    doc_translated = 0
    for page in doc.get("pages", []):
        for b in page.get("blocks", []):
            doc_blocks += 1
            if (b.get("metadata") or {}).get("translated"):
                doc_translated += 1

    mech2 = []
    fixup_counts = Counter()
    for e in plan:
        if want_pages and int(e.get("page") or 0) not in want_pages:
            continue
        fx = e.get("render_fixup")
        if fx:
            fixup_counts[fx] += 1
        rp = e.get("render_payload") or {}
        cmds = rp.get("commands") or []
        if e.get("render_fixup") == "shift_down" and cmds:
            first_y = float(cmds[0].get("y") or 0.0)
            box = e.get("dst_box") or [0, 0, 0, 0]
            # settled anchor sits at block top (dst_box y1, v3 y-up); after a
            # fixup shift the box moves but commands don't -> decoupled.
            decoupled = abs(first_y - float(box[3])) > 0.5
            mech2.append(
                {
                    "block_id": e.get("block_id"),
                    "page": e.get("page"),
                    "shift": e.get("render_fixup"),
                    "dst_box_y0_y1": [box[1], box[3]],
                    "first_cmd_y": round(first_y, 1),
                    "decoupled": bool(decoupled),
                }
            )

    summary = {
        "schema": "7n-real-align-v1",
        "engine": engine,
        "plan_entries": len(plan),
        "recovery_decisions": dict(decisions),
        "recovery_steps": dict(steps_hist),
        "mech1_clip_blocks": len(mech1),
        "mech1_detail": mech1[:200],
        "fixup_counts": dict(fixup_counts),
        "mech2_shifted_with_commands": len(mech2),
        "mech2_decoupled": sum(1 for m in mech2 if m["decoupled"]),
        "mech2_detail": mech2[:200],
        "mono_pdf": mono_path,
        "doc_blocks": doc_blocks,
        "doc_blocks_with_translated": doc_translated,
    }
    with open(os.path.join(out, f"align-{engine}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    print(f"[7N-REAL] recovery decisions : {dict(decisions)}")
    print(f"[7N-REAL] steps histogram    : {dict(steps_hist)}")
    print(f"[7N-REAL] MECH-1 CLIP blocks : {len(mech1)}")
    print(f"[7N-REAL] fixup counts       : {dict(fixup_counts)}")
    print(
        f"[7N-REAL] MECH-2 shifted-with-commands: {len(mech2)} "
        f"(decoupled: {summary['mech2_decoupled']})"
    )
    print(f"[7N-REAL] wrote {os.path.join(out, f'align-{engine}.json')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="execute the production CLI (zero prod change)")
    r.add_argument("--out", default="doc/7n-real")
    r.add_argument("--engine", choices=sorted(CONFIGS), default="magicpdf")
    r.add_argument("--pages", default=None, help="e.g. 5,8,12,20,40")
    r.add_argument("--debug", action="store_true")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("align", help="align real output with 7N mechanisms")
    a.add_argument("--out", default="doc/7n-real")
    a.add_argument("--engine", choices=sorted(CONFIGS), default="magicpdf")
    a.add_argument("--pages", default=None, help="e.g. 5,8,12,20,40")
    a.set_defaults(func=cmd_align)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
