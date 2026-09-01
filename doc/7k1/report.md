# 7K-1 — Annotation Preservation Evidence Inventory

**Status: COMPLETE (evidence-only, zero production changes)**

**Declared boundary: Annotation preservation = UNSUPPORTED / out of scope.**
This is a *requirements boundary* recorded by the pipeline, not a defect the
forensic system failed to detect. The corpus below stays as the standing
regression fixture for any future unlock (see section 6).

## 1. Question

Where do PDF annotations (highlight / underline / link / text) disappear in the
current pipeline — parser, document model, translation, or emitter?

## 2. Corpus inventory (7K-1A)

The 5-book residual corpus contains **zero annotations** (`pymupdf` scan of all
5 source PDFs: 0 annots across 3326 pages). Like F5 figures, the existing corpus
cannot exercise annotations, so a dedicated synthetic corpus was built:

`doc/7k1/annotation_corpus.pdf` — 6 pages, **24 cases**:

| type        | count | pages |
|-------------|-------|-------|
| highlight   | 9     | 1,2,3,4,5 |
| underline   | 6     | 1,2,3,4,5 |
| text/comment| 4     | 1,2,4,5 |
| link (URI)  | 3     | 1,2,5 |
| link (GOTO) | 1     | 3 |
| none (ctrl) | 1     | 6 |

Coverage: multi-annotation same page (p2, p3), cross-line highlight (p3),
CJK text (p2), Latin text (p1), mixed CJK+Latin (p2), figure-overlap (p4),
symbols (p5), cross-page internal link (p3→p1), negative control (p6).
Each case has an expected contract in `annotation_corpus.json`.

## 3. Pipeline visibility (7K-1B)

| layer | annotation support |
|-------|--------------------|
| pymupdf / MuPDF (measurement) | reads `/Annots` fine (used for all counts above) |
| pdfminer | `PDFPage.annots` parsed (pdfminer/pdfpage.py:76), but **pdf2zh never reads it** (0 matches) |
| BabelDOC document IL | **no annotation concept** (no class, no schema node) |
| BabelDOC backend (IL→PDF) | **never writes `/Annots`** (0 matches in pdf_creater.py) |
| **BabelDOC preprocessing** | **actively deletes it**: `fix_null_xref` (high_level.py:453-477) nulls every xref object containing `/Annots` |

`fix_null_xref` runs unconditionally in 4 production sites:
`high_level.py:747` (migrate_toc), `:858` (debug path), `:873` (main
`do_translate` path), and `parse_shared.py:60` (pdf2zh parse path). The
split-part path additionally nulls `/Annots` explicitly before `insert_pdf`
(high_level.py:620-634).

## 4. First-divergence matrix (7K-1C)

Observed by applying the **exact production function** `fix_null_xref` to the
corpus, then confirmed by a **full offline E2E** (babeldoc 0.6.4, stub-CJK
translator, same harness as 7J-3B/C):

```
stage                    annots+links
source (corpus PDF)      24
preprocess (fix_null_xref) 0      ← FIRST DIVERGENCE
output mono (real E2E)   0
output dual (real E2E)   0
```

Per-case verdict (`evidence_matrix.json`): **23/23 real cases diverge at
`preprocess`**; the only `source` divergence is F00, the negative control.
`first_divergence` = preprocess for every real annotation.

## 5. Conclusion — representation boundary (deliberate policy)

Annotations are not lost by the parser (MuPDF/pdfminer see them), not by the
IL schema, and not by the emitter. They are **deliberately stripped in
BabelDOC's input preprocessing** (`fix_null_xref`), before the document model
exists — for every page, unconditionally, in every translation path.

This is a **representation boundary by policy**, same family as F5, but
stronger: F5 is "model cannot express figures"; here the pipeline *actively
deletes* the object before the model is built.

Per the 7K decision tree:

```
source annotation exists (verified)
    ↓
pipeline preprocessing deletes /Annots (fix_null_xref, 4 call sites)
    ↓
IL / model / translation / emitter have no annotation path
    ↓
output PDF has no annotations (observed E2E)
    ↓
→ representation boundary, NOT a detector or emitter defect
→ FROZEN: no production change in 7K-1
```

## 6. Frozen contract & unlock conditions

Annotation preservation is currently **out of scope** (like F5). It becomes a
repair milestone **only** when a product requirement demands it (comments /
highlights / links must survive translation). The unlock conditions:

* BabelDOC must stop nulling `/Annots` (upstream change or pdf2zh-side shim
  with a preservation contract), **or** annotations must be carried in a
  translation-aware form (re-targeted to translated layout geometry);
* a preservation contract must define: present / type / page / target /
  geometry tolerance / appearance;
* `annotation_corpus.pdf` + `annotation_corpus.json` are the standing
  regression corpus for that contract — upgrade of babeldoc / pymupdf / font
  stack can be validated against it.

## 7. Artifacts

```
doc/7k1/build_corpus.py       corpus generator (24 cases, 6 pages)
doc/7k1/annotation_corpus.pdf synthetic annotated PDF
doc/7k1/annotation_corpus.json per-case expected contracts + source inventory
doc/7k1/evidence_matrix.py    matrix builder
doc/7k1/evidence_matrix.json  stage verdicts (source/preprocess/output)
doc/7k1/e2e_corpus.py         offline E2E harness (stub translator)
doc/7k1/report.md             this report
```

Zero production code changed. flake8-clean (scripts use only stdlib + pymupdf).
