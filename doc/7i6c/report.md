# 7I-6C — Gap Closure / Corpus Eligibility Assessment (milestone report)

**Status:** ✅ COMPLETE — evidence-only. **No production code changed.**

## What 7I-6C was

After 7I-6B wired F9/F10 into coverage (NOT_MEASURED → PASS 33/33), the only
open cells were **F5** (SKIP) and **F7** (NOT_MEASURED). Per the frozen plan,
7I-6C did **not** "find another detector" — it assessed whether either gap is
worth engineering, and if not, closed with an **observability boundary
established**.

## Findings

### F5 — representation gap, frozen

- The model **can** express figure blocks: `annotate_figures` (figure_understanding.py)
  creates `kind="figure"` blocks and is exported.
- It is **never called** — no caller anywhere in `pdf2zh/`; `build_document_model`
  emits zero figure/image blocks (no `LTFigure`/`LTImage` handling).
- Physical layer has drawings=142, images=10 on the corpus → the objects exist,
  the model just never classifies them. The capability is **dormant**, not missing.
- **Decision:** freeze as representation gap. Wiring `annotate_figures` into the
  model build is a *production semantic* decision (float preservation, render
  path) — not something to do to turn SKIP into PASS. F5 stays SKIP.

### F7 — contract frozen, gated on a real-translation harness

- **Real-translation artifacts exist**: `pdf2zh_files/` has dual+mono PDFs for 4
  books with genuine CJK translation (230/947/964/222 CJK pages respectively,
  ~874k CJK chars total). The corpus is **not** the gate.
- **The forensic harness cannot consume them today**: `capture_source_chain` is
  identity-only by design (offline, reproducible, no network). It never aligns
  the real dual/mono outputs into per-block source→translated→rendered triples.
- **Decision:** keep NOT_MEASURED. The unblock condition is a **harness
  ingestion path** (real-translation forensic mode), which is detector-era work,
  not an eligibility question. Identity translation remains degenerate for F7 and
  is locked as never-FAIL (`test_f7_identity_translation_is_not_fail`).

## Final matrix (frozen)

```
F1  PASS 33/33          F6  PASS/SKIP (caption-evidence)
F2  PASS/SKIP           F7  NOT_MEASURED (gated)
F3  PASS 33/33          F8  PASS 33/33 (7I-5C: 71→0)
F4  FAIL 1 @p300/parser F9  PASS 33/33 (wired)
F5  SKIP (representation)F10 PASS 33/33 (wired)
```

Residual: **F4 ×1** (source-PDF CID anomaly, intentionally preserved). All other
zeros are measured-clean or honest SKIP/NOT_MEASURED.

## Milestone status

```
7I-6A  Evidence Inventory        ✅ COMPLETE
7I-6B  F10/F9 wiring + F7 contract ✅ COMPLETE
7I-6C  Gap Closure / Eligibility ✅ COMPLETE
         └─ F5  representation gap, frozen (capability dormant, not missing)
         └─ F7  NOT_MEASURED, gated on real-translation harness ingestion
         └─ final F1–F10 matrix frozen (doc/7i6c/final_matrix.md)
7I-6   ⛔ COMPLETE — every F-id has a truthful, justified state
```

## Artifacts

- `doc/7i6c/eligibility_assessment.md` — data-grounded F5/F7 assessment
- `doc/7i6c/final_matrix.md` — frozen F1–F10 matrix + residual histogram
- `doc/7i6c/report.md` — this report
- `tests/test_eligibility_7i6c.py` — contract tests freezing the boundary

## What closes with 7I-6

> The fidelity system now has a complete, falsifiable measurement contract for
> F1–F10. Every cell is PASS / FAIL / SKIP / NOT_MEASURED with a reason. The two
> remaining gaps are precisely characterized with exact unblock conditions —
> **F5** needs a production decision (wire dormant figure capability) and **F7**
> needs a harness ingestion path (real-translation forensic mode). Neither is
> hidden, neither is fabricated into a zero.
