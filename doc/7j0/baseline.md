# 7J-0 — Independent Requalification: Frozen Baseline

**Status:** ✅ COMPLETE — evidence-only. No production code, no tests, no
detector, no F4 "fix". Goal: **7I final baseline is reproducible from a clean
checkout.**

## What was done

1. Verified the working tree was clean at the latest committed state
   (`ef78202` — 7I-6C close; chain `ef78202 → f4b6c2f → f329f03 → dcd53a1 → 2819bb6`).
2. Fixed the corpus exactly as committed:
   - 5 books (C book / AI for Games / Game Physics / Networking /
     Multiprocessor 2e), 34 requested pages, 31 evaluable.
   - Multiprocessor Tier-1 sample `[0,5,8,12,20,40,80,120,200,300,400,500,550]`.
3. Re-ran both baseline tools from **committed code only**:
   - `doc/7i4/residual_corpus_scan.py`
   - `doc/7i4/full_corpus_baseline.py`
4. Confirmed **determinism**: each tool run twice; `sha256` of
   `summary.json` + `report.md` **byte-identical across runs**.
5. Confirmed **reproducibility**: regenerated artifacts are byte-identical to
   the artifacts committed at `f329f03`/`f4b6c2f` — `git status` on the
   artifact dirs is clean, i.e. running the committed code reproduces the
   committed baseline exactly.

## Frozen baseline (F1–F10, 31 pages / 489 blocks)

| defect | PASS | FAIL | SKIP | NOT_MEASURED |
|---|---:|---:|---:|---:|
| F1 | 31 | 0 | 0 | 0 |
| F2 | 12 | 0 | 19 | 0 |
| F3 | 31 | 0 | 0 | 0 |
| F4 | 30 | **1** | 0 | 0 |
| F5 | 0 | 0 | 31 | 0 |
| F6 | 10 | 0 | 21 | 0 |
| F7 | 0 | 0 | 0 | 31 |
| F8 | 31 | 0 | 0 | 0 |
| F9 | 31 | 0 | 0 | 0 |
| F10 | 31 | 0 | 0 | 0 |

**Total residual: 1** — the sole measured defect is:

```
defect          F4 ×1
page            p300 (Multiprocessor 2e, 13 blocks / 1 finding)
FDS             parser
glyph           Times-Roman (cid:129)
recovery        unrecoverable — no reliable Unicode evidence
policy          intentionally preserved (DO NOT GUESS)
```

CID detail on Multiprocessor 2e (3 undefined): `recovered_unicode=1`
(`GLBJKM+MTMI` cid:3 → `Θ`, reliable glyph evidence) and
`preserved_placeholder=2` (`GLBJJG+Times-Roman` cid:129 ×2, kept explicit).

## Determinism evidence

```
residual_corpus_scan:  run1 sha == run2 sha   → IDENTICAL
full_corpus_baseline:  run1 sha == run2 sha   → IDENTICAL
regenerated vs HEAD artifacts:  git status clean → IDENTICAL
```

## What this proves

> **7I final baseline is reproducible from a clean checkout.** Running the
> committed code over the fixed corpus yields exactly the committed artifacts:
> F4 ×1 @ p300 @ parser is the *only* residual, F5 is a representation gap
> (SKIP 31/31), F7 is NOT_MEASURED (31/31), and the four-state contract
> (PASS/FAIL/SKIP/NOT_MEASURED) holds without any fabricated zeros.

## Preserved negative control

F4 ×1 @ p300 is deliberately **not** "fixed": the `(cid:129)` bullet has no
reliable glyph/encoding evidence, so CID recovery correctly refuses to guess
and the anomaly stays attributed to **parser** — the system can now prove when
it *should not* repair.

## Next

7J-1 — convert the 7I invariants into a permanent release-qualification suite
(no new production behavior). See `doc/7j1/` once opened.
