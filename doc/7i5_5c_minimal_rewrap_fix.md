# 7I-5C — Minimal Re-Wrap SHRINK Fix

**Status:** ✅ COMPLETE — the erroneous single-line-collapse SHRINK is fixed at
its decision point, corpus rerun confirms the causality chain is repaired, and
the two red contract tests are green.

## Fix (minimal, one layer)

Single change in `pdf2zh/semantic/layout/adaptive.py`, **Stage 2 (SHRINK)**:

- **Before (bug):** `lay_out(policy=SHRINK)` → `overflow.py` SHRINK branch →
  closed-form single-line `shrink_to_fit(text, …)` → `_finish([text], …)` —
  it **discarded the already-WRAPped multi-line layout** and re-measured the
  whole paragraph as one line, drove it to the 5.0pt floor, then CLIP.
- **After (fix):** a bounded geometric lattice — descend font size
  (`*0.85`, at most `_MAX_SHRINK_REWRAP_STEPS = 12`, clamped by
  `_shrink_floor`) and **re-`lay_out(policy=WRAP)` at each candidate size**,
  so the text re-wraps under the current box; accept the first size that fits
  (overflow False).  Only if every size down to the floor still overflows does
  it fall through to Stage 3 **CLIP** (terminal, never silent).

SHRINK stays a **single recovery stage** — one `recovery_steps` entry and one
7F-7 trace entry recording its final state (fit or at-floor) — so the
stage-per-decision diagnostics contract and the golden baseline remain intact.

**Explicitly out of scope (per 7I-5 contract):** no minimum-font change, no
``font < X ⇒ CLIP`` hard rule, no banning CLIP, no per-book rules, no renderer /
PDF-emitter / F8-detector / document-model changes, no handling of the
unbreakable residual (deferred to 7I-5D).

## Contract latch flipped

`tests/test_layout_policy_contract_7i5b.py` — the two `xfail(strict=True)`
tests that encoded 7I-5A's smoking gun went **XPASS→green**; markers **removed**
(never allowed to stay permanently red).  4/4 contract tests now pass:
`test_shrink_preserves_wrapped_lines`, `test_wrap_shrink_rewraps_instead_of_clip`,
`test_clip_is_terminal_and_requires_admissible_exhaustion`,
`test_terminal_overflow_verdict`.

## Recovery Transition Histogram (corpus, post-fix)

```
NO_ACTION : 309      fits immediately
SHRINK    :  96      shrinkable single-token / re-wrap fits
WRAP->SHRINK: 84     the re-wrap ACCEPT path  (formerly erroneous WRAP->SHRINK->CLIP)
CLIP ladders:  0     (7I-4-4 pre-fix: 71)
```

The causality fix is proven at the chain level, not just the count:
the 43 `WRAP→SHRINK→CLIP` (WRAP-collapse) cases now resolve as `WRAP→SHRINK`
(ACCEPT).  Genuinely-unbreakable tokens are still protected — verified to
terminate in explicit `CLIP` (overflow=True, never silent).

## Corpus acceptance (5 books / 33 pages / 489 blocks)

| Metric | 7I-4-4 (pre-fix) | 7I-5C | Requirement |
|---|---|---|---|
| F8                 | 71  | **0**  | significant drop ✔ |
| F4                 | 1 @ p300 parser | **1 @ p300 parser** | stays ✔ |
| F10                | 0   | 0      | not up ✔ |
| F6                 | 0/... | 0    | not up ✔ |
| F1 / F2 / F3       | 0   | 0      | no new ✔ |
| dangling / stray / preserved-violation | 0 | 0 | none new ✔ |
| total residual     | 72  | **1**  | — |

`doc/7i4-corpus-baseline/report.md` regenerated (F8 now PASS everywhere; the
7I-4-4 doc that said "only measure, don't fix" describes the baseline that the
fix then eliminated — this doc supersedes it for post-fix state).

## Verification

- Contract tests: 4/4 pass (markers removed); `test_layout_adaptive` /
  `test_flow_recovery_7f6b` / `test_flow_sidechannel` / `test_layout_diagnostics_7f7`
  updated where they had encoded the old clip outcome, + regenerated golden
  baseline (`tests/baselines/layout_diagnostics_7f7.json`) to reflect the
  corrected WRAP->SHRINK fit for a re-wrapable block.
- Full core suite: **2347 passed, 3 skipped** (`--ignore=tests/v3`).
- black / flake8 clean on the new hist tool (`doc/7i5/recovery_transition_histogram.py`).

## Artifacts

- Fix: `pdf2zh/semantic/layout/adaptive.py` (Stage 2 SHRINK)
- Contract tests: `tests/test_layout_policy_contract_7i5b.py`
- Transition histogram: `doc/7i5-transition/{summary.json, report.md}`
- Corpus baseline: `doc/7i4-corpus-baseline/{summary.json, report.md}`

## Next — 7I-5D

Residual unbreakable analysis.  The fix correctly left the genuinely
unbreakable path as terminal CLIP; the sampled corpus happened to contain no
such case (all 71 were re-wrapable), so a targeted unbreakable-token corpus (long
URLs / identifiers / math tokens wider than the box even at floor) is the right
probe to decide whether any policy change is warranted.  Do not force this —
only act if real evidence appears.