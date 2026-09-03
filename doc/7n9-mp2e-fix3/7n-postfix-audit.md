# 7N-8 — Full-book post-FIX machine audit

- run dir: `C:\Users\14977\source\repos\PDFMathTranslate_FORKED\doc\7n9-mp2e-fix3`
- total pages: **562** (plan blocks: 6690, translated: 6046)
- render paths: `{'translate_refit': 3573, 'preserve_float': 2826, None: 97, 'shift_down': 59, 'overlay': 135}`
- fixups: `{'keep': 3805, 'preserve': 2826, 'shift_down': 59}`
- recovery decisions: `{'shrink': 25, 'clip': 1}` steps: `{'WRAP->SHRINK': 24, 'SHRINK': 1, 'WRAP->SHRINK->CLIP': 1}`

## Page grading (A/B/C/D)

| Grade | Pages |
|---|---|
| A | 506 |
| B | 55 |
| C | 0 |
| D | 1 |

### D — confirmed defect candidates: 442

## FIX-2 regression qualification (8D)

- shift_down total: **59**
- shifted with settled commands: **1**
- **decoupled: 0** (must be 0)
- double-shift suspects: 0 (must be 0)
- x-changed: 0 / font-changed: 0 (must be 0)
- alias value mismatches: 0 (must be 0)

## Mono-PDF visual cross-check (8B)

- available: True; flagged blocks checked: 1
- visual_missing: **0** (translation not found at command site)
- visual_overlap: **0** (translation bbox intersects foreign span >10%)


## MECH-3 sweep (8E): shift_down landing vs preserved blocks

- plan-level landing overlaps (>20% of landing box): **30**
- ink-verified on mono PDF: 6
- **real glyph collisions: 1** (must be 0 to close MECH-3 as benign)

  - REAL p233_4 p233 into p233_5 (formula) ink=56.1%

## Forensic packets (8C)

### p442_4 (page 442)

- source (1 src lines): `tions.`
- translated (1 final lines): `系统蒸发散。`
- trace: `[{"decision": "WRAP", "overflow": true, "line_count": 2, "font_size": 7.65}, {"decision": "SHRINK", "overflow": true, "line_count": 2, "font_size": 5.0}, {"decision": "CLIP", "overflow": true, "line_count": 1, "font_size": 5.0}]`
- first_divergence: **Stage-3 (SHRINK line-collapse, no CLIP)**
- rp_overflow=True layout_ok=False
