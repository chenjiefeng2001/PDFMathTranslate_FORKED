# -*- coding: utf-8 -*-
"""7J-3D — dual-subclass qualification on the current pinned stack.

Consumes the outputs of ``doc/7j3c/reproduce_case_b.py`` (fresh mono/dual
from babeldoc 0.6.4 with a token-faithful translator) and verifies the
acceptance table:

  - Case B: ï/—/►/→ preserved (not NUL, not stripped)
  - token:   no <b1>/<b2> leak in the final text layer
  - NUL:     mono & dual NUL = 0
  - CJK:     cjk_delta (dual translated half vs mono) = 0
  - F9:      text-layer integrity sensor PASS on fresh, FAIL on the
             historical artifacts (regression guard still catches them)
  - negative control: specials are preserved, not "fixed away"

Writes ``doc/7j3d/qualification.json`` and prints a summary.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dual_forensics"))

from pdf_inspector import text_layer_integrity  # noqa: E402

WORK = ROOT / "doc" / "7j3c" / "work" / "out"
SPECIALS = {"\u00ef": "U+00EF ï", "\u2014": "U+2014 —", "\u25ba": "U+25BA ►", "\u2192": "U+2192 →"}
HISTORICAL = [
    ("AI mono p3  (Case A footer)", r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf", 2),
    ("AI mono p157 (Case B ►)", r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf", 156),
]


def load(path: Path) -> pymupdf.Document | None:
    if not path.exists():
        return None
    return pymupdf.open(str(path))


def main() -> int:
    results: dict = {"schema": "7j3d-qualification", "checks": {}}
    ok_all = True

    # ---- Case B fresh outputs ----
    mono = load(WORK / "case_b_input.no_watermark.zh.mono.pdf")
    dual = load(WORK / "case_b_input.no_watermark.zh.dual.pdf")
    if mono is None or dual is None:
        print("fresh outputs missing - run doc/7j3c/reproduce_case_b.py first")
        return 2

    mono_text = mono[0].get_text()
    dual_texts = [dual[i].get_text() for i in range(len(dual))]

    # specials preserved in mono (translated) and dual (source + translated)
    def specials_in(text: str) -> dict:
        return {hex(ord(ch)): ch in text for ch in SPECIALS}

    mono_specials = specials_in(mono_text)
    dual_specials = specials_in("".join(dual_texts))
    results["checks"]["case_b_specials_preserved"] = {
        "mono": mono_specials,
        "dual": dual_specials,
        "ok": all(mono_specials.values()) and all(dual_specials.values()),
    }
    ok_all &= results["checks"]["case_b_specials_preserved"]["ok"]

    nul_mono = mono_text.count("\x00")
    nul_dual = sum(t.count("\x00") for t in dual_texts)
    results["checks"]["nul_zero"] = {"mono": nul_mono, "dual": nul_dual, "ok": nul_mono == 0 and nul_dual == 0}
    ok_all &= results["checks"]["nul_zero"]["ok"]

    token_leak = re.findall(r"<b\d+>|</b\d+>", mono_text + "".join(dual_texts))
    results["checks"]["no_token_leak"] = {"leaks": token_leak[:5], "ok": not token_leak}
    ok_all &= results["checks"]["no_token_leak"]["ok"]

    # cjk_delta: dual translated half (last page in alternating mode) vs mono
    cjk_mono = sum(1 for ch in mono_text if "\u4e00" <= ch <= "\u9fff")
    cjk_dual_translated = sum(1 for ch in dual_texts[-1] if "\u4e00" <= ch <= "\u9fff")
    cjk_dual_source = sum(1 for ch in dual_texts[0] if "\u4e00" <= ch <= "\u9fff")
    delta = cjk_mono - cjk_dual_translated
    results["checks"]["cjk_delta"] = {
        "mono": cjk_mono,
        "dual_translated_half": cjk_dual_translated,
        "dual_source_half": cjk_dual_source,
        "delta": delta,
        "ok": delta == 0,
    }
    ok_all &= results["checks"]["cjk_delta"]["ok"]

    # detector on fresh (PASS expected)
    fresh_sensor = text_layer_integrity(mono, 0)
    results["checks"]["f9_fresh_pass"] = {
        "nul_chars": fresh_sensor["nul_chars"],
        "checked": fresh_sensor["checked"],
        "ok": fresh_sensor["checked"] and fresh_sensor["nul_chars"] == 0,
    }
    ok_all &= results["checks"]["f9_fresh_pass"]["ok"]

    # detector on historical artifacts (FAIL expected - regression guard alive)
    hist = {}
    for label, rel, pno in HISTORICAL:
        doc = load(ROOT / rel)
        s = text_layer_integrity(doc, pno)
        hist[label] = {"nul_chars": s["nul_chars"], "detected": s["nul_chars"] > 0}
    results["checks"]["f9_historical_captured"] = {
        "pages": hist,
        "ok": all(v["detected"] for v in hist.values()),
    }
    ok_all &= results["checks"]["f9_historical_captured"]["ok"]

    # ---- Case A fresh (cross-referenced from 7J-3B E2E) ----
    results["checks"]["case_a_fresh"] = {
        "evidence": "doc/7j3/report_7j3b.md (fresh AI full-book E2E: NUL=0 over 237 pages, "
        "footer 'Taylor & Francis'/'Group'/'http://taylorandfrancis.com' extractable on mono p3 and dual p5)",
        "ok": True,
    }

    results["all_ok"] = ok_all
    out = ROOT / "doc" / "7j3d" / "qualification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(results, indent=1, ensure_ascii=False))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
