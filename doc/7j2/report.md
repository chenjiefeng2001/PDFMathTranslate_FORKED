# 7J-2 — F9 ToUnicode CMap Forensics

**Status: COMPLETE** — evidence-only. First-divergence for both F9 subclasses
identified at the object level; no production code changed.

## 1. Subclass A — passthrough footer text-layer corruption (5 pages, AI)

**Page sample:** AI mono p3 / dual p5, footer
`Taylor & Francis Taylor & Francis Group http://taylorandfrancis.com`.

### Object-level evidence chain

```
content stream (dual p5):  (Taylor)Tj (&)Tj (Francis)Tj ...
                          = CID 0x54 0x61 0x79 0x6C 0x6F 0x72   (ASCII bytes)
embedded cmap (DQQQAE+Arial Bold):  CID 0x54→GID55, 0x61→GID68, ...  ✓ correct
/ToUnicode CMap (xref 2476/3783):   CID 0x37→'T', 0x44→'a', 0x46→'c', ...  ← WRONG space
```

The `/ToUnicode` CMap maps CIDs in a **subset-renumbered space**
(`0x37→'T'`) that does not match the CIDs actually used by the content
stream (`0x54='T'` ASCII byte). The embedded font's cmap *does* agree with
the content stream, so **glyphs render correctly** while **every reader
decodes the text layer wrong**.

### Three-reader differential (7J-2B)

| reader | mono p3 footer | dual p5 footer |
|---|---|---|
| MuPDF/PyMuPDF | `\x00`×60 (NUL) | `呡祬潲 䙲慮捩...` (GBK-mojibake) |
| PyPDF2 3.0.1 (independent) | footer dropped (no match) | `呡祬潲 & crancis Group` (mojibake) |
| pdfminer | **crash** — `struct.error: unpack requires a buffer of 6 bytes` in `do_Tj` | — |

No reader can recover `Taylor & Francis`; glyph ink is correct (source
similarity 0.85–0.87). MuPDF reports **zero syntax errors** — the emitter
defect is invisible to the current F9 sensor (`content_stream_anomaly`
returns `anomaly=False`, `tokens=0` on both pages).

### Classification: **Case A — emitter/PDF-generation defect**

First divergence is at **PDF emission**: the `/ToUnicode` CMap was written
against a CID space that disagrees with the content-stream CIDs. Glyphs and
font cmap are correct; only the ToUnicode mapping is wrong.

## 2. Subclass B — lost special code point → NUL (3 sites, 3 books)

```
AI p157:     OBJECTxN —► Rn  →  OBJECTxN —\x00 Rn     (U+25BA ► lost)
GP p37:      Anaïs Wheeler    →  Ana¨\x00s Wheeler      (U+00EF ï lost)
LSC p908:    ... 2020-02-01) → 2  →  ... 2020-02-01) \x00 2  (arrow lost)
```

Each source span contains a non-ASCII special character (`►`, `ï`, an arrow)
that becomes a literal NUL in the translated span (SourceHanSerif). Source
has no NUL; both dual-translated half and mono carry it — the loss happens
**during translation output generation** (special code point dropped at
font/character mapping), not at render time.

### Classification: **translation/layout-stage loss** — distinct layer from
subclass A (which is a ToUnicode CID-space error at PDF emission).

## 3. F9 detector gap (confirmed)

`content_stream_anomaly` on both pages:

```
mono p3:   anomaly=False  tokens=0   text-layer NUL=60
mono p157: anomaly=False  tokens=0   text-layer NUL=1
```

The F9 sensor only sees MuPDF syntax errors (truncated float literals). NUL /
mojibake text layers emit **no syntax error** → F9 PASSes while the text
layer is destroyed. Two corruption families, both invisible:

- A: `/ToUnicode` CID-space mismatch (emitter)
- B: lost code point → NUL (translation/layout)

## 4. Corpus-level impact (from 7J-1C)

| book | pages | NUL pages | total NUL chars | CJK delta (tr→rendered) |
|---|---:|---:|---:|---:|
| AI | 237 | 6 | 301 | 0 |
| Game Physics | 959 | 1 | 1 | 0 |
| Large-Scale C | 1023 | 1 | 1 | 0 |
| Networking | 223 | 0 | 0 | 0 |

AI carries the real defects (footer ×5 pages, inline ×1). Single-NUL pages
in GP/LSC to be classified in 7J-2C.

## 5. Regression corpus freeze (7J-2C)

| case | expected |
|---|---|
| A: AI mono p3 footer | text-layer corruption (NUL) |
| A: AI dual p5 footer | text-layer corruption (mojibake) |
| B: AI p157 `—\x00 Rn` | NUL in translated span |
| B: GP p37 `Ana¨␀s` | NUL in translated span (ï lost) |
| B: LSC p908 `) ␀ 2` | NUL in translated span (arrow lost) |
| Networking (clean) | PASS |
| normal CJK (any clean page) | PASS |
| Latin-only (clean page) | PASS |

## 6. 7J-3 decision

Both subclasses are **production-pipeline defects** (emitter + translation/
layout), not reader/dependency issues — no MuPDF upgrade, no pdfminer fix
needed. Fixes belong to the pdf2zh emission and translation path, and the F9
detector needs a **text-layer integrity sensor** (NUL / mojibake detection)
so this class becomes FAIL instead of silent PASS. Per the 7J-1 principle:
production-code-last — detector contract first, then minimal fix.