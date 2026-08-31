# 7I-6A — Evidence Inventory: F5 / F7 / F9 / F10

**Status:** ✅ COMPLETE — evidence-only.  **No production code or detector
changed** (7I-4 discipline: Evidence Inventory first).

Goal: for each of F5 / F7 / F9 / F10, determine exactly what must be measured,
which stage's snapshot can first reliably see it, whether that snapshot
currently exists in `dual_forensics`, and — critically — whether any gap is a
*missing detector*, a *missing wiring*, a *representation gap*, or a
*methodology limit*.  The output is an honest `PASS / FAIL / SKIP /
NOT_MEASURED` state per defect with a justification, NOT a forced detector
implementation.

## Method

Read the taxonomy (`dual_forensics/defect.py`), the diff/provenance machinery
(`diff.py`, `provenance.py`), the render inspector (`pdf_inspector.py`), the
CLI wiring (`__main__.py`), the corpus scans (`doc/7i4/*`), and the historical
7H reports — then **probe the actual signals** in-pipeline.  No `pdf2zh/`
production change; no detector written.

---

## F5 — figure/table detached from text

| Field | Value |
|---|---|
| Object to measure | spatial semantics: a figure/table/image block vs the surrounding text blocks on its page |
| First reliable stage | **model / layout** (float kind + geometry + caption/host relation) |
| Authoritative evidence | a `figure/table/image`-kind block with a drawn box **and** >=1 text block nearby |
| In snapshot? | **detector**: ✅ `_detect_f5_detached_page`; **subject object**: ❌ no float model block exists |
| Current state | **SKIP** (detector runs; no float object to measure, so nothing evaluated) |
| Gap type | **representation gap** |

**Evidence (corpus, 7I-4-4 §5):** 31/31 sample pages have **no** model
`figure/table/image` block, despite the physical layer carrying drawings=142 /
images=10.  The document model never produces a float semantic block from this
corpus, so F5 can genuinely measure nothing → `SKIP`, never a clean `0`.

> Decision: **do not** insert a weak/indirect figure model just to turn F5 into
> PASS.  If real figure evidence appears (a PDF whose model yields float
> blocks), F5 measures it.  Otherwise F5 remains an honest representation gap.

---

## F7 — source text leftover / duplicate

| Field | Value |
|---|---|
| Object to measure | a block whose rendered output still carries **source-language** text (leftover) or duplicates it, instead of only the translation |
| Suspect (taxonomy) | translation / segmentation |
| First reliable stage | **translation → render** (needs translated_text + rendered_text cross-compare) |
| Authoritative evidence | `source_text` vs `translated_text` vs `rendered_text`; whether the block renders source where translation was owed |
| In snapshot? | text signals ⚠️ partial (source/translation/rendered all on Trace), but no detector |
| Current state | **NOT_MEASURED** |
| Gap type | **detector gap + methodology limit** |

**Why it is more than "write a detector":** under this project's corpus method
(**identity translation**), `translated_text == source_text` by construction.
A detector that flags "rendered text == source text" would fire on *every*
block and be meaningless.  So F7's "leftover/duplicate" discrimination requires
a **real (non-identity) translated corpus** to be measurable — the evidence
sensor exists on the Trace, but the corpus lowers its discriminative power to
zero.  An honest F7 therefore stays **NOT_MEASURED** until:
1. a detector contract is frozen (what "leftover" means given source/trans/render
   stages), AND
2. a real-translation corpus is sampled so the signal has dynamic range.

> Do not prototype an F7 detector against the identity corpus — it would be a
> null test.  This is a methodology limit, not a missing import.

---

## F9 — text layer vs visual layer mismatch

| Field | Value |
|---|---|
| Object to measure | the rendered **text layer** disagrees with the **visual layer** (e.g. malformed emitter tokens the text extractor hides) |
| First reliable stage | **render / pdf** (object layer) |
| Authoritative evidence | `pdf_inspector.content_stream_anomaly()` — MuPDF emitter malformed-float detection (`-9.0e` → bare mantissa+sig) |
| In snapshot? | ✅ **signal exists** (`content_stream_anomaly`); only faked into `__main__.py`, **not** into `coverage_page` |
| Current state | **NOT_MEASURED** |
| Gap type | **wiring gap** (evidence present; not exposed to the detector contract) |

**Evidence (7H-2B):** the malformed-float emitter defect was real in the
external dual (`F9:10`); the in-pipeline emitter is clean (`F9-in-pipeline: 0`).
The detector *sensor* is fully implemented and called in `__main__.py`, but
`coverage_page`/`run_defect_detectors` never see it, so it reads
`NOT_MEASURED`.  **Closing the gap is wiring only** — thread
`content_stream_anomaly()` as a page-level F9 detector into the coverage
contract (7I-6B).

---

## F10 — XObject / draw object lost or drifted

| Field | Value |
|---|---|
| Object to measure | a model block lost (absent) / drifted (moved) in render, or stray render objects with no model backing |
| First reliable stage | **render** |
| Authoritative evidence | ID-direct provenance: `aggregate_page_id_direct` → `present_blocks` / `dangling_blocks` / `stray_records` |
| In snapshot? | ✅ **signal exists** (`aggregate_page_id_direct`); only surfaced in scans, **not** in `coverage_page` |
| Current state | **NOT_MEASURED** |
| Gap type | **wiring gap** |

**Evidence:** `aggregate_page_id_direct` already classifies
present / absent(dangling) / stray using `source_node_id → render_object_ref`.
The corpus baseline shows dangling=0 / stray=0 (all blocks present).  Closing
F10 = a page-level detector wired on that output: `FAIL` if any
`dangling_blocks`/`stray_records`, `PASS` if all present, `SKIP` only when no
provenance ran for the page.  A block absent from the plan (never owed) must
stay **SKIP**, not PASS.

---

## Summary table

| Defect | Object | Auth. evidence | In snapshot? | State now | Gap type |
|---:|---|---|---|:---:|:---:|
| F5 | float↔text spatial | float block + text block | detector ✅ / float object ❌ | SKIP | **representation** |
| F7 | source leftover/duplicate | src vs trans vs rendered | ⚠️ text yes, sensor no | NOT_MEASURED | **detector + methodology** |
| F9 | text↔visual mismatch | `content_stream_anomaly` | ✅ signal exists | NOT_MEASURED | **wiring** |
| F10 | object lost/drifted/stray | ID-direct prov summary | ✅ signal exists | NOT_MEASURED | **wiring** |

## Recommendation for 7I-6B

Order by gap-type (cheap, high-signal first) and **only** act where evidence
exists:

1. **F10 (wiring)** — page-level detector over `present/dangling/stray`; PASS on
   corpus (dangling=0).  Lowest risk, evidence already computed.
2. **F9 (wiring)** — page-level detector over `content_stream_anomaly`; PASS
   on corpus (F9-in-pipeline=0 since 7H-2B).
3. **F7** — freeze the detector **contract** only; do NOT run it on identity.
   Mark gate: needs a real-translation corpus.
4. **F5** — leave as representation gap; no model change.  Re-open only when a
   float block appears.

Everything above is evidence-only.  `doc/7i6/evidence_matrix.json` + `report.md`
carry the data backing this inventory.