# 7K-2 — Real-User Risk Audit

**Status: COMPLETE (evidence-only)**
Question: of the frozen known limitations, which affect actual user
deliverables, and how much risk is real vs theoretical?

---

## 1. Annotation not preserved — HIGH product impact when relied upon

**Finding (7K-1):** the pipeline deletes `/Annots` (BabelDOC `fix_null_xref`,
4 call sites) before the document model exists. Any highlight / underline /
comment / link in the source PDF is absent from mono and dual output.

**User-visible effect:** a user who translates an annotated PDF (marked-up
documents, links, reviewer comments) loses all annotations, silently — no
warning, no placeholder.

**True risk:** high *only if* annotation preservation is an expected contract.
For plain book translation (the current corpus) it never triggers. This is a
**product-contract gap**, must be stated in release notes, not silently
shipped as if unsupported.

**Mitigation available without new model work:** a pre-flight warning could
detect `/Annots` in the input and surface "annotations are not preserved".
(Not implemented — this is a product decision, not a forensic one.)

---

## 2. F4 × 1 (Multiprocessor p300) — LOW risk, correctly preserved

**The case** (`doc/7i2_f4_render_investigation.md` §2, `doc/7i3/`):

```text
p300_1 | heading | (cid:129) | GLBJJG+Times-Roman | bullet (•, Adobe StandardEncoding 0x81)
```

* Times-Roman has **no ToUnicode** → pdfminer per spec emits `(cid:129)`.
* The glyph is a list bullet; the recovery rule requires reliable
  font/glyph/encoding evidence and forbids guessing (`unknown ≠ best guess`),
  so it is **intentionally preserved as an explicit placeholder + parser anomaly**.

**User-visible effect:** on p300 of *The Art of Multiprocessor Programming 2e*,
one heading's text layer shows `(cid:129)` instead of `•`. It is a
source-PDF-encoding anomaly **existing before translation** (FDS=parser), not
introduced by the pipeline, and it is **explicitly visible** rather than a
silently wrong character.

**Risk rating: LOW.**
* Scope: 1 block / 1 page / 1 book in the corpus; none in 4 of 5 books.
* Correctness: an explicit placeholder beats a fabricated glyph
  (a wrong `Θ` is worse than an honest `(cid:3)` — the 7I-3 invariant).
* It doubles as the **negative control** proving detector attribution works
  (parser anomaly is not blamed on render/layout).

**Verification for release:** re-run `doc/7i4/full_corpus_baseline.py`
(or the 7J-4 gate) and confirm `total_residual == 1`, `by_first_divergence
== {"parser": 1}`.

---

## 3. F7 NOT_MEASURED — test-capability limitation, NOT a quality PASS

**Finding:** F7 (source-text leftover / duplication detection) is
NOT_MEASURED on 31/31 pages because the harness only has identity
translation, and identity can never distinguish "real translation". The
contract (7I-6B) freezes:

```text
identity translation under which translated_text == source_text must NEVER
be judged F7 FAIL — it is a corpus/methodology gate, not a detector gap.
```

**User-visible effect:** none — this is entirely an internal measurement
limitation. It does NOT say "translation may duplicate text"; it says "we
cannot yet measure it without a real source→translation→render triple".

**Risk rating: LOW (measurement only).** It becomes a reporting issue only if
someone reads NOT_MEASURED as PASS. The four-state contract (7I-4-1) exists
precisely to prevent that reading.

---

## 4. Summary

| item | risk to users | action |
|------|--------------|--------|
| Annotation dropped | **HIGH if relied upon** (silent loss) | release-note statement; optional pre-flight warning (product decision) |
| F4 × 1 | LOW (1 heading, explicit placeholder, source-originated) | keep as negative control; verified by gate |
| F7 NOT_MEASURED | none (measurement only) | document as harness boundary, never read as PASS |
| Everything else | none observed on pinned stack | regression gates on every dependency bump |

No finding in this audit justifies expanding the repair scope; all three are
bounded, explainable, and either preserved-by-design or gated by evidence.