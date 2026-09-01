# 7I-7 — XObject / Unicode Compatibility Investigation

**Status:** 7I-7A (reproduction) ✅ · 7I-7B (first-divergence / root cause) ✅ ·
7I-7C (fix decision) 🔒 pending evidence-based choice · 7I-7D (regression) 🟡
(latch written, production fix not yet made)

## Reported failure

Two books fail during translation with:

```
Xobj id must be provided when unicode is provided
```

- `Matrix Algebra (Abadir K.M., Magnus J.R.)`
- `Groups and Symmetries: From Finite Groups to Lie Groups, Second Edition`

Both source PDFs live in `tests/file/`; neither was part of the 7I-4 corpus or
the F1–F10 forensic contract (7I-6's "F4 ×1 is the only measured residual"
means exactly that — this is a separate production failure path).

## 7I-7A — Reproduction

### Error origin

The string exists in exactly one place in the environment:

```
babeldoc/format/pdf/document_il/midend/typesetting.py:136
    class TypesettingUnit:
        def __init__(..., unicode=None, ..., xobj_id=None, ...):
            if unicode:
                assert font_size, "Font size must be provided when unicode is provided"
                assert style, "Style must be provided when unicode is provided"
                assert len(unicode) == 1, "Unicode must be a single character"
                assert xobj_id is not None, "Xobj id must be provided when unicode is provided"
```

It is **BabelDOC's invariant**, not pdf2zh's. `babeldoc>=0.6.4` is the
installed engine (0.6.4).

### Minimal reproducer (frozen as a test)

A paragraph carrying a translated **unicode** composition
(`pdf_same_style_unicode_characters`) whose `xobj_id` is `None`:

```python
PdfParagraph(xobj_id=None,
             pdf_paragraph_composition=[PdfParagraphComposition(
                 pdf_same_style_unicode_characters=PdfSameStyleUnicodeCharacters(
                     unicode="测", pdf_style=style))])
→ Typesetting.create_typesetting_units()
→ AssertionError: Xobj id must be provided when unicode is provided
```

Boundary confirmed: `xobj_id in (-1, 0, 1, 7)` all pass; only `None` asserts.
`-1` is BabelDOC's own sentinel for "no XObject" (used at typesetting.py:1250),
so `None` is the abnormal state.

## 7I-7B — First divergence / root cause

### Data flow (the wire that breaks)

```
PDF content stream (C parser: new_parser → legacy IR)
  → per-char xobj_id            (from the native text sink)
  → PdfCharacter.xobj_id        (il_creater.py:1079: xobj_id=char.xobj_id)
  → paragraph_finder.py:158     paragraph.xobj_id = chars[0].xobj_id
  → il_translator               translation output → PdfSameStyleUnicodeCharacters
  → typesetting.py:1505+        TypesettingUnit(unicode=..., xobj_id=paragraph.xobj_id)
  → assert xobj_id is not None  ← FAILS when paragraph.xobj_id is None
```

### Trigger condition

- A paragraph whose first character's `xobj_id` is **None** (page-level text
  not attributed to any Form XObject container), **and**
- that paragraph receives a **translated unicode composition** (any real
  translation that changes text — the normal case).

Legacy `IlCreater` seeds `self.xobj_id = 0` (never None); the active new-parser
path can leave page-level characters with `None`. Both reported books place
essentially **all text at page level** (PyMuPDF probe: `xobjects=0` on the vast
majority of pages, including p2/p3/… of Matrix Algebra and p0–p39+ of Groups
and Symmetries), which is exactly the class where `None` can survive.

### Same root cause for both books — yes

Both books share the identical failure surface: page-level text streams (no
Form XObject wrapper), a shared pipeline contract (BabelDOC typesetting), and
the same assertion. This is **one shared root cause**, not two independent
failures.

### Divergence layer

- **First divergence**: BabelDOC frontend parsing — the page-level character
  `xobj_id` is not guaranteed non-None (parser-originated).
- **Failure point**: BabelDOC midend typesetting assertion (the invariant is
  stricter than the parser's guarantee).

Neither layer is pdf2zh's code; pdf2zh's role is bridging into BabelDOC.

## Fix layer decision (7I-7C — pending, evidence-based)

Options under the 7I discipline (no fix without evidence, no hard-coded rules):

1. **Upstream/patch BabelDOC's typesetting** to tolerate `xobj_id=None` for
   page-level text (treat None like the `-1` sentinel). Correct semantically:
   page-level unicode text has no XObject container by definition; the -1
   sentinel already exists for exactly this.
2. **pdf2zh-side monkeypatch** (following `babeldoc_formula_protect.py` /
   `babeldoc_toc_protect.py` pattern) that normalizes `paragraph.xobj_id`
   None→-1 before typesetting. Local, version-pinned, no site-packages edits.
3. **BabelDOC version pin / upgrade** — only if upstream has a fix; not
   confirmed for 0.6.4.

Recommended: **(1) upstream-first, (2) as the local shim** — but this is a
production-engine change and must be decided with the user before 7I-7C is
implemented. The regression latch (below) is already in place regardless.

## 7I-7D — Regression latch (written)

`tests/test_xobj_unicode_7i7.py` (5 tests) freezes:

- the exact assertion (`None` → raise `Xobj id must be provided …`);
- the boundary (`-1/0/1/7` → pass);
- the error-string origin (BabelDOC `TypesettingUnit.__init__`);
- the root-cause wire (`paragraph.xobj_id = chars[0].xobj_id`);
- the high-risk page-level-text shape of both reported books.

## Artifacts

- `doc/7i7/reproduce_xobj_unicode.py` — drives real BabelDOC engine with a
  stub translator (identity or CJK-mapped) over both books.
- `doc/7i7/instrument_typesetting.py` — patches `TypesettingUnit.__init__` to
  observe `xobj_id` distribution during a real run.
- `doc/7i7/report.md` — this report.
- `tests/test_xobj_unicode_7i7.py` — regression latch (5 pass).

## Open question for the user

The production fix (7I-7C) is a **BabelDOC-engine-level** change. Do we:

- **A.** patch upstream BabelDOC (contribute fix + vendor pin), or
- **B.** shim in pdf2zh (`babeldoc_xobj_shim.py` following the existing
  monkeypatch pattern: normalize `paragraph.xobj_id` None→-1 before
  typesetting), or
- **C.** hold 7I-7C until a real full-book run (with a real translator)
  reproduces the failure end-to-end, keeping the latch as the guard?
