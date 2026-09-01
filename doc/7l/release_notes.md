# Release Notes — pdf2zh 1.9.15 RC (forensic-qualified)

> Draft for the release owner. This RC is qualified on the **pinned stack**
> `babeldoc 0.6.4 · pymupdf 1.28.2 · Python 3.13` via the 7I–7K forensic
> chain and the release gate (`python doc/7j4/release_gate.py --smoke`).

## 1. What this release delivers (qualified)

* **XObject/Unicode crash fixed** — `Matrix Algebra (Abadir/Magnus)` and
  `Groups and Symmetries 2nd ed.` translated end-to-end again
  (`Xobj id must be provided when unicode is provided` eliminated via
  `None → -1` normalization; real `0/1/7/-1` untouched).
* **Layout overflow contract** — WRAP→SHRINK now re-WRAPs instead of
  collapsing wrapped text into a squeezed single line; only genuinely
  unbreakable tokens may terminal-clip, always with an explicit `overflow`
  verdict. Corpus clip defects: 71 → 0.
* **CJK text-layer integrity sensor** — text layers containing NUL/mojibake
  are detected as FAIL instead of silently passing (historical artifacts
  captured; fresh outputs clean, `cjk_delta = 0`).
* **Forensic observability** — F1–F10 now measured with an honest
  PASS/FAIL/SKIP/NOT_MEASURED contract; the single measured residual is
  documented below (F4 × 1).

## 2. KNOWN LIMITATIONS — read before shipping

### 2.1 PDF annotations are NOT preserved (most important)

**Highlights, underlines, comments, and links in your source PDF are not
carried into the translated output.** The pipeline deliberately strips
`/Annots` during preprocessing (before the document model exists).

* If you rely on your PDF's markups/links, **keep the original PDF** —
  the translation output will not contain them.
* This is **not** a MuPDF/OCR issue and not a regression in this release;
  it is a documented, deliberate limitation.
* If annotation preservation becomes a product requirement, the standing
  regression corpus already exists
  (`doc/7k1/annotation_corpus.{pdf,json}`, 24 cases) to gate that work.

### 2.2 Character placeholders can appear instead of exotic glyphs

One known residual in the corpus: on page 300 of *The Art of Multiprocessor
Programming 2e*, a list bullet is emitted as `(cid:129)` in the text layer
because the source font carries no ToUnicode mapping. We intentionally
preserve the explicit placeholder instead of guessing a replacement —
**an honest `(cid:129)` beats a fabricated `•`.** Scope: 1 page, 1 heading,
source-PDF-originated.

### 2.3 Measurement boundaries (not defects, not fixed)

* **F5 (figure/table layout fidelity)**: currently SKIP — the document
  model has no figure/table/image semantic blocks. Not measured as clean;
  simply not measurable yet.
* **F7 (untranslated-text residue)**: currently NOT_MEASURED — requires a
  real source→translation→render triple to validate, which the offline
  harness does not have.

**Do not claim these are "fixed" or "passing" in any release material.**
They are scoped boundaries with recorded unlock conditions.

## 3. Upgrade discipline

7J-3 proved that *stack changes change whether defects appear*. After any
dependency bump (babeldoc / pymupdf / Python / OCR / fonts), re-run:

```text
python doc/7j4/release_gate.py --smoke
```

and compare against `doc/7j4/gate_report.json`. The known limitations above
are scoped to the pinned stack, not to "all future versions".

## 4. Verification performed for this RC

| check | result |
|-------|--------|
| Full test suite | passed (see 7L-1 record) |
| Release gate `--smoke` | green (95 latch tests · corpus residual=1@parser · historical capture · fresh smoke NUL=0) |
| Historical failing books E2E | both complete (7I-7C qualified) |
| Corpus determinism | baseline rerun byte-identical across runs |
| Worktree | clean at qualification point |
## 5. 7L-1 RC verification record

Performed 2026-09-01 on the pinned stack:

| item | result |
|------|--------|
| worktree | clean before qualification |
| full test suite | 3995 passed · 3 skipped · 1 env-flake (`test_submit_task`, passes in isolation — known, untouched path) |
| release gate `--smoke` | see `doc/7j4/gate_report.json` (refreshed by 7L-3) |
| historical failing books | Matrix Algebra: dual 932p / mono 466p, complete, no XObject assert (7I-7C reaffirmed); Groups and Symmetries 2e: dual 532p / mono 266p, complete, both fully extractable, no XObject assert |
