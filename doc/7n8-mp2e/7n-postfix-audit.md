# 7N-8 — Full-book post-FIX machine audit

- run dir: `C:\Users\14977\source\repos\PDFMathTranslate_FORKED\doc\7n8-mp2e`
- total pages: **562** (plan blocks: 6690, translated: 6046)
- render paths: `{'translate_refit': 3576, 'preserve_float': 2826, 'shift_down': 153, 'overlay': 135}`
- fixups: `{'keep_overflow': 22, 'preserve': 2826, 'keep': 3689, 'shift_down': 153}`
- recovery decisions: `{'shrink': 25, 'clip': 1}` steps: `{'WRAP->SHRINK': 24, 'SHRINK': 1, 'WRAP->SHRINK->CLIP': 1}`

## Page grading (A/B/C/D)

| Grade | Pages |
|---|---|
| A | 456 |
| B | 105 |
| C | 0 |
| D | 1 |

### D — confirmed defect candidates: 442

## FIX-2 regression qualification (8D)

- shift_down total: **153**
- shifted with settled commands: **37**
- **decoupled: 0** (must be 0)
- double-shift suspects: 0 (must be 0)
- x-changed: 0 / font-changed: 0 (must be 0)
- alias value mismatches: 0 (must be 0)

## Mono-PDF visual cross-check (8B)

- available: True; flagged blocks checked: 1
- visual_missing: **0** (translation not found at command site)
- visual_overlap: **1** (translation bbox intersects foreign span >10%)

  - OVERLAP p442_4 p442 `系统蒸发⋯` × `using one for even-numbered ph` 43.6%

## MECH-3 sweep (8E): shift_down landing vs preserved blocks

- plan-level landing overlaps (>20% of landing box): **36**
- ink-verified on mono PDF: 13
- **real glyph collisions: 0** (must be 0 to close MECH-3 as benign)


## Forensic packets (8C)

### p442_4 (page 442)

- source (1 src lines): `tions.`
- translated (1 final lines): `系统蒸发散。`
- trace: `[{"decision": "WRAP", "overflow": true, "line_count": 2, "font_size": 7.65}, {"decision": "SHRINK", "overflow": true, "line_count": 2, "font_size": 5.0}, {"decision": "CLIP", "overflow": true, "line_count": 1, "font_size": 5.0}]`
- first_divergence: **Stage-3 (SHRINK line-collapse, no CLIP)**
- rp_overflow=True layout_ok=False
