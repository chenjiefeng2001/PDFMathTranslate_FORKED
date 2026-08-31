# 7I-5D — Unbreakable Residual (evidence-only)

**Status:** ✅ COMPLETE — evidence-only; **no production code changed**.

7I-5C eliminated all 71 corpus F8 clips because every sampled block was
re-WRAPable.  The genuinely-unbreakable path (a single token wider than the box
even at the 5pt floor) never appeared.  7I-5D builds a **targeted** corpus to
ask: *when no admissible WRAP solution exists, is the terminal CLIP correct,
auditable, and non-silent?*

## Corpus (drives the production `render_flow_text` ladder, both ascii + cjk tables)

| family | cases |
|---|---|
| long URL / path / identifier | url_medium, url_wide_box, identifier_snake, uuid |
| oversized math token          | math_cid_run, math_operators |
| extreme single token          | single_huge_word, single_very_huge |
| wrapable control (margin)     | wrapable_control |

18 case-records.  Each records source_width / box_width / box_height /
initial_font / min_font / WRAP presence / SHRINK attempts / final_font /
line_count / overflow / terminal_decision / reason / steps / reconstruction.

## Result

**All 5 soundness checks pass (All sound: YES).**

| check | outcome |
|---|---|
| ① never silent            | ✅ CLIP always `overflow=True` |
| ② verdict complete        | ✅ decision / reason / steps / original↔final font all recorded |
| ③ no wrong line collapse  | ✅ unbreakable tokens kept whole on one line (correct); wrapable control re-WRAPs to 11 lines |
| ④ no F10 migration        | ✅ wrapable control reconstructs fully — nothing vanishes |
| ⑤ no spurious clip on wrapable | ✅ control → `WRAP→SHRINK` → 11 lines → `overflow=False`, never clipped |

Representative cases:

```
url_medium (src 465 / box 90, font 10)   → SHRINK->CLIP @5.0  ovf=True  unbreakable_token  (1 line, whole)
url_wide_box (src 465 / box 300)          → SHRINK @6.14       ovf=False shrink            (shrinks to fit — correct)
single_very_huge (src 600 / box 30)       → SHRINK->CLIP @5.0  ovf=True  unbreakable_token
wrapable_control (src 490 / box 40)       → WRAP->SHRINK @7.22 ovf=False shrink   11 lines (the 7I-5C fix holds)
```

## Verdict

> When genuinely no admissible WRAP solution exists, the current terminal CLIP
> is **correct, auditable, and non-silent**: it shrinks to the 5pt floor, keeps
> the unbreakable token whole on its own line, reports `overflow=True`, and
> records the full verdict — it never silently truncates, never collapses a
> valid multi-line layout, and never migrates to F10.

Per the 7I-5D stop condition, this is the **known admissible terminal overflow
class**.  **No new special recovery policy is introduced** (no evidence warrants
even a URL-break rule).  The targeted corpus proves terminal CLIP does **not**
cause semantic loss that would justify a 7I-5E / new policy investigation.

## 7I-5 conclusion

- Wrapable content keeps layout semantics via re-WRAP SHRINK (7I-5C).
- Truly indivisible and uncontainable tokens enter explicit terminal overflow
  (this milestone) — admissible, auditable, non-silent.
- No evidence ⇒ no new recovery policy.  This is invariant, not a gap.

## Artifacts

- Probe: `doc/7i5/unbreakable_corpus.py` (black/flake8 clean)
- Data: `doc/7i5-unbreakable/{summary.json, report.md}`
- Committed: 7I-5C atomic `2819bb6`; 7I-5D is evidence-only (untracked docs
  unless you choose to commit them).