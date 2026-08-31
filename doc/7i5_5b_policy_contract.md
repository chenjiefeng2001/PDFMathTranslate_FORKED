# 7I-5B — Layout / Recovery Policy Contract (frozen)

**Status:** ✅ COMPLETE — contract frozen as red (xfail strict) tests; **no production code changed**.

The 7I-5A causality forensics established the root cause precisely:

```
WRAP → SHRINK(1 line, 5pt floor) → CLIP
       └─ SHRINK discards the multi-line WRAP layout and re-renders the whole
          paragraph as a single-line shrink_to_fit
```

This milestone freezes the *semantics* that must hold after 7I-5C — before
touching `adaptive_layout`.  It does not modify any production code.

## The four contract clauses

1. **WRAP is a valid layout state, not a throwaway.**  SHRINK may change font
   size / line breaks / line count, but it may not degenerate an already
   completed multi-line layout into a single-line `shrink_to_fit` unless a
   re-layout measurement proves a single line is admissible.
2. **SHRINK re-lays-out from current constraints.**  The flow must be
   WRAP → shrink font → re-WRAP → re-measure → ACCEPT (fit) / continue.
   Not WRAP → drop lines → shrink_to_fit → CLIP.
3. **CLIP is a terminal state.**  Allowed only after admissible layout/recovery
   attempts are exhausted.  Wrapable cases must finish WRAP→SHRINK→re-WRAP
   first; only genuinely unbreakable / single-token cases (SHRINK → font floor
   → still overflow) may reach the terminal overflow policy.  There is **no**
   hard-coded `font < 7pt ⇒ CLIP` rule.
4. **Terminal overflow is auditable.**  Preserve `overflow / decision / reason /
   final_font_size / attempts / layout_strategy` so F8 can attribute a
   `terminal_clip` verdict directly instead of guessing from the final PDF.

## Regression latch

`tests/test_layout_policy_contract_7i5b.py` encodes the contract as
`pytest.mark.xfail(strict=True)`.  Today they are **red**:
`test_shrink_preserves_wrapped_lines`, `test_wrap_shrink_rewraps_instead_of_clip`
(the exact `C p62_9` smoking-gun, reproduced at unit level as
`['WRAP','SHRINK','CLIP'] @ 5.0pt → 1 line → clip`).

When 7I-5C lands the minimal SHRINK fix, these flip to **XPASS**, which
`strict=True` turns into a hard failure — forcing the fix to remove the marker.
That is the point: the contract cannot silently regress.

Two clauses already hold today (unmarked):
`test_clip_is_terminal_and_requires_admissible_exhaustion`,
`test_terminal_overflow_verdict` — auditable terminal verdict already exists.

## Result

```
tests/test_layout_policy_contract_7i5b.py: 2 passed, 2 xfailed (red, pending)
tests/test_layout_adaptive.py + test_flow_recovery_7f6b.py: 31 passed (no regression)
black / flake8: clean
```

## 7I-5C acceptance criteria (frozen)

Only the SHRINK recovery path in `adaptive_layout` Stage 3 changes — not
renderer, PDF emitter, CID recovery, F4/F5/F6/F10, parser, or document model.

| Metric            | Requirement                                      |
| ----------------- | ------------------------------------------------ |
| F8                | significant drop, esp. the 43 WRAP-collapse cases |
| F10               | must not rise (no defect migration)              |
| F6                | must not rise                                    |
| F4                | stays 1 @ p300 @ parser                          |
| F1/F2/F3          | no new findings                                  |
| dangling/stray/preserved | none new                                    |
| WRAP→SHRINK→single-line | drops toward 0 unreasonable collapses       |

Do **not** require 71 → 0.  The 43 wrapable cases should lose their erroneous
CLIP; the ~28 genuinely unbreakable cases may legitimately remain as
`terminal_clip` residuals — that is correct output, not failure.

## Next

- **7I-5C:** minimal SHRINK re-wrap fix → flip the two xfail markers green →
  full corpus rerun (33 pages / 489 blocks) → compare the table above.
- **7I-5D:** residual analysis of the remaining unbreakable cases.

## Data & artifacts

- Contract tests: `tests/test_layout_policy_contract_7i5b.py`
- Causality forensics: `doc/7i5-causality/{summary.json, report.md}`
- 7I-5A report: `doc/7i5_5a_clip_causality.md`