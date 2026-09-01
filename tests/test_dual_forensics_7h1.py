"""7H-1 Dual Fidelity Forensics — unit tests for the dual_forensics toolchain.

Covers the pure/thin logic (provenance ids, inspector anomaly + geometry,
snapshot on a tiny fixture, defect attribution & first-divergence stage) without
needing the large real-book corpus or any translator/ONNX.
"""

from __future__ import annotations

import json
import os
import tempfile

import pymupdf

from dual_forensics.diff import match_runs_to_blocks
from dual_forensics.defect import (
    F2,
    F4,
    DEFECTS,
    DefectFinding,
    classify_findings,
    run_defect_detectors,
)
from dual_forensics.pdf_inspector import content_stream_anomaly, inspect_page
from dual_forensics.provenance import node_id, stage_index
from dual_forensics.snapshot import capture_source_chain


def _tiny_pdf() -> str:
    """A 1-page A4 PDF with an English code-ish paragraph (pure text layer)."""
    doc = pymupdf.Document()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 200), "int f(int x) { return x + 1; } // code", fontsize=10)
    page.insert_text((72, 230), "This is ordinary body text to translate.", fontsize=10)
    path = os.path.join(tempfile.gettempdir(), "df_tiny.pdf")
    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


# ── provenance ────────────────────────────────────────────────────────────


def test_node_id_and_stage_index():
    assert node_id(77, 3) == "p77_3"
    assert stage_index("model") == 2
    assert stage_index("render") == 5


def test_defect_taxonomy_table():
    # The F1..F10 taxonomy is defined (identifiers stable).
    for fid in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10"):
        assert fid in DEFECTS, f"{fid} missing from taxonomy"
        assert "suspect" in DEFECTS[fid]


# ── pdf_inspector ─────────────────────────────────────────────────────────


def test_inspect_page_and_anomaly():
    doc = pymupdf.open(_tiny_pdf())
    try:
        r = inspect_page(doc, 0, doc[0].rect.height)
        assert r["page"] == 0
        joined = "".join(t["text"] for t in r["text_runs"])
        assert "int f" in joined and "body text" in joined
        # Every run got a v3_bbox (y-up) computed.
        for t in r["text_runs"]:
            assert len(t["v3_bbox"]) == 4
        cs = content_stream_anomaly(doc, 0)
        assert cs["checked"] is True
        assert cs["anomaly"] is False  # clean emitter
    finally:
        doc.close()


def test_match_runs_to_blocks():
    # Two source blocks; one run overlaps the second block entirely.
    rows = [
        {
            "node_id": "p0_0",
            "kind": "paragraph",
            "parser": {"bbox": [0, 0, 100, 50], "text": "a"},
            "translation": {"translated_text": "a", "translation_status": "translated"},
        },
        {
            "node_id": "p0_1",
            "kind": "code",
            "parser": {"bbox": [0, 50, 100, 100], "text": "int main(){}"},
            "translation": {
                "translated_text": "int main(){}",
                "translation_status": "preserved",
            },
        },
    ]
    runs = [{"text": "int main(){}", "v3_bbox": [1, 52, 90, 98]}]
    m = match_runs_to_blocks(runs, rows)
    assert m[0]["node_id"] == "p0_1"


# ── snapshot (pure fixture, identity translation) ─────────────────────────


def test_capture_source_chain_tiny():
    pdf = _tiny_pdf()
    r = capture_source_chain(pdf, page_ids=[0])
    assert not r["errors"]
    assert "0" in r["pages"]
    rows = r["pages"]["0"]
    assert rows, "no blocks parsed"
    ids = [x["node_id"] for x in rows]
    assert all(i.startswith("p0_") for i in ids)
    # model/translation/layout evidence present per block
    for ev in rows:
        assert (
            "parser" in ev and "model" in ev and "translation" in ev and "layout" in ev
        )


def test_snapshot_survives_bad_path():
    r = capture_source_chain(
        os.path.join(tempfile.gettempdir(), "nope.pdf"), page_ids=[0]
    )
    assert r["errors"], "expected a parse failure recorded, never an exception"


# ── defect attribution & first-divergence stage ───────────────────────────


def _traces_fixture():
    from dual_forensics.diff import Trace

    return [
        Trace(
            node_id="p185_14",
            page=185,
            kind="paragraph",
            source_text="namespace xyza {",
            translated_text="namespace xyza {",
            translation_status="translated",
            render_rows=[{"text": "命名空间 xyza {（误译）分区的代码"}],
            matched_present=True,
        ),
        Trace(
            node_id="p185_10",
            page=185,
            kind="formula",
            source_text="int f(int x) { return x+1; }",
            translated_text="int f(int x) { return x+1; }",
            translation_status="preserved",
            render_rows=[{"text": "\ufffd 损坏字形 (cid:5xxx)"}],
            matched_present=True,
        ),
    ]


def test_f2_code_translated_attrs_first_divergence_to_model_translation():
    findings = [f for f in run_defect_detectors(_traces_fixture()) if f.defect_id == F2]
    assert findings
    f2 = findings[0]
    # source was genuine code → the divergence first shows at translation (block
    # typed paragraph, so status translated) — we assert it is NOT renderer.
    assert f2.first_divergence != "render"
    # exactly one of model/translation is the first FAIL
    failed = [k for k, v in f2.stage_verdicts.items() if v == "FAIL"]
    assert failed in (["translation"], ["model"])


def test_f4_font_anomaly_attrs_render():
    findings = [f for f in run_defect_detectors(_traces_fixture()) if f.defect_id == F4]
    assert findings
    assert all(f.first_divergence == "render" for f in findings)


ALLOWED_FDS = {
    "source",
    "parser",
    "model",
    "translation",
    "layout",
    "packing",
    "render",
    "pdf",
    "unknown",
}


def test_every_defect_has_allowed_first_divergence():
    """7H-1 contract: a detected defect must carry an FDS from the allowed
    set — never a guessed/None divergence.  This is the invariant that turns
    diagnosis from "guess" into "evidence"."""
    findings = list(run_defect_detectors(_traces_fixture())) + [
        DefectFinding(
            defect_id=F2,
            node_id="p0_0",
            page=0,
            first_divergence="model",
            stage_verdicts={"model": "FAIL"},
        ),
    ]
    for f in findings:
        assert f.first_divergence in ALLOWED_FDS, (
            f"defect {f.defect_id} on {f.node_id} has invalid/guessed FDS "
            f"{f.first_divergence!r}"
        )
        # every FAIL stage must be ordered before any PASS in walk-order is
        # NOT required (a node may legitimately pass later), but the FDS must
        # equal the earliest FAIL stage for walk-consistency.
        failed = [k for k, v in f.stage_verdicts.items() if v == "FAIL"]
        if failed:
            earliest = min(
                failed,
                key=lambda s: (
                    [
                        "source",
                        "parser",
                        "model",
                        "translation",
                        "layout",
                        "packing",
                        "render",
                        "pdf",
                    ].index(s)
                    if s
                    in {
                        "source",
                        "parser",
                        "model",
                        "translation",
                        "layout",
                        "packing",
                        "render",
                        "pdf",
                    }
                    else 99
                ),
            )
            assert (
                f.first_divergence == earliest or f.first_divergence == "unknown"
            ), f"FDS {f.first_divergence!r} != earliest FAIL {earliest!r}"


def test_classify_findings_totals():
    findings = [
        DefectFinding(
            defect_id=F2,
            node_id="p0_0",
            page=0,
            stage_verdicts={"model": "FAIL"},
            first_divergence="model",
        ),
        DefectFinding(
            defect_id=F4,
            node_id="p0_1",
            page=0,
            stage_verdicts={"render": "FAIL"},
            first_divergence="render",
        ),
    ]
    cls = classify_findings(findings)
    assert cls[F2]["count"] == 1
    assert cls[F4]["by_first_divergence"]["render"] == 1


# ── report writer (tempdir) ───────────────────────────────────────────────


def test_renderer_provenance_id_direct():
    """7H-2A: renderer emits source_node_id → render_object_ref provenance,
    and the ID-direct diff resolves blocks to present (not geometry-UNCERTAIN)."""
    from dual_forensics.diff import aggregate_page_id_direct, load_provenance
    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

    plan = [
        {
            "block_id": "p0_0",
            "page": 0,
            "kind": "paragraph",
            "text": "Hello",
            "translated": "\u4f60\u597d",
            "src_box": [50, 700, 550, 720],
            "dst_box": [50, 700, 550, 720],
            "font_size": 12.0,
        },
        {
            "block_id": "p0_1",
            "page": 0,
            "kind": "formula",
            "text": "x = a + b",
            "translated": "x = a + b",
            "src_box": [200, 600, 400, 620],
            "dst_box": [200, 600, 400, 620],
            "font_size": 14.0,
        },
    ]
    pdf, stats = render_plan_to_pdf(plan, page_sizes={0: [612, 792]}, provenance=True)
    prov = stats["provenance"]
    assert len(prov) == 2
    nids = {r["source_node_id"] for r in prov}
    assert nids == {"p0_0", "p0_1"}
    assert all("render_object_ref" in r and "final_bbox_v3" in r for r in prov)

    rows = [
        {
            "node_id": "p0_0",
            "kind": "paragraph",
            "parser": {"bbox": [50, 700, 550, 720], "text": "Hello"},
            "translation": {
                "translated_text": "你好",
                "translation_status": "translated",
            },
        },
        {
            "node_id": "p0_1",
            "kind": "formula",
            "parser": {"bbox": [200, 600, 400, 620], "text": "x = a + b"},
            "translation": {
                "translated_text": "x = a + b",
                "translation_status": "preserved",
            },
        },
        # this block is NOT drawn → must be reported dangling (confirmed absent
        # by ID, not geometry-UNCERTAIN).
        {
            "node_id": "p0_9",
            "kind": "paragraph",
            "parser": {"bbox": [10, 10, 50, 20], "text": "absent block"},
            "translation": {
                "translated_text": "absent block",
                "translation_status": "translated",
            },
        },
    ]
    aggr = aggregate_page_id_direct(0, rows, load_provenance(prov))
    assert aggr["id_direct"] is True
    assert aggr["present_blocks"] == 2
    assert "p0_9" in aggr["dangling_blocks"]
    by_node = {t.node_id: t for t in aggr["traces"]}
    assert by_node["p0_0"].matched_present is True
    assert by_node["p0_9"].matched_present is False


def test_report_tree_writes_expected_files():
    from dual_forensics.report import build_summary, write_report_tree

    with tempfile.TemporaryDirectory() as d:
        pages = {
            77: {
                "source": [{"node_id": "p77_0"}],
                "model": [{"node_id": "p77_0", "kind": "paragraph"}],
                "translation": [{"node_id": "p77_0", "translated_text": "译"}],
                "layout": [{"node_id": "p77_0"}],
                "render": {"text_runs": [], "content_stream": {}},
                "diff": {"traces": [], "defects": []},
            }
        }
        summary = build_summary(
            [{"path": "x.pdf", "pages_analysed": [77]}],
            [
                DefectFinding(
                    defect_id=F2,
                    node_id="p77_0",
                    page=77,
                    first_divergence="model",
                    stage_verdicts={"model": "FAIL"},
                )
            ],
        )
        write_report_tree(d, pages, summary, {"source": "x.pdf", "dual": "y.pdf"})
        for name in ("manifest.json", "summary.json"):
            assert os.path.exists(os.path.join(d, name))
        pdir = os.path.join(d, "page-077")
        for name in (
            "source.json",
            "model.json",
            "translation.json",
            "layout.json",
            "render.json",
            "diff.json",
        ):
            assert os.path.exists(os.path.join(pdir, name)), f"missing {name}"
        with open(os.path.join(d, "summary.json"), encoding="utf-8") as fh:
            s = json.load(fh)
        assert s["defects"]["F2"]["total"] == 1
