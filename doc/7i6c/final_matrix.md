# 7I-6C — Final F1–F10 Coverage Matrix (frozen)

**Date:** 7I-6C close. All cells are *measured claims*, not guesses. The four
states are strict:

```
PASS          = detector executed + evidence sufficient + no defect
FAIL          = detector executed + defect found
SKIP          = detector executed + insufficient evidence / no object to measure
NOT_MEASURED  = detector contract not wired (honest, never dressed as 0)
```

## Matrix (5-book corpus: 31–33 pages, 489 blocks)

| Defect | Status | pages_evaluated | Notes |
|---|---:|---:|---|
| **F1** | PASS | 33/33 | placement within target box |
| **F2** | PASS / SKIP | 33/33 | style-alias check; SKIP where no comparable alias evidence |
| **F3** | PASS | 33/33 | font-size divergence vs layout target |
| **F4** | **FAIL ×1** | 33/33 | `Multiprocessor p300` — source-PDF parser CID anomaly, `(cid:129)`, insufficient recovery evidence → intentionally preserved |
| **F5** | SKIP | 0/33 | representation gap — model emits 0 float blocks (physical layer has drawings=142, images=10) |
| **F6** | PASS / SKIP | varies | caption-evidence pages PASS; pages without captions SKIP |
| **F7** | NOT_MEASURED | 0/33 | contract frozen, gated on real-translation harness (see eligibility assessment) |
| **F8** | PASS | 33/33 | 7I-5C re-WRAP fix; 71 → 0 clip residuals |
| **F9** | PASS | 33/33 | wired 7I-6B; `content_stream_anomaly` clean since 7H-2B |
| **F10** | PASS | 33/33 | wired 7I-6B; all 489 blocks present, 0 dangling / 0 stray |

## Residual histogram (all books)

```
F1       0
F2       0
F3       0
F4       1   ← source-PDF parser anomaly @ p300 (FDS=parser), intentionally kept
F5       0   ← SKIP, nothing to measure
F6       0
F7       0   ← NOT_MEASURED, not wired
F8       0   ← 7I-5C: 71 → 0
F9       0
F10      0
─────────────────
total    1   ← the only real residual is the F4 source anomaly
```

## What the zeros mean (frozen semantics)

- **F1/F2/F3/F6/F8/F9/F10 = 0** → detector executed, evidence sufficient, no
  defect found. These are *measured* clean.
- **F5 = 0** → NOT a clean result; it is a representation gap: the model has no
  float objects to measure (capability dormant, not missing). Do not convert to
  PASS by fabricating a figure model.
- **F7 = 0** → NOT a clean result; NOT_MEASURED. Identity translation is
  degenerate for F7 (translated == source), so the detector must not run until a
  real-translation harness exists.
- **F4 = 1** → the deliberately preserved negative control: a real source-PDF
  anomaly correctly attributed to **parser**, not render.

## Detector independence (control page p300)

```
Multiprocessor p300:
  F4  = FAIL  @ parser   (source CID anomaly)
  F8  = PASS             (no layout clip)
  F9  = PASS             (content stream clean)
  F10 = PASS             (all objects present)
```

One page, four detectors, independent verdicts — no cascade, no false positive
from the F4 anomaly.

## Observability boundary (established by 7I-6C)

| Defect | Unblock condition |
|---|---|
| **F5** | A corpus/model path that actually produces float semantic blocks (wiring `annotate_figures` into `build_document_model` — a production decision, not a scorecard fix) |
| **F7** | A forensic harness ingestion path that aligns real dual/mono artifacts into per-block source→translated→rendered triples |
