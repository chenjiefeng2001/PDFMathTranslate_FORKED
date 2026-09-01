# 7J-1 — Real-Translation E2E Residual Scan

**Status: COMPLETE** — evidence-only. No production code changed. Uses the
real translated artifacts in `pdf2zh_files/` (source / dual / mono per book)
to answer: *does real translation into the production pipeline produce
divergence the 7I F1–F10 matrix does not cover?*

## 1. Corpus (real translation, not synthetic)

| book | src | dual | mono | CJK chars (mono) |
|---|---:|---:|---:|---:|
| AI for Games | 237 | 474 | 237 | 84,486 |
| Game Physics | 959 | 1918 | 959 | 269,076 |
| Large-Scale C | 1023 | 2046 | 1023 | 329,334 |
| Networking | 223 | 446 | 223 | 104,285 |
| **total** | 2442 | 4884 | 2442 | ~787k |

Dual layout: alternating pages `dual[2k]` = source page, `dual[2k+1]` =
translated page (verified: source half matches `src[k]`, `align_ratio ≥ 0.6`
on 2433/2442 pages; the 9 exceptions are blank/cover pages and the 5 footer-
mojibake pages below).

## 2. Stage alignment (7J-1B)

```
source (src[k])
  → translated (dual[2k+1])
  → rendered (mono[k])
```

Alignment check: `dual[2k]` vs `src[k]` text ratio ≥ 0.6 on 2433/2442 pages
(non-issues: blank covers p1 of each book + the footer-corruption pages).

**Translated → rendered loss: 0.** `cjk_delta = 0` on all 2442 pages for all
four books — the CJK translation content that exists in the dual translated
half is fully present in the mono render.

## 3. Found: F9 coverage gap (two text-layer corruption subclasses)

The 7I F9 detector (`content_stream_anomaly`) only sees MuPDF **syntax
errors** (truncated float literals). Real-translation artifacts reveal two
corruption classes that produce **no MuPDF parse error** → detector PASSes
while the text layer is destroyed.

### 3A. Passthrough footer text-layer corruption (5 pages, all in AI)

Publisher footer `Taylor & Francis Taylor & Francis Group
http://taylorandfrancis.com` on AI pages 3/7/11/165/181:

| stage | text layer | content stream | visual glyphs |
|---|---|---|---|
| source | `Taylor & Francis...` ✓ | correct ASCII | ✓ |
| dual src-half | GBK-mojibake `呡祬潲 䙲慮捩...` | **correct ASCII** `(Taylor)&(Francis)` | ✓ (ink present) |
| mono | **all NUL** `\x00\x00...` (60/页) | correct ASCII | ✓ (ink = source 0.87 sim) |

Diagnosis: renderer re-emits passthrough text as a subset TTF (Arial-BoldMT,
`Type0/Identity-H`) whose **ToUnicode CMap maps ASCII CIDs to GBK/NUL** while
the glyph bytes (`Tj` literals) stay correct. Visual OK; copy/search/
select/a11y destroyed. F9-class (text layer vs visual layer mismatch),
**renderer-originated**, currently invisible to the F9 detector.

### 3B. Inline translated NUL (1 site, p157 AI)

`OBJECTxN —␀ Rn` in translated text (`OBJECT` is CenturySchoolbook in source).
A literal NUL code point inside a translated span between an em-dash and a
math-like token `Rn`. Source has no NUL; dual translated half and mono both
carry it → **translation/layout-stage corruption**, second F9 subclass.

## 4. Diagnostics cross-checked

- Truncated-exponent MuPDF spam (`-9.000000001435637e`) seen when scanning
  all books in one process is an **aggregation artifact** — per-file scans and
  raw-byte token search find zero such tokens in any artifact. Not a defect.
- `802.11e` in Networking = WiFi standard text, regex false positive. Not a
  defect.
- Game Physics p37 and Large-Scale C p908 each have a **single** NUL char —
  to be classified (likely a legit code point mapped to NUL, same 3B family).

## 5. F7 eligibility status (unchanged, honest)

Real corpora exist and now have a validated 3-stage page alignment (this
scan), but **block-level** source→translated→rendered triples still require
the harness ingestion path that `capture_source_chain` (identity-only by
design) does not provide. F7 stays **NOT_MEASURED / harness-gated** — the
page-level alignment here is not sufficient to run the F7 detector contract
(needs per-block translation evidence). No fabricated alignment.

## 6. Conclusion for 7J

> **Real translation does produce divergence 7I does not cover: F9 text-layer
> corruption (ToUnicode CMap mis-mapping) on passthrough text and an inline
> NUL in translated spans — both renderer/translation-stage, both currently
> invisible to the F9 detector because they emit no MuPDF syntax error.**

F4×1 @ p300 @ parser (7I frozen baseline) remains intact — this scan ran on a
different (real-translation) corpus and found no new F1/F2/F3/F4/F5/F6/F8/F10
signals; the new signals are F9-subclass coverage gaps.

**Next**: decide whether 7J-2 extends the F9 detector with a text-layer
integrity sensor (NUL/mojibake detection) — production-code-last discipline
applies; evidence first.