# 7J-3A — F9 Text-Layer Integrity Detector Contract

**Status: COMPLETE** — detector contract + sensor implemented, qualified on
the real 7J-1/7J-2 corpus. Production *fixes* for Case A/B are 7J-3B/7J-3C
(next); this stage only makes the corruption *visible* (FAIL instead of
silent PASS).

## 1. What was added

### Sensor — `dual_forensics/pdf_inspector.text_layer_integrity`

New page-level evidence on `inspect_page`:

```python
{"checked": bool, "nul_chars": int, "samples": [...], "corruption_suspect": bool}
```

Reports NUL characters and their context in the extracted text layer. The
7J-2 forensics showed both corruption families (Case A `/ToUnicode`
CID-space mismatch, Case B lost code points) produce NUL in the text layer
while MuPDF reports **no syntax error** — the old F9 sensor
(`content_stream_anomaly`) could not see them.

### F9 detector — `_detect_f9_text_visual_mismatch_page` extended

Evidence now = `content_stream` (7H-2B emitter sensor) **+** `text_layer`
(7J-3A sensor):

```
content-stream syntax anomaly            → FAIL @ render
text-layer NUL + translated content      → FAIL @ render
text-layer NUL, no translated content    → SKIP (attribution unavailable)
both clean                               → PASS
neither inspectable                      → SKIP
```

## 2. Frozen contract (7J-3A)

```
PASS    sensor ran + content stream clean + text layer clean
FAIL    content-stream anomaly, OR text-layer NUL with cross-stage evidence
SKIP    no inspectable evidence, OR NUL suspect without translated content
NOT_MEASURED  F9 wired → unused here
```

**Locked invariant:** `NUL/mojibake ≠ automatic corruption`. A NUL count
alone never FAILs a page that has no translated content — the corruption must
be attributable to a render/emission stage via cross-stage evidence, so an
exotic font with legitimate control characters cannot be mislabelled.

## 3. Qualification on the real corpus (AI mono)

| page | text_layer | F9 verdict |
|---|---:|---|
| p3 (footer, Case A) | NUL=60, suspect | **FAIL** `nul_chars=60` |
| p157 (inline, Case B) | NUL=1, suspect | **FAIL** `nul_chars=1` |
| p200 (clean control) | NUL=0 | **PASS** |

Both 7J-2 regression cases now FAIL instead of silent PASS; clean pages stay
PASS; pages inspected before the sensor still evaluate from content-stream
evidence alone (backward compatible).

## 4. Regression

- `tests/test_text_layer_integrity_7j3a.py` — 8 tests (sensor ×2, F9
  contract ×6 incl. the no-translated-content SKIP invariant and backward
  compat).
- Forensic subset (7I-3/4/6/7 + 7J-3A): **86 passed**.
- Full suite: **3990 passed / 3 skipped** (1 pre-existing env flake in
  `test_services.py`, passes in isolation; unrelated to this change).

## 5. Next

- **7J-3B** — fix Case A: make the `/ToUnicode` emitter write CIDs in the
  same space as the content stream (content CID → glyph CID → ToUnicode CID
  one space). Target: `Taylor & Francis` extractable.
- **7J-3C** — fix Case B: trace where `► / ï / →` first becomes `\x00` in the
  translation→layout→emission path and fix that single point.
- **7J-3D** — dual-subclass requalification: A extractable, B restored,
  CJK delta 0, F4×1 @ p300 preserved, F8/F10 no growth, F5/F7 unchanged,
  no MuPDF change.