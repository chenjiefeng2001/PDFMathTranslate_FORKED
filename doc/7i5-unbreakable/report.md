# 7I-5D — Targeted Unbreakable Corpus (evidence-only)

Question: when genuinely no admissible WRAP solution exists, is the terminal CLIP correct / auditable / non-silent?

- cases: **18**  ·  terminal CLIP: **14**
- soundness checks:
  - never_silent: ✅
  - verdict_complete: ✅
  - no_wrong_line_collapse: ✅
  - no_f10_migration: ✅
  - no_spurious_clip_on_wrapable: ✅

**All sound: YES**

## Per-case record (source_width / box_width / font → final)

| case | measure | src_w | box_w | font→final | steps | lines | ovf | decision | reason |
|---|---|---|---|---|---|---|---|---|---|
| url_medium | ascii | 465.0 | 90.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| url_wide_box | ascii | 465.0 | 300.0 | 10.0→6.14 | SHRINK | 1 | False | shrink | unbreakable_token |
| identifier_snake | ascii | 407.0 | 80.0 | 11.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| uuid | ascii | 301.5 | 60.0 | 9.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| math_cid_run | ascii | 405.0 | 70.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| math_operators | ascii | 1512.0 | 50.0 | 12.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| single_huge_word | ascii | 680.0 | 40.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| single_very_huge | ascii | 600.0 | 30.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| wrapable_control | ascii | 490.0 | 40.0 | 10.0→7.22 | WRAP->SHRINK | 11 | False | shrink | width |
| url_medium | cjk | 465.0 | 90.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| url_wide_box | cjk | 465.0 | 300.0 | 10.0→6.14 | SHRINK | 1 | False | shrink | unbreakable_token |
| identifier_snake | cjk | 407.0 | 80.0 | 11.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| uuid | cjk | 301.5 | 60.0 | 9.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| math_cid_run | cjk | 405.0 | 70.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| math_operators | cjk | 1512.0 | 50.0 | 12.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| single_huge_word | cjk | 680.0 | 40.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| single_very_huge | cjk | 600.0 | 30.0 | 10.0→5.0 | SHRINK->CLIP | 1 | True | clip | unbreakable_token |
| wrapable_control | cjk | 490.0 | 40.0 | 10.0→7.22 | WRAP->SHRINK | 11 | False | shrink | width |

## Terminal-CLIP detail (auditability)

- silent truncation: none (overflow always True on CLIP) — see per-case ovf column.
- verdict fields recorded: decision / reason / steps / original↔final font.
