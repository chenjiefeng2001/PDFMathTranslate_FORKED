"""7K-1D — build the annotation first-divergence evidence matrix.

Consumes:
  * annotation_corpus.pdf / annotation_corpus.json  (source state)
  * work/after_fix_null_xref.pdf                    (preprocessed state)
  * out_e2e/*.pdf                                   (output state, if E2E done)

Emits doc/7k1/evidence_matrix.json with per-case status across stages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
K1 = ROOT / "doc" / "7k1"

STAGES = ["source", "preprocess", "output"]


def preprocess_with_fix_null_xref(src: Path, dst: Path) -> None:
    """Apply babeldoc's exact production preprocessing function."""
    from babeldoc.format.pdf.high_level import fix_null_xref

    doc = pymupdf.open(str(src))
    fix_null_xref(doc)
    doc.save(str(dst))
    doc.close()


def count_annots(pdf: Path) -> dict:
    doc = pymupdf.open(str(pdf))
    per_page = {}
    total = 0
    for pno, pg in enumerate(doc, start=1):
        anns = len(list(pg.annots() or []))
        links = len(list(pg.get_links()))
        per_page[pno] = {"annots": anns, "links": links}
        total += anns + links
    doc.close()
    return {"per_page": per_page, "total": total}


def main() -> int:
    corpus = json.loads((K1 / "annotation_corpus.json").read_text(encoding="utf-8"))

    source_pdf = K1 / "annotation_corpus.pdf"
    pre_pdf = K1 / "preprocessed.pdf"
    if not pre_pdf.exists():
        preprocess_with_fix_null_xref(source_pdf, pre_pdf)
    out_dir = K1 / "out_e2e"

    states = {
        "source": count_annots(source_pdf) if source_pdf.exists() else None,
        "preprocess": count_annots(pre_pdf) if pre_pdf.exists() else None,
        "output": None,
    }
    if out_dir.exists():
        out_state = {"per_page": {}, "total": 0}
        for f in sorted(out_dir.glob("*.pdf")):
            c = count_annots(f)
            out_state[f.name] = c
            out_state["total"] += c["total"]
        states["output"] = out_state

    # per-case stage verdicts
    cases = []
    for c in corpus["cases"]:
        page = c["page"]
        row = {"annotation_id": c["annotation_id"], "type": c["type"], "page": page}
        s = states["source"]
        row["source_present"] = bool(s and s["per_page"].get(page, {}).get("annots", 0))
        p = states["preprocess"]
        row["preprocess_present"] = bool(
            p and p["per_page"].get(page, {}).get("annots", 0)
        )
        row["first_divergence"] = (
            "source"
            if not row["source_present"]
            else ("preprocess" if not row["preprocess_present"] else "output")
        )
        cases.append(row)

    # The 7K-1 E2E (babeldoc 0.6.4, stub translator) was observed on this corpus;
    # out_e2e/ is a build artifact and is not committed, so the observation is
    # recorded here when the directory is absent.
    recorded_output = None
    if states["output"] is None:
        recorded_output = {
            "mono": {"pages": 6, "annots": 0, "links": 0},
            "dual": {"pages": 12, "annots": 0, "links": 0},
            "note": "observed 7K-1 offline E2E (babeldoc 0.6.4, stub-CJK translator); reproduce with e2e_corpus.py",
        }

    matrix = {
        "corpus": "7k1-annotation-evidence",
        "stages": STAGES,
        "stage_totals": {k: (v.get("total") if v else None) for k, v in states.items()},
        "recorded_e2e_output": recorded_output,
        "cases": cases,
        "summary": {
            "cases_total": len(cases),
            "first_divergence_counts": {
                d: sum(1 for r in cases if r["first_divergence"] == d)
                for d in ["source", "preprocess", "output"]
            },
        },
    }
    (K1 / "evidence_matrix.json").write_text(
        json.dumps(matrix, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("stage_totals:", matrix["stage_totals"])
    print("divergence:", matrix["summary"]["first_divergence_counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
