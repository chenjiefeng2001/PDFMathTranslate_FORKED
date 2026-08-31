# 7I-6A — Evidence Inventory Report

**Status:** ✅ COMPLETE — evidence-only; **no production code, no detector,
no document-model change**.  Discipline per 7I-4: inventory before implementation.

## What this milestone established

For F5 / F7 / F9 / F10, we determined **what** must be measured, **which
stage's snapshot** first and reliably exposes it, **whether that evidence
currently exists** in `dual_forensics`, and — the critical part — **what kind of
gap** remains.  Each defect now has an honest `PASS/FAIL/SKIP/NOT_MEASURED`
state with a justification.  Data: `doc/7i6/evidence_matrix.json` (31 corpus
pages sampled).

## The four states

| Defect | Object | Authoritative evidence | In snapshot? | State | Gap type |
|---:|---|---|---|---|:---:|
| **F5** | float↔text spatial | `figure/table/image` model block + nearby text | detector ✅ / float object ❌ | **SKIP** | **representation** |
| **F7** | source leftover/duplicate | source vs translated vs rendered cross-compare | ⚠️ text yes, sensor no | **NOT_MEASURED** | **detector + methodology** |
| **F9** | text↔visual mismatch | `content_stream_anomaly()` (MuPDF malformed-float) | ✅ signal exists, not wired | **NOT_MEASURED** | **wiring** |
| **F10** | object lost/drifted/stray | ID-direct `present/dangling/stray` | ✅ signal exists, not wired | **NOT_MEASURED** | **wiring** |

## Key findings (why each is the gap it is — no hand-waving)

**F5 = representation gap.** `_detect_f5_detached_page` is implemented and
correct, but the corpus (31/31 pages) yields **0** `figure/table/image` model
blocks despite physical drawings(142)+images(10).  There is no float object to
measure → SKIP is the *true* result.  Per 7I-4/7I-5 discipline, we do **not**
inject a weak/indirect figure model to turn it into PASS.  Real figure evidence
would measure it; until then it is an honest gap.  **No model change.**

**F7 = detector gap + methodology limit.** Under the project's **identity
translation** corpus, `translated_text == source_text` by construction.  Any
"rendered == source → leftover" heuristic would trivially fire on **every**
block.  So F7's discriminating signal requires a **real (non-identity)
translation corpus** to have dynamic range.  The sensor fields exist on Trace,
but running a detector now would be a null test.  We freeze the F7 **contract**
(what "leftover/duplicate" means, cross-stage) and gate implementation on a
real-translation sample — do not prototype on identity.

**F9 = wiring gap.** `pdf_inspector.content_stream_anomaly()` (7H-2B emitter
malformed-float sensor) is fully implemented and appears in `__main__.py`, but
`coverage_page`/`run_defect_detectors` never see it → NOT_MEASURED.  7I-6B's fix
is pure wiring: expose it as a page-level F9 detector.  Corpus result would be
**PASS** (0 in-pipeline anomaly pages; the emitter has been clean since 7H-2B).

**F10 = wiring gap.** `aggregate_page_id_direct` already computes
`present_blocks` / `dangling_blocks` / `stray_records` from
`source_node_id → render_object_ref`, but it is surfaced only in scans, not in
the coverage contract → NOT_MEASURED.  Corpus = 0 dangling / 0 stray.
7I-6B's fix is wiring: a page-level F10 detector over that summary (FAIL on any
dangling/stray, PASS if all present, SKIP only if no provenance ran).  A block
never owed by the plan must stay **SKIP**, not PASS.

## Recommendation for 7I-6B (evidence-driven order)

| # | Defect | Action | Why now |
|---|---|---|---|
| 1 | **F10** | wire `present/dangling/stray` into coverage | evidence already computed; corpus PASS |
| 2 | **F9** | wire `content_stream_anomaly` into coverage | sensor exists; corpus PASS |
| 3 | **F7** | freeze **contract only**, gate on real-translation corpus | detector would be null under identity |
| 4 | **F5** | leave as representation gap; re-open on float evidence | no object to measure |

## Honest-states principle preserved

`SKIP` ≠ `0` (F5: nothing measured) and `NOT_MEASURED` ≠ `0` (F7/F9/F10: not yet
evaluable).  No defect was flipped to PASS or 0 to beautify the scorecard.  This
milestone's deliverable is the *evidence*, the *gap classification*, and the
*wiring plan* — not a premature detector.

## Artifacts

- `doc/7i6/evidence_inventory.md` — per-defect fieldwork / contract
- `doc/7i6/evidence_matrix.json` — data-backed matrix (31 pages)
- `doc/7i6/report.md` — this report
- `tests/test_evidence_inventory_7i6a.py` — inventory contract tests (freeze
  the honest states + gap classification; confirm F9/F10 sensors exist)