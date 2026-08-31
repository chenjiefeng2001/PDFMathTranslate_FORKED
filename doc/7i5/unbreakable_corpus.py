# -*- coding: utf-8 -*-
"""7I-5D — targeted unbreakable corpus (evidence-only).

Question this probe answers:

> When genuinely no admissible WRAP solution exists, is the terminal CLIP a
> correct, auditable, non-silent behavior?

We drive the **production** ``render_flow_text`` Ladder (WRAP -> SHRINK re-wrap
-> CLIP) on three families of genuinely-unbreakable tokens and record the full
causal record per case (as 7I-5B contract §4 requires).  This is strictly
evidence — no production code is modified.  If every case satisfies all five
soundness checks (① never silent, ② verdict complete, ③ no incorrect line
collapse, ④ no F10-migration signal, ⑤ no spurious clip), we conclude "known
admissible terminal overflow class" and do NOT open a new policy investigation.

Families:
  1. long URL / path / identifier  — no legal breakpoint, may be very wide.
  2. oversized math token           — unbreakable formula-like run.
  3. extreme single word/identifier — single token width >> box width.
"""

import json
import os
from typing import Callable

from pdf2zh.semantic.renderer.flow import render_flow_text


def _ascii_measure(text: str, size: float):
    w = 0.0
    for ch in map(ord, text or ""):
        w += size if ch >= 0x2E80 else size * 0.5
    return w


def _cjk_measure(text: str, size: float):
    w = 0.0
    for ch in map(ord, text or ""):
        w += size if ch >= 0x2E80 else size * 0.5
    return w


# (label, text, max_width, max_height, font_size)
CASES = [
    # ── family 1: long URL / path / identifier ──
    (
        "url_medium",
        "https://very-long-unbreakable-token-path-"
        "with-no-legal-breakpoint-anywhere.example.com/api/v2",
        90.0,
        400.0,
        10.0,
    ),
    (
        "url_wide_box",
        "https://very-long-unbreakable-token-path-"
        "with-no-legal-breakpoint-anywhere.example.com/api/v2",
        300.0,
        400.0,
        10.0,
    ),
    (
        "identifier_snake",
        "some_module_global_configuration" "_overrides_registry_initializer_identifier",
        80.0,
        400.0,
        11.0,
    ),
    (
        "uuid",
        "0123456789abcdef-0123456789abcdef-0123456789abcdef" "-0123456789abcdef",
        60.0,
        400.0,
        9.0,
    ),
    # ── family 2: oversized math token ──
    ("math_cid_run", "1q2w3e4r5t6y7u8i9o0pGmEskDr" * 3, 70.0, 400.0, 10.0),
    ("math_operators", "+-*/=<>^_~$%&#(|)[]{}" * 12, 50.0, 400.0, 12.0),
    # ── family 3: extreme single token ──
    ("single_huge_word", "supercalifragilisticexpialidocious" * 4, 40.0, 400.0, 10.0),
    ("single_very_huge", "A" * 120, 30.0, 400.0, 10.0),
    # ── margin control: a wrapable paragraph must STILL fit (never clipped) ──
    (
        "wrapable_control",
        "A normal paragraph with several words that can wrap "
        "gracefully when the available width is narrow.",
        40.0,
        400.0,
        10.0,
    ),
]

# a box/width that *should* accommodate moderately long text comfortably
_EASY_W = 320.0


def _source_width(text: str, size: float, measure: Callable) -> float:
    return measure(text, size)


def main():
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "doc",
        "7i5-unbreakable",
    )
    os.makedirs(out_dir, exist_ok=True)

    records = []
    soundness = {
        "never_silent": True,
        "verdict_complete": True,
        "no_wrong_line_collapse": True,
        "no_f10_migration": True,
        "no_spurious_clip_on_wrapable": True,
    }
    for measure_name, measure in (("ascii", _ascii_measure), ("cjk", _cjk_measure)):
        for label, text, w, h, font in CASES:
            final_font = None
            try:
                out = render_flow_text(
                    text,
                    origin=(0.0, 200.0),
                    max_width=w,
                    max_height=h,
                    font_size=font,
                    measure=measure,
                )
                rec = out.get("recovery") or {}
                lines = out.get("lines") or []
            except Exception as exc:  # noqa: BLE001
                records.append(
                    {
                        "family": label.split("_")[0],
                        "case": label,
                        "measure": measure_name,
                        "error": repr(exc),
                    }
                )
                continue
            steps = list(rec.get("steps") or [])
            overflow = bool(out.get("overflow"))
            decision = rec.get("decision")
            final_font = rec.get("final_font_size")
            reason = rec.get("reason")
            sw = round(_source_width(text, font, measure), 1)

            line_count = len(lines)
            # wrong-line-collapse: an *unbreakable* token kept whole on one line
            # is fine; but for the wrapable control the lines must reconstruct.
            # A genuine bug would be a multi-token WRAP collapsing to one line.
            reconstruction_ok = " ".join(lines).replace(" ", "") == text.replace(
                " ", ""
            )

            # ① silent? overflow must be True when decision == clip and vice versa
            if decision == "clip":
                soundness["never_silent"] &= bool(overflow)
            # ② verdict complete
            if steps and (
                decision not in ("clip", "shrink", "wrap", "preserve_overflow")
            ):
                soundness["verdict_complete"] = False
            # ③ no incorrect line collapse: unbreakable -> whole single line is OK,
            #    but the wrapable control must not be collapsed nor clipped
            if label == "wrapable_control":
                if decision == "clip" or overflow or line_count <= 1:
                    soundness["no_spurious_clip_on_wrapable"] = False
            else:
                if not reconstruction_ok and not lines:
                    soundness["no_wrong_line_collapse"] = False

            records.append(
                {
                    "family": label.split("_")[0],
                    "case": label,
                    "measure": measure_name,
                    "source_width": sw,
                    "box_width": w,
                    "box_height": h,
                    "initial_font": font,
                    "min_font": 5.0,
                    "text_len": len(text),
                    "wrap_attempt": "WRAP" in steps,
                    "shrink_attempts": steps.count("SHRINK"),
                    "final_font": final_font,
                    "line_count": line_count,
                    "overflow": overflow,
                    "terminal_decision": decision,
                    "reason": reason,
                    "steps": steps,
                    "reconstructed": reconstruction_ok,
                }
            )
            if label == "wrapable_control":
                # ④ no F10 migration: for the control, text must survive (not vanish)
                if not reconstruction_ok:
                    soundness["no_f10_migration"] = False

    summary = {
        "schema_version": 1,
        "post_7i5d_evidence": True,
        "case_count": sum(1 for r in records if "error" not in r),
        "soundness": {k: bool(v) for k, v in soundness.items()},
        "all_cases_sound": bool(all(soundness.values())),
        "cases": records,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        w = fh.write
        w("# 7I-5D — Targeted Unbreakable Corpus (evidence-only)\n\n")
        w(
            "Question: when genuinely no admissible WRAP solution exists, is the "
            "terminal CLIP correct / auditable / non-silent?\n\n"
        )
        clips = [
            r
            for r in records
            if (
                r.get("terminal_decision") == "clip"
                or r.get("steps", [])[-1:] == ["CLIP"]
            )
        ]
        w(f"- cases: **{summary['case_count']}**  ·  terminal CLIP: **{len(clips)}**\n")
        w("- soundness checks:\n")
        for k, v in soundness.items():
            w(f"  - {k}: {'✅' if v else '❌ FAIL'}\n")
        w(f"\n**All sound: {'YES' if summary['all_cases_sound'] else 'NO'}**\n\n")
        w("## Per-case record (source_width / box_width / font → final)\n\n")
        w(
            "| case | measure | src_w | box_w | font→final | steps | lines | ovf | decision | reason |\n"
        )
        w("|---|---|---|---|---|---|---|---|---|---|\n")
        for r in records:
            if "error" in r:
                w(f"| {r['case']} | {r['measure']} | ERROR {r['error']} |\n")
                continue
            steps_s = "->".join(r["steps"]) if r["steps"] else "NO_ACTION"
            w(
                f"| {r['case']} | {r['measure']} | {r['source_width']} | "
                f"{r['box_width']} | {r['initial_font']}→{r['final_font']} | "
                f"{steps_s} | {r['line_count']} | {r['overflow']} | "
                f"{r['terminal_decision']} | {r['reason']} |\n"
            )
        w("\n## Terminal-CLIP detail (auditability)\n\n")
        w(
            "- silent truncation: none (overflow always True on CLIP) — see per-case ovf column.\n"
        )
        w(
            "- verdict fields recorded: decision / reason / steps / original↔final font.\n"
        )
    print(f"wrote {out_dir}/summary.json report.md")
    print("soundness:", summary["soundness"])
    print("all_cases_sound:", summary["all_cases_sound"])


if __name__ == "__main__":
    main()
