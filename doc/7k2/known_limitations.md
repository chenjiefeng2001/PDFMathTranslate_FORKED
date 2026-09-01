# 7K-2 — Known Limitations (Release Readiness)

**Status: COMPLETE (evidence-only · zero production changes)**
Baseline: corpus `doc/7i4-corpus-baseline/` (byte-identical on rerun) · release gate `doc/7j4/release_gate.py`.

## 1. Limitation inventory

Every item below states **what is true on the pinned stack** (babeldoc 0.6.4,
pymupdf 1.28.2, Python 3.13 — see `doc/7j4/dependencies.md`). `PASS/FAIL/
SKIP/NOT_MEASURED` follow the four-state detector contract
(`SKIP`/`NOT_MEASURED` ≠ clean).

| item | conclusion | evidence |
|------|-----------|----------|
| **F4** | **1 known residual** — preserved negative control | `doc/7i4-corpus-baseline/`, `doc/7i2_f4_render_investigation.md`, `doc/7i3/` |
| **F5** | SKIP 31/31 — **representation boundary** (physical drawings/images exist, document model has no float semantics) | `doc/7i4-corpus-baseline/report.md` §5 |
| **F7** | NOT_MEASURED 31/31 — **real-translation harness boundary** (identity translation can never validate it) | `doc/7i6/` eligibility assessment |
| **F8** | PASS 31/31 — 7I-5C re-WRAP fix: WRAP→SHRINK must re-WRAP; unbreakable→terminal CLIP is admissible | `doc/7i5/`, `doc/7i5_5d_unbreakable_residual.md` |
| **F9** | PASS 31/31 on corpus; **historical-artifact regression guard** active (Case A p3 NUL=60, Case B p157 NUL=1 still FAIL); both subclasses **not reproducible on pinned stack** → no production fix | `doc/7j1/`, `doc/7j2/`, `doc/7j3/`, `doc/7j3c/`, `doc/7j3d/` |
| **F10** | PASS 31/31 (provenance wiring, present/dangling/stray) | `doc/7i6/` |
| **XObject/Unicode** | Fixed (`None → -1` normalization, 7I-7C) + both historical failing books E2E-qualified | `doc/7i7/` |
| **Annotation** | **UNSUPPORTED / out of scope** — BabelDOC `fix_null_xref` deletes `/Annots` before IL; corpus 24→0 observed | `doc/7k1/` |
| **MuPDF / pymupdf** | No upgrade or modification needed (Case A/B first divergence is on generation side; both not reproducible on pinned stack) | `doc/7j2/`, `doc/7j3/` |
| **OCR** | No evidence to replace the model; scanned detection keeps working | — |
| **URL 专用断词** | Not introduced (no evidence) | `doc/7i5/` |
| **更低 font floor** | Not introduced (no evidence) | `doc/7i5/` |
| **renderer 特判** | Not introduced (no evidence) | `doc/7i5/`, `doc/7j/` |

## 2. What is NOT a limitation

* Text-layer integrity on the pinned stack: fresh mono 237 pages NUL=0,
  fresh dual footer `Taylor & Francis` extractable verbatim, CJK delta = 0.
* Layout: all wrapable content re-WRAPs through SHRINK (43/43 collapse cases
  fixed in 7I-5C); only genuinely unbreakable tokens may terminal-CLIP with a
  complete auditable verdict.
* Detector independence: p300 shows F4=FAIL@parser / F8=PASS / F6=PASS /
  F10=PASS simultaneously — no cascade.
* Regression contract: 7J-4 release gate green (95 latch tests + corpus
  matrix + historical capture + fresh smoke) — see `doc/7j4/gate_report.json`.

## 3. Upgrade discipline

Because 7J-3B/3C proved that *stack changes change whether defects appear*,
every dependency bump (babeldoc, pymupdf, fonts, OCR) MUST re-run:

```text
python tests/…           # latch tests (7i7/7j3a/7i4/7i6/7i5b/7i6c/7j3c …)
python doc/7j4/release_gate.py
python doc/7k1/evidence_matrix.py   # annotation boundary regression fixture
```

The known-limitations table above is **scoped to the pinned stack**; a bump
may move items between the three states (native / historical-artifact /
boundary), which is exactly what the gates are for.