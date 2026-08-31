# 7I-6C — Gap Closure / Corpus Eligibility Assessment

**Status:** ✅ COMPLETE — evidence-only. No production code, no detector, no
document-model change, no renderer change. Discipline per 7I-4/7I-6A: assess
**eligibility** before deciding whether to engineer a fix.

## Scope

Per the frozen 7I-6 plan, the only two open cells in the coverage matrix are:

| Defect | 7I-6B state | Open question |
|---|---|---|
| **F5** | SKIP (representation gap) | Is there a corpus that produces real float blocks? If not → freeze the gap. |
| **F7** | NOT_MEASURED (contract frozen) | Does a real-translation corpus with 3-stage evidence exist? If yes → detector eligible; if no → keep gated. |

---

## F5 — Figure/table/image spatial semantics

### Question
> Physical PDF has drawings (142) and images (10) on the corpus, but the
> document model emits **0** float blocks on 31/31 pages. Is this a missing
> *capability* (model cannot express figures) or a *dormant* capability (model
> can, but the build path never invokes it)?

### Evidence

1. **The model *can* express figure blocks.** `pdf2zh/v3/figure_understanding.py`
   defines `annotate_figures(image_records, page)` which appends blocks with
   `kind="figure"` (plus per-figure glyph evidence). It is exported from
   `pdf2zh/v3/__init__.py` (line 687, 1336).

2. **The capability is never wired into the build path.** `grep annotate_figures`
   across `pdf2zh/` shows only the definition, `__all__`, and `__init__`
   re-exports — **no caller**. `build_document_model` / `build_page_model`
   (`document_model.py`) contains **zero** occurrences of `kind="figure"`,
   `kind="image"`, `LTFigure`, or `LTImage`. The classifier's `type_map` never
   emits float kinds; "figure"/"image"/"table" appear only as literals in
   `annotate_render`'s `preserve_float` render-advice set.

3. **Physical layer confirms float content exists to be missed.** The corpus
   physical probe (7I-6A matrix) found `drawings=142, images=10` across the
   sampled pages — the objects are in the PDF, the model just never classifies
   them.

4. **Forensic path inherits the same gap.** `capture_source_chain` →
   `build_document_model` — no figure annotation anywhere in the forensic
   snapshot.

### Verdict

**F5 = representation gap, frozen.** The *detector* is implemented and correct
(`_detect_f5_detached_page`); the *model capability* exists but is dormant
(`annotate_figures` is never called). There is currently **no corpus page** that
produces a float semantic block through the model build path, so there is nothing
for the detector to measure.

Per 7I-4/7I-5 discipline: we do **not** wire `annotate_figures` into
`build_document_model` just to turn F5 into PASS. That is a **production model
change** with real semantic consequences (float preservation, render path), and
it should only happen when float fidelity is an actual goal — not to satisfy a
scorecard. F5 stays **SKIP** until real float evidence is needed.

**Boundary:** F5 = SKIP is the *correct* state. It is a representation gap, not
a detector gap and not a defect.

---

## F7 — Source text leftover / duplication

### Question
> Does the existing corpus provide **source → translated → rendered** three-stage
> comparable evidence, so the frozen F7 contract can be executed against real
> (non-identity) translation?

### Evidence

1. **Real-translation artifacts exist.** `pdf2zh_files/` contains `*-dual.pdf`
   and `*-mono.pdf` outputs for 4 books. A PyMuPDF probe over all pages finds
   genuine CJK translation:
   - `AI for Games and Animation` mono: 230/237 pages CJK, ~92,760 CJK chars.
   - `Game Physics` mono: 947/959 pages CJK, ~301,951 CJK chars.
   - `Large-Scale C` mono: 964/1023 pages CJK, ~365,445 CJK chars.
   - `Networking` mono: 222/223 pages CJK, ~114,365 CJK chars.
   The dual PDFs contain both the original Latin text and the translated CJK on
   the same double-width page (e.g. AI dual p7: latin=95, cjk=24).

2. **But the forensic harness cannot consume them today.** `capture_source_chain`
   (the only forensic snapshot entry used by the corpus scan) is **identity-only
   by design** — its docstring states this explicitly: it builds
   parser→model→translation→layout with the `identity` translator so analysis is
   reproducible offline and needs no network. It parses a **source** PDF, builds
   the model, translates with `identity`, plans, and emits per-block
   `source_text`/`translated_text` (which are therefore equal).

3. **Therefore no per-block 3-stage triple exists in the pipeline for real
   translation.** The dual/mono artifacts are *outputs* of a real translation
   run, but nothing in `dual_forensics` ingests them and aligns per-block
   source↔translated↔rendered evidence. The gap is a **harness ingestion
   capability**, not corpus availability.

### Verdict

**F7 stays NOT_MEASURED — contract frozen, gated on a real-translation forensic
harness.** The 7I-6B frozen contract stands unchanged:

```
PASS  real translation evidence exists + rendered text has no source residue
FAIL  real translation evidence exists + source residue/duplication identified
SKIP  translation evidence insufficient / identity translation
NOT_MEASURED  detector contract not wired            ← current
INVARIANT: identity (translated == source) is NEVER an F7 FAIL
```

The eligibility assessment sharpens the *reason* for the gate: it is not "no
real translation corpus exists" (it does — 4 books, thousands of CJK pages), but
"the forensic snapshot harness has no ingestion path that turns those artifacts
into aligned per-block source→translated→rendered triples". Building that
ingestion path **is** the F7 detector-era work; it is not an eligibility
question. Under identity translation the detector remains degenerate and must
not run (locked by `test_f7_identity_translation_is_not_fail`).

---

## Bottom line

- **F5**: capability exists, dormant; no eligible float corpus → **freeze as
  representation gap**. Do not engineer a weak figure model.
- **F7**: eligible artifacts exist but the forensic harness cannot yet produce
  3-stage per-block evidence → **keep gated**. The gate is harness ingestion,
  not corpus availability.

7I-6C closes with **"observability boundary established"**: every F1–F10 cell now
has a truthful, justified state, and the two remaining gaps are precisely
characterized with their exact unblock conditions.
