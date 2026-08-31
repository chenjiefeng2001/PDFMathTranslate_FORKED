# V1 Failure Corpus — real-PDF audit of the V1 baseline (track A)

Date: 2026-08-30 · Tool: `build/corpus_failure_scan.py` +
`build/failure_forensic.py` (scratch, gitignored) · Data:
`build/corpus_v1_failures.json`.

This answers *"what does the V1 baseline actually ship on real PDFs?"* It is
deliberately **V1-only**: `plan → global_recovery`, NO packing, identity
translation, rendered through the full chain and compared at the words layer.
Two tools:

- the **scanner** produces per-page/per-block *candidate* defect rows (§1–§4);
- the **forensic probe re-derives the actual glyph-rect + SHIFT geometry** of
  each flagged page and turns candidates into judgments (§5).

Packing is a separate, already-measured story (`doc/corpus_layout_scan_7g_report.md`
§8–§14); this corpus isolates **V1 as it stands** BEFORE 7G-4 touches policy.

## 1. What was scanned

45 arxiv books/papers (≤10 MB, deduped). **32 completed**, 3 hard timeouts
(600 s process kill — the heavy books `2111`, `2506.17366`, `2507.15240`, the
class the parallel runner was built for); the pool never wedged.

## 2. Aggregate defect ledger (word-layer row counts, 32 docs)

| problem_type    | rows | docs affected | meaning                                           |
|-----------------|------:|-----------:|---------------------------------------------------|
| `space_loss`    | 530  | 31         | mean output token length high → runs collapsed (proxy) |
| `glyph_overlap` | 360  | 19         | new word-rect overlaps recovery introduced         |
| `oob`           | 289  | 31         | words outside the rendered page rect               |
| `lost_line`     | 11   | 10         | a plan word absent from the recovered page         |
| `font_surge`    | 3    | 3          | a word's glyph height grew (>25% + 0.5 pt)         |

Honest readings:

1. **`recovery_delta` is small and net-positive-but-localised**: Σ = **+72**
   across 32 docs; 9 docs are >0, 15 are <0 (recovery *reduced* word-overlaps),
   the rest ≈0. The reader program — *some* V1 output is visually fine — holds
   at the words layer.
2. **`space_loss` (530) and `oob` (289) blanket the corpus** — honest signal
   that the **words/page-geometry proxies themselves are dominated by the
   renderer's dense-line concatenation and the identity layer, not by V1
   moving text wrong**. Treat them as *"open these pages"* flags, not truths.
3. **`glyph_overlap` (360) is the candidate-signal for the recovery cascade**
   — but §4/§5 show exactly how much of it is real vs renderer noise: the
   scanner number is **inflated**, and the real subset is concentrated on
   pages recovery actually shifted.
4. **`lost_line` (11) and `font_surge` (3, all a `·`)** are detector residue —
   a concatenated token that splits differently across renders, and a bullet
   falling back to a wider font box. Not V1 dropping paragraphs.

## 3. Recovery health on V1 (32 docs)

- **0 of 32 converge** (`converged=False` everywhere); residual block
  collisions run 1–129 per doc; `unresolved` is the honest 7F-9.3 remainder
  (preserved regions / multi-line title fragments that cannot move).
- One genuine **large-shift**: `2608.22602v1 p1 b18`, SHIFT_DOWN **86.3 pt**
  (resolved bbox top 119.2 → 32.5). §5 judgement: this is a *real large shift*
  that **does not collide any neighbour** (parks against the page-bottom
  margin, keeping its own column above it) — an odd-looking placement, not a
  visual overlap.
- `passes` stay bounded (all hit `no_progress` early — the 7F-9.2 guard holds).

## 4. The candidate severe pages (scanner `recovery_delta` ≥ 5)

| doc | severe candidate pages |
|-----|------------------------|
| 2608.27395v1 | p7 (10), p6 (7), p5 (5) |
| 2608.26638v1 | p9 (9) |
| 2608.22602v1 | p11 (13) + p1 large-shift |
| 2608.17744v1 | p15 (8) |
| 2608.20183v1 | p16 (5) |

## 5. Forensic annotation — candidates → REAL / PROXY / UNCERTAIN

The scanner rows are candidates, not verdicts. `build/failure_forensic.py`
re-derives each flagged page's **actual glyph rects in the V1 `plan` vs the
`recovered` render, plus the `global_recovery` SHIFT events** that moved
blocks. Verdict rubric:

- **REAL** — glyph rects from two *different* lines/block actually intersect
  after recovery and did not before, on a page that recovery shifted;
- **PROXY** — the "new overlap" is the renderer's own wrap/line-concat noise
  (present even on pages recovery did nothing to, or same-line fragments);
- **UNCERTAIN** — needs the PNG (ambiguous geometry).

Per-case judgements (geometry evidence, `plan` vs `recovered` word rects):

| doc · page | recovery moves | verdict | evidence |
|------------|----------------|---------|----------|
| **2608.26638 p9** | SHIFT_DOWN #44/#45 ≈9 pt | **REAL** (visual_overlap) | two different reference lines now interleave by 6–25 pt: `'Loïc'(y121–134)↔'jar,'(y127–140)`, `'Barrault,'↔'Marta'`, `'Magdalena'↔'Costa-jussà,'`, … 19 new pairs, all citation lines at y≈121/127/500 – the classic U↕L→L⇒R cascade. |
| **2608.27395 p7** | SHIFT_DOWN #7 13 pt, #65 13 pt, #79 12 pt, … 24 shifts | **REAL** (visual_overlap, wrong_break) | paragraph lines interleave: `'temporalstructure...'(y435)→'thanaspecializedone...'(y441)`, `'broaderimplication.'(y296)↔'Thecomparisonto...'(y297)` — block moved down onto the neighbour below's drawn extent. |
| **2608.22602 p1** | b18 SHIFT_DOWN **86.3 pt** | **UNCERTAIN** (large_shift, but no neighbour overlap) | b18 parked at y20–32 against the bottom margin; the block below/above (b8/b19) is a different column — a big odd move, **not** a created overlap. The page's 6 "new overlap" pairs are within `paragraph#p2_19` (integrates-no-shift — renderer wrap noise, see below). |
| **2608.22602 p11** | #60/#61 only 0.2–4 pt | **PROXY** | 76 "new" pairs but all between a mega-concatenated token `'forLargeLanguageModelServi'` and the line-fragments of the *same* wrapped block `paragraph#p2_87` — the renderer's wrap split. Recovery barely moved anything under 5 pt. |
| **2608.17744 p15** | **SHIFT_DOWN = 0** | **PROXY** | 34 "new overlap" pairs on a page recovery did NOTHING to — all `'theaxesamodelcardpublishes'` mega-token vs the same block's next wrapped line. Pure renderer line-concat noise. |
| **2608.20183 p16** | **SHIFT_DOWN = 0** | **PROXY** | 7 "new overlap" pairs on a no-shift page — Puiseux math text `'Let·(x)='` / formula fragments re-split differently. |
| **2103.04021 p39** | SHIFT_DOWN = 0 | **PROXY** | the `font_surge` flag is a `·` bullet (6.9→11 pt font-fallback box); forensic shows **0 new overlaps**, 71→72 words. Bullet rendering, expected. |
| **1905.11395 p3** | no SHIFT events | **PROXY** | `lost_line=1` is a concatenated `}` token splitting differently (79→80 words). No text dropped. |

Corpus-level quantification (per earliest heavy docs, glyph_overlap rows located
on pages recovery actually shifted ≥7 pt vs all):

| doc | glyph_overlap rows | on pages shifted ≥7 pt |
|-----|-------------------:|------------------------:|
| 2608.26638v1 | 59 | 42 |
| 2608.27395v1 | 52 | 49 |
| 2608.22602v1 | 50 | 19 |
| 2608.17744v1 | 60 | 14 |
| 2608.20183v1 | 18 | **0** |

Reading (this is the 7G-4 decision input):

1. **The real cascade IS concentrated and mono-patterned.** 2608.27395 (49/52),
   26638 (42/59) put almost all their `glyph_overlap` on pages recovery shifted
   ≥7 pt — and the forensic rects show the **same one mechanism everywhere**:
   `U↕L → SHIFT_DOWN(L) → L's drawn lines land on the block below`. That is
   precisely the §14.3 ordered-two-phase target. **7G-4's regression gate
   should be defined at the page level on these shifted pages.**
2. **A meaningful share of the scanner's 360 is renderer noise that recovery
   never created** — 2608.20183 is 18/18 PROXY (its max shift is 10 pt but on
   other pages; the flagged page is no-shift), and 2608.17744 only 14/60 rows
   sit on shifted pages. The **honest cascade number is materially below the
   scanner headline**; the corpus's own `recovery_delta` (§2.1) already
   reflected the smallness (Σ=+72).
3. **The `large_shift` class (86 pt) is real but NOT an overlap** — it is a
   placement-quality issue (a fragment parked far from its neighbours), which
   7G-4's neighbour-aware floor would *also* improve, but it is not the word
   overlap the collision gate measures.

## 6. Verdict vs the operating posture

| claim | status |
|-------|--------|
| "V1 baseline can be frozen" | ✅ no text-drops (Σ lost=11, all 1-token residue), no font bloat, overlaps localised. Freeze stands. |
| "recovery creates local overlap" | ⚠️ **REAL but narrower than the 360 headline** — 2 verified pages (§5 REAL), mono-patterned (U↕L→SHIFT_DOWN→land on below), Σ recovery_delta only +72. |
| "P0 visual classes (font giant / blank / missing space)" | not reproduced; `space_loss`/`oob` are renderer-proxy flags needing the manual PNG check |
| "7G-4 ordered two-phase neighbour-aware cascade" | **GO — clearly justified** | regression gate = per-page recovery_delta ≤ these numbers on **pages recovery actually shifted** (26638 p9, 27395 p7), and 0 new overlaps there; `large_shift` handling is a secondary outcome of the same neighbour-aware floor |

**Next step** — the human-labelled V1 subset is now the §5 table; open the two
REAL pages (`build/v1_failure_shots/2608.26638v1__p09.png`,
`2608.27395v1__p07.png`) to eyeball-confirm, mark any `UNCERTAIN` with the
PNG, then build 7G-4 against the page-level regression gate the table defines.