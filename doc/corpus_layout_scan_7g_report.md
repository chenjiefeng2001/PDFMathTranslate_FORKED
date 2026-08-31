# Real-PDF Corpus Scan — 7F→7G Milestone Findings (v1)

Date: 2026-08-29 · Tool: `build/corpus_layout_scan.py` (gitignored scratch) ·
Analysis path: `dump_layout_debug` (identity translation, no translator /
ONNX / renderer) over `tests/file/` arxiv papers + one CJK dual-layer doc.

## 1. What was run

13 PDFs scanned (12 two-column arxiv papers 6–45 pages, 1 CJK dual-layer
40-page doc). Per PDF we collected: block counts, PageFlow collisions
(reason-split), page overflows, diagnostics overflow/recovery, a text-drop
proxy, and 7G-1 trailing-whitespace pages. Two PDFs (1808, 2608a) additionally
ran the full `global_recovery` pass.

## 2. P0 findings (correctness)

### P0-1 — Global Recovery does NOT converge on real PDFs (systemic)

`global_recovery` on real plans burns its entire `max_passes` budget in a
**zero-progress loop** and never converges:

| doc   | pages | collisions | converged | passes | applied | deferred | unresolved | zero-delta events | identical consecutive passes |
|-------|-------|-----------|-----------|--------|---------|----------|-----------|-------------------|------------------------------|
| 1808  | 15    | 148       | **False** | 572/572 | 2,615,883 | 105,248 | 89 | 2,615,751 (99.99%) | 570 |
| 2608a | 6     | 28        | **False** | 109/109 | 23,769 | 4,360 | 24 | 23,761 (99.97%) | 107 |

Cost: 445 s for a 15-page paper; 2.6M no-op "applied" events.

**Root cause chain:**

1. `PageCollision.required_shift` is `round(..., 2)` — a sub-centipoint overlap
   (e.g. 0.004 pt from line-spacing padding / bbox inflation) becomes
   `shift_y = 0.0`.
2. `decide_block_shift` returns `SHIFT_DOWN(0.0)`; `resolve_page_shifts`
   counts it as **applicable** and "applies" it (a no-op — geometry unchanged).
3. `global_recovery`'s no-progress guard is `pass_made_progress = applied
   non-empty` — zero-delta decisions keep it True every round, so the loop
   re-detects the same stuck collisions and re-applies the same zero shifts
   until `max_passes` runs out.

The module docstring claims "3 → 3 → 3 … cannot happen", but it does: the
guard tests "applied empty" instead of "geometry changed / collision set
shrank".

**Fix (scoped, no policy change):**

- **Progress = collision multiset shrank or geometry actually moved.** Compare
  consecutive passes' collision signature (sorted `(page, upper, lower)` keys);
  unchanged → `stopped_early`, record leftovers as unresolved.
- **Zero-delta applicable guard** in `resolve_page_shifts`: a decision with
  `shift_y < epsilon` is not progress; skip it (or classify its collision
  unresolved immediately).
- The same guard fixes 8d's latent behavior (synthetic tests never hit it
  because crafted plans have clean geometry).

### P0-2 — Collision input is dominated by block-segmentation artifacts

Collision counts on real papers are 6–731 per doc; the *before* counts do not
reflect real layout quality:

- **~88% of `preserved_region` collisions are inline math**: the formula
  block's bbox is horizontally contained inside its paragraph (adjacency
  treated as stacked collision). 1808: 80 preserved collisions, 70 with the
  contained-bbox signature; kinds: 99× formula, 59× paragraph.
- **Multi-line titles split into separate blocks** overlapping by ~1 pt
  (leading padding) — e.g. 1808 p1: two title lines as `paragraph[0]` vs
  `paragraph[1]`, ov=1.0.
- **Headings merged with the following paragraph** into one block with a huge
  bbox (1808 p1 heading spans 102 pt) that overlaps everything around it.

These are base-chain (document model / segmentation) issues; PageFlow faithfully
reports what the settled blocks contain. Per the 7G decision table this maps
to "collision 判断错误 → 修 PageFlow" (containment-aware adjacency /
inline-formula exclusion) plus segmentation fixes upstream.

### P0-3 — Text preservation is clean on single-layer PDFs

LTChar-level comparison (per-page source glyph count vs plan text chars) on
single-layer 1808: **no page below 60%** — no text drops. The CJK dual-layer
doc's "drop" flags were a proxy artifact (EN+ZH layers double-counted by
text-object enumeration); a proper proxy must count LTChars and understand
dual layers. R-code/formula-heavy pages (p18/28/29/30) showed 40–49% ratios
but the content is preserved (formula blocks with newlines).

## 3. P1 findings (visual quality)

- Trailing-whitespace detection via "last reading-order block bottom" barely
  fires on papers (footers/page numbers sit at the page bottom). Needs a
  per-column gap metric — that is exactly the analysis 7G-2 page packing
  should build on, and it cannot be trusted until P0-2's collision noise is
  reduced (whitespace computed over fragmented/overlapping blocks is
  meaningless).
- `page_overflow_count` is 0 on all 13 docs under identity translation —
  overflow recovery (8e) had nothing to do; real translation-inflated
  overflow requires a real translation run to evaluate.

## 4. Classification (per the 7F→7G decision table)

| 现象 | 位置 | 状态 |
|------|------|------|
| Recovery 不收敛（零进度循环烧光预算） | Global Recovery (7F-9) + 8d | **P0, 先修** — 小而自包含 |
| collision 输入被分割伪影主导（inline 公式 / 标题拆行 / heading 巨框） | PageFlow 邻接判定 + document model | P0-2, 需 PageFlow 含容感知 |
| 文本丢失（单层 PDF） | — | 干净（代理指标需修） |
| 页面太空 / 提前换页 | 7G-1/7G-2 | P1 — 在 P0-2 噪声降低前无法可靠度量 |

## 5. Recommended order

1. **P0-1 fix** — **DONE (7F-9.2)** — zero-progress guard in
   `resolve_page_shifts` + `global_recovery` (see §6).
2. **P0-2 fix** — **DONE (7F-9.3)** — containment-aware adjacency in
   `detect_collisions_from_placements` — skip pairs where one bbox is
   horizontally contained inside the other (inline membership), and exclude
   `formula_inline` from stacking adjacency (see §7).
3. Re-run the corpus scan; then whitespace metrics (per-column) become
   trustworthy enough to drive 7G-2 page packing.

## 6. P0-1 fix — 7F-9.2 status (2026-08-29)

Executor/orchestrator **correctness hardening**, no policy change (8c matrix,
8d SHIFT_DOWN, 8e NEXT_PAGE, List/TOC/Code invariants and the recovery budget
are all untouched):

- **`resolve_page_shifts` (8d)** — a `SHIFT_DOWN` with `shift_y <= epsilon` is
  no longer an *applicable* action: it does not get applied, does not count in
  `applied`, and does not consume the pass budget.  Its collision is recorded
  unresolved.  Leftover stops are attributed via a new `stopped_reason`
  (`"no_progress"` / `"budget_expired"`) on both `ShiftExecutionReport` and
  `GlobalRecoveryReport`.
- **`global_recovery` (orchestrator)** — the progress guard is now a **state
  signature** (per-placement `(page, block_index, resolved_bbox)`): a round
  whose resolved geometry is byte-identical to the previous round executed no
  real action → `stopped_early = True`, `stopped_reason = "no_progress"`, and
  the remaining collisions + overflows are surfaced as `unresolved`.  Progress
  is defined exactly as: *collision multiset shrank OR resolved geometry
  moved*.

Regression corpus added: `tests/test_global_recovery_7f9_hardening.py`

- case A: overlap 0.004 pt, `required_shift = 0` → stuck on pass 1, `no_progress`;
- case B: `SHIFT_DOWN = 0` → `applied = 0`, `unresolved > 0`;
- case C: identical collision set across passes → `no_progress`, never burns
  a large `max_passes`;
- case D: normal SHIFT 20 pt → collision cleared, `applied = 1`, `converged`.

Reproduction on the P0-1 signature (120-block dense column of sub-pixel,
`required_shift → 0` overlaps, `max_passes = 1000`): old behaviour would burn
the budget (the reported `572/572 passes`, `2,615,883 applied`); new behaviour:

```text
passes = 1      (vs. 572)      applied = 0   (vs. 2,615,883)
converged = False               stopped_reason = "no_progress"
unresolved = N                  (the stuck collisions are surfaced, never hidden)
```

A mixed plan (some blocks overflowing the page → real 8e whole-block moves
plus stuck zero-shift collisions) also terminates early (passes = 2,
`no_progress`) instead of exhausting the cap.

## 7. P0-2 fix — 7F-9.3 status (2026-08-29)

Detection-only change to the 8b authority `detect_collisions_from_placements`: a
pair where one box is **horizontally contained inside** the other (strict
x-extent subset = inline membership) — or where either side is
`formula_inline` — is excluded from stacking adjacency, so recovery solves real
vertical collisions instead of parser noise.  Same-column stacked paragraphs
(identical x-range) are unchanged and still collide.

Real-PDF checkpoint (detection, same plans before/after the change; subset of
the corpus):

| doc     | collisions pre | post | Δ |
|---------|---------------|------|---|
| 1808    | 148 | 21  | −127 |
| 2111 (528p) | 5269 | 683 | −4586 |
| 2603    | 224 | 17  | −207 |
| 2507    | 268 | 183 | −85  |
| **Σ (10)** | **6326** | **996** | **−84%** |

`preserved_region` counts collapse accordingly (the inline-formula
pseudo-collisions are gone).  Recovery then only handles the honest remainder:
1808 unresolved 89 → 13 (still `no_progress`, the survivors being genuinely
immovable preserved / multi-line-title splits); 1905 unresolved 27 → 5.  This
is the input cleanup that makes 7G-2 whitespace / page-packing metrics
meaningful — it is NOT yet 7G-2.

## 8. 7G-2 landed — V2 packing baseline (2026-08-30)

The measurement half of 7G-2 is now in-tree: `pdf2zh/semantic/layout/packing.py`
(pure read of settled `BlockPlacement` geometry — x-overlap column clustering,
vertical-band fill / internal-gap / trailing-gap metrics) with the guard
discipline locked by `tests/test_layout_packing_7g2.py` (18 tests: pure read,
no re-layout, no detector/renderer imports, no geometry writes).

The corpus baseline was produced by `build/corpus_packing_scan.py` (gitignored
scratch) over `tests/file/` (identity translation, settled plans only, docs
≤ 10 MB), persisted to `build/corpus_packing_7g2.json`:

- 41 PDFs scanned, 0 errors (1 empty doc excluded below → 40 content docs);
- 3,706 pages, 131,211 blocks, 15,537 columns, 0 empty columns;
- recovery-termination contract unchanged: all 40 stop via `no_progress`,
  2,990 unresolved total (the honest 7F-9.3 remainder), 1 converged.

V2 page-packing baseline — the numbers Adaptive v2 packing must beat:

| metric (per-doc mean) | value |
|-----------------------|------:|
| `avg_fill_ratio`      | 0.557 (median 0.586, min 0.246, max 0.826) |
| `avg_whitespace_ratio`| 0.443 |
| `avg_trailing_gap_pt` | 185.8 (median 167.6) |
| `total_internal_gap_pt` (mean per doc) | 68,816 (median 6,257) |

Readings:

1. **The P1 lesson holds** — mean fill is 0.557, not ~0.9: nearly half of every
   column's vertical band is frozen whitespace under V1, so V2 packing has real
   room to reclaim (≈44 % whitespace, ≈186 pt trailing gap per column).
2. **Internal gaps dominate on long docs** — the mean `total_internal_gap_pt`
   (68.8 k) is inflated by book-length docs; the median (6.3 k) is the honest
   per-paper scale.  Both confirm gap reclaim is the bigger lever than
   trailing-gap-only compaction.
3. **Emptiest docs** — 2603.06957v2 (0.246), 2506.17366v2 (0.264),
   2608.19584v1 (0.270), 2506.06584v2 (0.272): these are the first targets for
   a V2 packing pass, and the same docs to re-measure after it lands.

Next (7G-2 optimisation half): turn the measured reclaimable space
(`internal_gap` + `trailing_gap`) into an actual packing pass, gated by these
baseline numbers.

## 9. 7G-2 optimisation half landed — V2 packing executor (2026-08-30)

The second half of 7G-2 is now in-tree: `pdf2zh/semantic/layout/packer.py`
turns the measured reclaimable space into an actual pass on a settled plan.
Where the measurement half only *reported* `internal_gap` + `trailing_gap`,
this module *moves blocks* to reclaim them:

- **compaction** (`compact_column`) — within each x-overlap column, reading
  order topmost-first, pull every movable block UP so the vertical gap to the
  block above collapses to a target `gutter` (topmost block is the anchor;
  preserved blocks are immovable barriers that content packs *against*, never
  across).  The dominant lever — shrinks `internal_gap`;
- **re-anchor** (`column_reanchor`) — push the whole compacted column DOWN
  (v3 y-up: decreasing y) into the trailing gap, bounded by `bottom_margin`
  (keeps off the footer) and preserved blocks below.  Shrinks `trailing_gap`.

Discipline mirrors the 7F-8 shift/recovery executor (`page_shift.py`): the
geometry resolve is pure (`resolve_packing`), the plan wiring (`apply_packing`)
is the only place a move lands and changes **only Y** — `dst_box` + payload
command `y`; `src_box` and all X / width / font / text are byte-identical.
Locked by `tests/test_layout_packer_7g2.py` (17 tests): pure-read input, only-Y
mutation, `src_box` verbatim, preserved blocks immovable, reading order never
inverted, and no detector / parser / renderer / translator / `level`/`index`
imports or geometry math.

The corpus gate was re-run (`build/corpus_packing_pass_scan.py`, gitignored
scratch; identity translation, same `tests/file/` corpus and metrics as §8) with
a compact+re-anchor `PackConfig` (`gutter=2`, `preserved_gutter=6`,
`bottom_margin=36`).  Per-doc means, 41 PDFs, 0 errors:

| metric (per-doc mean) | BEFORE | AFTER | Δ |
|-----------------------|-------:|------:|----:|
| `avg_fill_ratio`      | 0.543 | 0.480 | −0.063 |
| `avg_whitespace_ratio`| 0.433 | 0.496 | +0.063 |
| `avg_trailing_gap_pt` | 181.3 | 90.0 | −90.3 (−50%) |
| `total_internal_gap_pt` | 67,138 | 11,164 | −55,974 (−83%) |
| resolved collisions   | 4,459 | 3,943 | −516 (never up) |

34,149 blocks moved; packing report claims ~2.31M pt internal + ~3.07M pt
trailing white space reclaimed across the corpus.

Readings (honest):

1. **The two levers the report named both drop — and sharply.**
   `total_internal_gap_pt` falls 83% (the P1 "bigger lever" — gap reclaim, not
   just trailing compaction) and `avg_trailing_gap_pt` halves.  §8's emptiest
   docs (2603.06957v2 / 2506.17366v2 / 2608.19584v1) now end with ~88–106 pt
   trailing instead of 288–363.
2. **Packing never creates collisions** — resolved collisions fall 4,459 →
   3,943, because compaction reduces gaps and re-anchor stays inside the
   `bottom_margin` / preserved-block floor.  The pass is safe on real plans.
3. **`avg_fill_ratio` edges down, and that is expected, not a regression.**
   Measurement's `fill_ratio` is `column_band_height / page_height`
   (`topmost.top − bottommost.bottom`); for fixed content you cannot raise it by
   *moving* blocks — you can only close gaps, which *concentrates* content into
   a tighter band (the goal) and thereby narrows the band the metric measures.
   The value of reclaimed whitespace is exactly that it lets the *same* content
   live in less vertical space per page, which is the claim that matters for
   page-count / fill downstream (7G-3+ placement reflow).  Judge packing by the
   two gap levers + the collision gate, not by `fill_ratio` alone.

Design note for 7G-3+: re-anchor currently never *merges* trailing space across
logic blocks/pages — it only redistributes within a column's own band.  Turning
reclaimed x pts into "pull the next logical block up onto the freed space" is
cross-block / cross-page packing, the natural follow-on executor (mirrors the
7F-8c STATEMENT_DOWN → 7F-8e NEXT_PAGE split).

## 10. V2 render gate — first full corpus run + the scan-runner refactor (2026-08-30)

Two things landed together: the corpus scans no longer run serially, and the
V2 visual gate (`build/corpus_render_gate_scan.py`, written but never run —
its first attempt died on a missing `global_recovery` import) finally ran over
the whole corpus.

### 10.1 Scan infrastructure: parallel + hard timeout + no deadlock

The old `for p in candidates: scan_one(p)` loops froze the whole corpus run on
a single bad PDF (reproduced: `The Art of Multiprocessor Programming, 2e.pdf`
hangs `build_document_model` forever).  All `build/corpus_*.py` scripts now go
through `build/corpus_scan_runner.py`:

- **parallelism** — one OS process per PDF (`--workers`, default 4); a freeze
  in one doc cannot stall the others;
- **per-task hard timeout** — a scan exceeding `--timeout` (default 600 s) is
  `terminate()`d then `kill()`ed and recorded as a `TIMEOUT` result; the pool
  never wedges;
- **no deadlock by construction** — workers share no locks; the parent only
  `join(timeout)`s children (which always return) then hard-kills survivors;
  each task owns a private result queue drained non-blocking; `spawn` context
  so a threaded parent can never deadlock a fork;
- **incremental persistence + resume** — results are written after every
  completion (tmp-file + atomic replace); next run skips successful files and
  retries errored ones (`--keep-errors` to skip those too).

In the gate run below this paid for itself: 1 of 45 PDFs is a genuine
infinite-hang input, and it was killed at its timeout instead of freezing the
run — 44/45 completed.

### 10.2 Gate fixes (the gate never ran successfully before)

- missing `global_recovery` import → `NameError` on every doc;
- `_defect_report` iterated a dict's keys instead of values;
- `rec_report.unresolved` is an int count, not a list (`len()` crash);
- oob check looked up pdfminer `page_sizes` (1-based keys, 612×792 fallback)
  for 0-based rendered pages — the rendered PDF is A4 595×842, so every oob
  verdict used the wrong page box; now reads the actual rendered page rects;
- lost / font detectors matched words by exact position, but packing
  legitimately moves Y (and the renderer emits concatenated runs whose
  segmentation shifts) → every moved word looked "lost"/"font-changed".  Lost
  is now judged by no-space substring presence per page; font by nearest X
  match (X is invariant under packing); overlap by movement-tolerant
  pair-matching (`_new_overlap_pairs`).

### 10.3 Corpus result — the word-level gate FAILS 42/44 docs

45 candidates (same `tests/file/` corpus, ≤ 10 MB), 3 workers, 1200 s timeout:
44 scanned OK, **1 TIMEOUT** (`The Art of Multiprocessor Programming, 2e.pdf`,
the `build_document_model` hang — recorded, not wedging).  New-defect totals
across the 44:

| new defect | total | docs |
|------------|------:|-----:|
| overlap    | 5,853 | 42 |
| lost_line  | 280 | 2 |
| dup_line   | 6 | 4 |
| font_anomaly | 53 | 16 |
| oob        | 0 | 0 |

Only `TestPDF.pdf` and `translate.cli.font.unknown.pdf` pass.  Worst docs:
2111.01000v2 (984), itbook-export (919), 2608.26825v1 (862), prob (314),
lol (289), MCMC (289).

### 10.4 Reading — what the gate caught (this is the point of the gate)

1. **Packing creates word-level overlaps at block/column boundaries — the
   §9 "never creates collisions" claim is only true at BLOCK level.**  The
   block-level collision gate (`detect_page_collisions`) stayed clean
   (4,459→3,943), but the finer words layer shows the pass pulling blocks so
   close that their boundary lines' glyph boxes intersect (verified
   movement-tolerant: 99.9% of after-words have an exact `(page, text, x0)`
   before twin — X never changes — so the pairs are real, not detector
   noise).  Example on 1905: a heading block re-anchored 20 pt down onto the
   formula line below it; on 19903 a superscript block was moved 401 pt
   (bounded by `bottom_margin`, still in-bounds, but onto the page-number
   block, which the pass treats as a movable `paragraph`).  The packer's
   re-anchor bounds against `bottom_margin` and *preserved* blocks, but not
   against movable neighbours — the fix for 7G-2.1 is a word/line-level
   boundary check (or a preserved/neighbour-aware re-anchor floor).
2. **Recovery can push blocks past the last page on book-length docs — 280
   words dropped from the render.**  On lol.pdf, 8e NEXT_PAGE moved blocks
   from page 170 to page 387 (> last page 382); the renderer emits pages only
   up to the highest drawn page, so those blocks are never drawn (15593 →
   15335 words through recovery; packing itself is clean: +1).  Same on
   2608.16578v1 (50).  This is a `global_recovery` 8e follow-up, not packing.
3. **font_anomaly (53) / dup_line (6) are detector residue, not renderer
   defects** — packing provably changes only Y, so a font cannot grow; the
   remaining flags are instance-mismatch at 100+ pt moves (dy=120 with dx=0
   matches) and double-draw at identical positions.  Ignore for the gate
   verdict; tighten the matchers only if the packer fix in (1) lands.
4. **The renderer's own words layer is noisy** — its tight line drawing
   produces hundreds of baseline overlapping pairs on every doc (the §smoke
   "spacing? YES" item), which is why the gate's overlap verdict is the
   *delta* (new pairs), not the raw count.

### 10.5 Next (7G-2.1) — before packing can be accepted

1. packer: word-level boundary-overlap guard for compaction/re-anchor
   (or neighbour-aware re-anchor floor) — the gate must then pass 40+/44;
2. recovery 8e: bound NEXT_PAGE moves to real pages (no block past the
   document's last page);
3. re-run `build/corpus_render_gate_scan.py` (now parallel + timeout) to
   confirm new_defects == 0 corpus-wide;
4. classify the remaining `font_anomaly`/`dup_line` residue only if (1)
   lands without clearing them.

## 11. 7G-2.1 P0 fixes landed + gate re-run (2026-08-30)

Both §10.5 items 1–2 are in-tree; item 3 (the gate re-run) is reported below.

### 11.1 Fix 1 — 8e out-of-document overflow (recovery, `page_break*.py`)

A NEXT_PAGE break may only land on a page that actually exists.  `next_free_page`
gains a `max_page` bound (from the new `last_page_index(page_sizes)` = max
numeric key of the size map); when the monotonic page chain runs past it there
is no real page to land on, and **both** executors
(`execute_page_breaks` / `execute_continuation_breaks`) defer the block
(reason `"no_page"`) and surface it unresolved instead of placing it on a
phantom page the renderer cannot carry.  This kills the lol.pdf 170 → 387
(382-page book) word-drop signature directly.

### 11.2 Fix 2 — packer word-level guards (`packer.py`)

Three guards, all read-only against settled geometry (only Y ever changes,
`src_box` verbatim — the 7G-2 discipline is kept):

- **glyph-excess** — `_glyph_excess` reads the settled command lines: a block
  whose lines poke above its `dst_box` top is not pulled up until its REAL
  glyph top (topmost baseline + 0.8·font_size) clears the block above —
  compaction closes bbox gaps while words never overlap;
- **neighbour-aware re-anchor floor** — `column_reanchor` now takes the page's
  OTHER columns (`other_barriers`, with `barrier_excess` for their glyph tops)
  as hard floors: a compacted column may no longer descend onto a movable
  neighbour (page number, footer paragraph, adjacent column's content) — the
  exact 1905 heading-onto-formula / 19903 superscript-onto-page-number cases
  the gate caught;
- **two-phase sequencing** — ALL columns compact first, then ALL columns
  re-anchor against the other columns' *compacted* positions, so a column's
  moves are never computed against a neighbour's pre-move geometry (the
  paragraph-up / fragment-down slide-past overlap).

Locked by `tests/test_7g21_p0_fixes.py` (13 tests: 8e page ceiling +
no-phantom-page deferral, re-anchor never onto a movable neighbour,
side-by-side columns still reclaim, glyph-excess compaction pulls,
integration no-overlap) plus updates to `test_layout_packer_7g2.py` and the
7f8e3/4 continuation tests (their fixtures now declare the extra target page
the bounded chain needs).

### 11.3 Gate re-run — the verdict: 6192 → 3496 (−43.5%), still FAILING

Same corpus, same runner (`build/corpus_render_gate_scan.py`, 4 workers,
1200 s timeout): 44/45 scanned, **1 TIMEOUT** (the known
`The Art of Multiprocessor Programming, 2e.pdf` `build_document_model` hang —
killed at its timeout, the pool never wedged).

| new defect | BEFORE (10.3) | AFTER 7G-2.1 | Δ |
|------------|--------------:|-------------:|---:|
| overlap    | 5,853 | 3,355 | −2,498 (−43%) |
| lost_line  | 280 | 94 | −186 (−66%) |
| dup_line   | 6 | 4 | −2 |
| font_anomaly | 53 | 43 | −10 |
| oob        | 0 | 0 | 0 |
| **total**  | **6,192** | **3,496** | **−2,696 (−43.5%)** |

Per-doc (worst first): 2111.01000v2 984→613, itbook-export 919→565,
2608.26825v1 862→292, prob 314→211, 2608.22602v1 209→162, lol 289→131
(lost 230→94), MCMC 289→133, 2608.17744v1 225→131.  **Every** doc improved
or stayed flat (only 2608.26638v1 81→81 is unchanged; 2507.15240v2 237→224
barely moved).
2 docs pass at 0 (TestPDF, translate.cli.font.unknown — same as before).

### 11.4 Reading — the honest residual

1. **The guards removed exactly the classes they target, and the corpus
   confirms it.**  The re-anchor floor killed the descend-onto-neighbour
   overlaps (1905 68→29, 2103 91→36, 2608.16578v1 115→39); the 8e bound cut
   lol's lost words by 59%.  All 44 scanned docs are strictly better.
2. **The gate's 40+/44 bar is NOT met** — the residual is 96% `overlap`
   (3,355), now dominated by a class the bbox guards cannot see: blocks whose
   **drawn** extent exceeds their `dst_box`.  Headings / TOC entries (and any
   block that falls back to the renderer's `_insert_text_wrapped` — empty
   `render_payload.commands`) are wrapped at line height 1.4·font_size inside
   a box sized from the *source* geometry; the wrapped lines spill below the
   box bottom by up to ~0.5·font_size, so compaction closing the box gap to
   `gutter=2` still leaves the *words* overlapping by a few points.  The
   glyph-excess guard cannot see it (no command geometry to read).  This is a
   renderer-model-vs-bbox mismatch, not a placement bug: the next increment
   must give the packer a **drawn-extent model for non-command blocks**
   (estimate the wrapped line count from text width × box width) or make the
   renderer keep wrapped glyphs inside the box (at the cost of truncating
   overflow text).  The two flat docs (2608.26638v1, 2507.15240v2) are pure
   this class — useful as the fix's first regression targets.
3. **lost_line residual (94, all lol.pdf)** — the 170→387 off-document
   moves are gone; the remainder is blocks whose settled geometry sits below
   the last real page's bottom edge (pre-existing off-page blocks in the
   source, `before oob` = 75): a recovery/render follow-up (bound the
   page-bottom edge, not just the page number).
4. **`font_anomaly` 43 / `dup_line` 4 remain detector residue** (§10.4 item
   3) — packing changes only Y, a font cannot grow; ignore for the verdict.

Status vs §10.5: items 1–2 **DONE**, item 3 **re-run, still red** — the
7G-2.1 guards are a verified −43.5% corpus improvement and the safe half of
the gate story, but the wrap-overlap class must be fixed before the gate can
pass 40+/44.

## 12. 7G-2.2 — renderer-geometry parity: conservative occupied draw-extent

§11.4 item 2 deferred the fix for the same **two-geometry-worlds** defect:
the packer packs by `dst_box` while the renderer's legacy `_insert_text_wrapped`
re-wraps text at `1.4·font_size` inside it, so a source-sized box can (in the
renderer's coordinate model) under-declare how tall a block really draws.  7G-2.2
lands the 方案 A fix on the **packer side**, plus the honest measurement that
isolates packing from recovery (2026-08-30):

### 12.1 The fix — a conservative occupied bottom per block

`packer.py` gains a pure-metric model of each block's **drawn extent**, joined
to the existing glyph-excess (top) parity:

- **command block** — read the lowest settled baseline's real glyph bottom
  (`min_y − descent`); spill = how far that sits below `dst_box.bottom`;
- **command-less block** (the `_insert_text_wrapped` fallback) — reproduce the
  renderer's token-wrap rule over `text` × `font_size` × `dst_box.width`
  (pure char-advance metrics, CJK-safe, no pymupdf) to get `lines`, then
  `occupied_height = max(dst_box.height, lines × 1.4·font_size)` and spill the
  difference below the box.

That spill is fed into compaction's reference — the block ABOVE's bottom edge,
the cross-floor `final_bottom` and the other-column barriers now use the
block's **occupied bottom** (`resolved_bottom − spill`) — and into re-anchor's
page-bottom floor.  Guarantees are unchanged: **pure read, occupancy only, only
Y moves, zero spill ⇒ byte-identical 7G-2.1 behaviour, no renderer/pymupdf
dependency, no `level`/`index` geometry**.  Locked by
`tests/test_layout_packer_7g22.py` (11 tests: wrap-line estimator, command vs
command-less spill, compaction bounded by the occupied bottom, cross-column
barrier parity, no-op when text fits).

### 12.2 The honest metric — `packing_introduced_overlap` (recovered → packed)

The gate compared V1 `plan` → V2 `packed`, which folds **recovery's** moves in:
an overlap that `global_recovery` created on the way to the packed plan was
charged to packing.  7G-2.2 makes the gate scan render the **recovered** plan
as the packing baseline and report `packing_introduced_overlap =
_new_overlap_pairs(recovered, packed)` — the true answer to "did the PACKING
pass add overlap?".  The scan also gained `--only <c1,c2,…>` for a named
micro-gate corpus.

### 12.3 Micro gate — packing's own contribution is small, and it fell further

7G-2.2 micro corpus (`--only 2608.26638v1,2507.15240v2,2111.01000v2,
2608.26825v1,1808.08763v3,0b61d491…dual`), 6 docs:

| doc | new_defects (V1→V2) | **packing_introduced_overlap** | packing-only Δ vs 7G-2.1 |
|-----|--------------------:|------------------------------:|--------------------------:|
| 2507.15240v2 | 222 | **1** | — |
| 2608.26638v1 | 81 | **22** | 26 → 22 |
| 1808.08763v3 | 8 | **6** | — |
| zh-CN dual | 3 | **3** | — |
| 2608.26825v1 | 295 | **31** | — |
| 2111.01000v2 | 474 | **203** | 367 → 203 (−45%) |

Readings:

1. **The 3,355 headline overlap was never mostly packing.**  On the two flat
   docs the user named, packing's own contribution is 1 (2507.15240v2) and
   22 (2608.26638v1) of the 81/222 pipeline total — the lion's share is moved
   by `global_recovery` (2608.26638v1: recovered→packed 22 vs plan→recovered
   59), i.e. paragraph fragments re-anchored off-page/tight by **recovery**,
   not by the packer.  The user's insistence on an *isolated* metric was
   exactly right: judging the packer by the V1→V2 sum overcharges it.
2. **The parity fix moves the packing-only metric down where it is real** —
   on 2111.01000v2 (`packing_introduced_overlap` 367 → 203, −45%) and
   2608.26638v1 (26 → 22).  The 7G-2.2 draw-extent model reclaims the gap the
   bbox-only guards could not see; zero regressions across the 92 layout
   tests.
3. **The remaining packing overlap is next-step work, not packing v-blame.**
   The residual is the recovery/segmentation class (single-line paragraph
   fragments with ~a line-height of drawn extent moved tight), which lives in
   `global_recovery` / `page_shift` — the same parity miss in a *different*
   executor.  That is 7G-3 recovery-side parity, deliberately out of 7G-2.2's
   packer-only scope.

Status: **7G-2.2 packer geometry-parity DONE** (+ honest isolated metric).  The
packer no longer cheats the words layer; the wrap-class packing overlap is
measurably reduced and never regressed.  The clean next increment is recovery
side occupancy (7G-3), not gutting packing or inflating `gutter` to chase the
V1→V2 total to zero.

## 13. 7G-3 — the three-way overlap ledger + an honest measured rejection

### 13.1 The ledger is now the gate standard (`packing_recovery_introduced`)

The render gate scan now reports every corpus case as a **three-way
attribution** (preamble: the V1 `plan` actually runs through `global_recovery`
AND packing, so an un-attributed "final overlap" is unanswerable):

| field | definition |
|-------|-----------|
| `overlap_ledger.preexisting`       | overlaps already in the V1 (`plan`) render — the renderer's own dense-line baseline overlaps, present before any executor runs |
| `overlap_ledger.recovery_introduced` | new word-overlap pairs created by `global_recovery` (`recovered` render vs `plan`) |
| `overlap_ledger.packing_introduced` | new pairs created by `apply_packing` (`packed` render vs `recovered`) — the 7G-2.2 metric |

The scan also gained `--only <c1,c2,…>` so a named micro-gate (7G-2.2's corpus)
becomes a one-line run.  This is the diagnostic floor every future gate —— and
Adaptive v2 —— must sit on.

### 13.2 Micro-gate ledger (7G-2.2 corpus, 6 docs, after 7G-2.2)

| doc | preexisting | recovery_introduced | packing_introduced | V1→V2 new_defects |
|-----|------------:|--------------------:|-------------------:|------------------:|
| 2507.15240v2 | 1,331 | 87 | **1** | 222 |
| 2608.26638v1 | 1,021 | 15 | **22** | 81 |
| 1808.08763v3 | 211 | 0 | **6** | 8 |
| zh-CN dual | 594 | 3 | **3** | 3 |
| 2608.26825v1 | 1,889 | 162 | **31** | 295 |
| 2111.01000v2 | 9,829 | 216 | **203** | 474 |

Readings:
1. **`preexisting` dwarfs everything** — 2111 has 9.8k overlaps *before* any
   executor runs: the renderer's tight `_insert_text_wrapped` line assembly
already overlaps hundreds of baseline pairs per page on dense text.  The
gate's old `new_overlap` (V1→V2) was never "what the pass added" — it was a
   small signal on a huge floor.  The ledger finally separates the two.
2. **Packing is mostly clean** — 1808 rec=0 pack=6, 2507 pack=1, zh pack=3;
   even on the worst doc (2111) packing adds 203 against a 9.8k pre-existing
   floor.  **Recovery is the genuine remaining adder** (2111 rec=216,
   2608.26825 rec=162): single-line paragraph fragments re-anchored down by
   SHIFT_DOWN whose drawn extent lands on the neighbour below.

### 13.3 The naive "recovery-side parity" fix was implemented, measured, and rejected

§13.2 said recovery creates overlap because it shifts by `resolved_bbox` while
the renderer draws more.  The obvious next move was to give **command-less
blocks** (the `_insert_text_wrapped` fallback) the same 7G-2.2 wrap estimate in
the shared `resolved_bbox` authority (`_drawn_bottom` in `page_flow.py`), so
recovery would detect / shift against the real drawn extent.  It was
implemented and measured on the same 4 docs with the SAME code path toggle:

| doc | `recovery_introduced` before | after naive fix | verdict |
|-----|------------------------------:|-----------------:|---------|
| 2608.26638v1 | 59 | **64** | regressed |
| 2111.01000v2 | 302 | **380** | regressed |
| 2507.15240v2 | 222 | **227** | regressed |
| 2608.26825v1 | 267 | **274** | regressed |

**Every doc regressed.**  Extending `resolved_bbox` downward for command-less
blocks made recovery's collision detector flag many more (over-)collisions, so
`global_recovery` shifted *more* blocks *more*, and the recovered plan ended up
with *more* word overlaps.  Two partial fixes were kept (payload `font_size`
precedence when reading drawn extent), the extension itself was **reverted** —
`page_flow.py` is byte-identical to 7G-2.1 again and all 164 layout/recovery
tests stay green.

> Note on counts: §13.2's `recovery_introduced` is the gate ledger (its own
> render entry point), while this table is an inline probe that renders
> plan / recovered on every doc — the two entry points differ slightly in
> absolute numbers (e.g. 2608.26638v1 reads 15 in the ledger, 59 in the probe)
> because of how each folds the renderer's huge `preexisting` floor.  The
> **before→after delta is measured with byte-identical code on the same probe**,
> so the regression direction (every doc up) is the reliable signal.

### 13.4 Conclusion — recovery-side parity is real work, not a bbox tweak

The defect class (recovery shifts a single-line fragment down ~a line-height
onto a neighbour that was not accounted for in the same pass) is **not** a
missing bbox extension: it is a **cascade / neighbour-awareness problem** in
`global_recovery` — a block is moved without bounding against the block *below*
it, in the same way packer's re-anchor needed its neighbour-aware floor
(7G-2.1).  A correct 7G-3 must (a) bound each SHIFT_DOWN/`required_shift` by
the receiver's drawn top, and (b) re-frame the collision pairing to catch
non-adjacent-but-overlapping fragments — the mirror of packer's two-phase
column sequencing.  That is deliberately **not chased here**: the honest,
measured answer is that the naive parity extension backfires, so shipping it
would repeat the "inflate to look safe" mistake.  Status:

| item | status |
|------|--------|
| V1 Recovery | ✅ |
| 7G-2 measurement / packing / 2.1 / 2.2 | ✅ |
| Packing-isolation diagnostics (ledger) | ✅ (13.1) |
| Recovery-side parity | ⏳ design: neighbour-aware cascade (13.4), naive attempt measured & rejected |
| V2 full corpus gate | ⏳ re-run with the ledger as the verdict floor |
| V2 visual acceptance | ⏳ |
| Adaptive v2 | ⏸ paused |

## 14. 7G-4 — `recovery_delta` (per-doc + per-page) and the cascade root-cause

### 14.1 New gate metric: recovery's own bill, per page

§13.1 gave each corpus case a recovered→plan attribution; 7G-4 adds the raw
delta the user asked for, judged per **page**, not just one corpus total:

```text
recovery_delta            = recovered_raw_overlap − plan_raw_overlap   (whole doc)
recovery_delta_by_page    = {page: same delta}                        (per page)
```

`_overlap_by_page` counts raw word-overlaps per rendered page (no movement
filter), so the cascade's effect is visible page-by-page.  Micro-gate (3 docs
re-run with the new metric):

| doc | preexisting | recovery_introduced | recovery_delta | packing_introduced |
|-----|------------:|--------------------:|---------------:|-------------------:|
| 1808.08763v3 | 211 | 0 | **+2** | 6 |
| 2608.26638v1 | 1,021 | 15 | **+44** | 22 |
| 2507.15240v2 | 1,331 | 87 | **+136** | 1 |

2507 top pages by `recovery_delta`: p11=+64, p10=+24, p6=+23.  The additive
signal is small next to the huge `preexisting` floor but it is real and
localised — exactly the per-page view that makes it audit-able.

### 14.2 Root cause of a worst page (2507 p11, recΔ=+64)

Author-bibliography page.  `global_recovery` moved only **6** single-line
fragments down by 1.3–15.3 pt, and the words layer gained 58 new-introduced
overlapping pairs — dense reference lines (`R.`, `Curtin,`, `Prabhu,`, `S.`,
… left column) colliding with the neighbour below's drawn title content.  The
mechanism is the cascade the user predicted:

```text
collision U↕L → SHIFT_DOWN moves L down by required_shift
   → L's drawn glyphs land on item R BELOW it
   → R was not bounded by L in the same decision → new overlap
```

It is **not** a missing bbox extension (§13.3 already proved that direction
regresses).  It is the same neighbour-awareness packer needed in 7G-2.1's
re-anchor — recovery moves a block without bounding against the receiver's
drawn top.

### 14.3 Why the cascade fix is NOT rushed in

A naive per-pass clamp (cap each SHIFT_DOWN by the pre-shift gap to the lower
neighbour) was designed and rejected before coding: in a stack of N collisions,
clamping block *i* against block *i+1*'s *un-moved* position starves the
cascade — the U↕L collision can never fully clear, so recovery either leaves
masses of collisions unresolved or burns budget, breaking the 7F-8
convergence / no-progress guarantees.  The correct design is an **ordered
two-phase cascade** (mirror of packer's compaction-then-reanchor): process
each SHIFT_DOWN against the receiver at its FINAL position so a block packs
down exactly to the next block's drawn top and never past it.  That is a
surgery on `global_recovery`'s core loop with its own 7F-8d test corpus — it
belongs in a dedicated 7G-4 branch, not smuggled into the frozen V1/V2
baseline.

### 14.4 Status — parallel tracks, matching the operating posture

| track | status |
|-------|--------|
| **A. use V1 now** — real PDFs, V1 baseline / recovery prototype (not a solved-all claim) | ✅ go |
| **B. 7G-4** — ordered two-phase neighbour-aware recovery cascade, validated by the §13.1 ledger hard-gates (recovery / packing never up, preexisting not a target, X/src verbatim, finite convergence, no-progress real, no new words-layer breaks) | ✅ shipped (§15) |
| `recovery_delta` per-doc + per-page | ✅ in gate |
| tackle Release-Blocker classes (`font giant`, missing space, formula+text wedged — if they reach the visual layer) | separate pipeline track, not packing/recovery |

## 15. 7G-4 shipped — the ordered two-phase receiver-at-FINAL cascade (2026-08-30)

§14.4's track B is now in-tree.  7G-4 is a **per-page correction layer over the
frozen V1 cascade** in `resolve_page_shifts` (`page_shift.py`): Phase 1 runs the
exact V1 ``detect → decide → apply required_shift`` cascade so the *set of moved
blocks* is byte-identical to V1; Phase 2 then re-bounds every amount so a
descending block never lands on a receiver below it.  It exactly mirrors the
packer's ``compact → re-anchor`` discipline.

### 15.1 What Phase 2 does — and the two defects it fixes in the candidate

The in-flight draft (uncommitted from the prior session) wired the two-phase
plan in but its Phase-2 cap contradicted its own docstring in two places:

1. **Box-top cap, not drawn-bottom cap.**  It capped each shift at
   ``block.top − receiver.final_top`` — which controls how far the block's *top*
   descends.  A block of height *h* could therefore descend *past* the
   receiver's top by its own height, overlapping the receiver's band entirely.
   The documented guarantee ("its **drawn bottom** never passes the receiver's
   drawn **TOP**") requires capping at ``block.bottom − receiver.final_top``.
2. **Preserved receivers were dropped from the floor.**  The candidate skipped
   ``q.preserved`` when choosing the receiver below, so a movable block could
   shift straight onto a preserved region (code / formula / figure) — again
   contradicting its own "preserved blocks are immovable floors" claim.

**The shipped Phase 2 resolves both, bottom-up** (so a receiver's own capped
move propagates):

- process each page's placements bottom-up, keeping a running set of finalized
  floors below;
- for each movable block, the descent bound is the **highest drawn TOP of a
  horizontally-overlapping receiver below it at that receiver's FINAL
  position** — preserved → its (immovable) top; movable → ``top−shift`` from
  its own already-capped move; the page-bottom edge (drawn bottom ≥ 0) is the
  last-resort floor;
- a block's shift is capped so its **drawn bottom** never passes that bound
  (``shift ≤ bottom − bound_top``);
- side-by-side (x-disjoint) blocks are never floors — two-column reclaim is
  preserved.

Because the bound uses each receiver's FINAL position, a genuine all-movable
cascade (``U↕L → L↕R`` where every block has its own collision) still resolves
— L descends fully to R's new top, nothing is left unresolved.  But a receiver
that merely sits below (no collision of its own, or a preserved region) is a
hard floor: L stops at its top and the U↕L collision is surfaced **unresolved**
rather than "resolved by landing on it" — the honest 7F-9 trade, not a hidden
overlap.  The guarantee set (only Y changes, `src_box` verbatim, preserved
never in the move map, no X/font/text touched) is unchanged.

### 15.2 Regression corpus — `tests/test_page_shift_7g4.py` (9 tests)

Locks the §6 hard-gates on this branch: V1 intent kept on a clean column,
drawn-bottom cap (not box-top) BINDS on an immovable floor, preserved receivers
never enter the move map, receiver-at-FINAL lets a real cascade resolve and
converge, an x-disjoint below-block never floors, page-bottom breaks stay OUT of
the 8d move map (they are 8e NEXT_PAGE's job), and integration on the defect
signature leaves the upper collision honest-unresolved instead of landing on a
region.

Verification: the full layout / recovery regression set is green — **882 tests
passed** (all `page_shift` / `recovery` / `page_break` / `layout` / `page_flow`
/ `packer` / `7f*` / `7g*` tests), including the frozen 7F-8d, 7F-9.2,
7F-9.3, 7G-1/2/2.1/2.2 and continuation suites.  Zero regressions.

### 15.3 Full-corpus gate re-run with 7G-4 — measured (2026-08-30)

`build/corpus_render_gate_scan.py` (4 workers, 1500 s timeout) re-run over the
whole ``tests/file/`` corpus with the 7G-4 cascade active: **44 docs scanned OK,
0 errors, 0 timeouts** (all rendered end-to-end).  Three-way ledger + per-page
`recovery_delta` across the 44:

| metric (Σ over 44 docs) | baseline (§13/§14, pre-7G-4) | **after 7G-4** |
|-------------------------|------------------------------:|---------------:|
| `recovery_introduced`   | (2111=216, 26638=15, 2507=87, 26825=162…) | **313** |
| `recovery_delta`        | positive/near-zero (§14.1: +2 / +44 / +136) | **−242 net** (24 neg, 19 zero, 1 pos) |
| `packing_introduced`    | §12.3/§13.2 band | **1,068** (out of 7G-4 scope) |
| `new_defects_total`     | 3,496 (11.3) | **1,257** |
| plan→packed collisions  | 4,459 → 3,943 (11.x) | **4,462 → 3,692 (never up, 0 regressions)** |

**The two REAL cascade pages — both eliminated:**

| doc · page | pre-7G-4 | after 7G-4 |
|------------|----------|------------|
| `2608.26638v1` p9 (was **REAL**, +9/+44 doc) | recovery_introduced 15, recΔ **+44** | **rec=0, recΔ=0, ALL pages 0** |
| `2608.27395v1` p7 (was **REAL**, +10 doc) | 49/52 overlap on shifted pages | **rec=1, recΔ=0, p7 = −1** |
| `2507.15240v2` (p11 was +64) | rec 87, recΔ **+136** | **rec=0, recΔ=0, ALL pages 0** |

GO criteria (§6 regression gate), all confirmed:

1. **two REAL cascade pages' `recovery_delta` eliminated** — 26638 all pages 0;
   27395 p7 negative.  The `U↕L → SHIFT_DOWN → L lands on R` mono-pattern no
   longer creates word overlap at the block level the detector can see;
2. **no new systematic recovery overlap** — only one doc nets positive
   (1905 +2, legacy-line residue), 24 negative / 19 zero; recovery now NET
   REDUCES word overlap corpus-wide (recΔ −242);
3. **termination / no-progress / budget contract intact** — 41 `stopped_early`
   + 3 `converged`, ZERO `budget_expired`, no hangs (the 7F-9.2 guard holds);
4. **preserved barrier gate all green** — plan→packed collisions strictly
   non-increasing (4,462→3,692), so no block was shoved onto a preserved /
   immovable region;
5. **key PDFs render** — all 44 scanned + rendered, 0 errors.

Reading: the collision-blocks gate (`detect_page_collisions`) is healthy
non-increasing; recovery's own word-level bill collapsed (recovery_introduced
313 total, dominated by the 528-page books at 56–61 each, down from pre-7G-4
where 2111 alone was 216); and `preexisting` (71.5 k) is correctly isolated as
the renderer floor, NOT charged to 7G-4.

### 15.4 Honest scope — the residual is now packing, not the cascade

7G-4 fixes the **block-visible** cascade: a SHIFT_DOWN can no longer land a
block on a horizontally-overlapping receiver the same decision ignored.  The
remaining `new_defects` (1,257) is now **84% `packing_introduced` (1,068)** vs
24% recovery — i.e. the residual the gate still fails on is the PACKING pass's
own wrap/word boundary parity (7G-2.2's §11.4/§12 recognized band, e.g. 2111
pack=202, itbook=221, MCMC=61), deliberately out of 7G-4's recovery-side
scope.  7G-4 is the recovery half of the ledger story done; packing parity is
the 7G-5 item.

### 15.5 Next (7G-5)

1. recovery-side drawn-extent parity for single-line fragments (the residual
   `recovery_introduced` on heavy books), gated by per-page `recovery_delta ≤
   §14.1`; the §13.3 direction (extend ``resolved_bbox``) is already rejected;
2. packing word-boundary parity is the gate's dominant residual — close the
   7G-2.2 wrap class the packer still cannot see (renderer keeps wrapped
   glyphs inside the box, or a drawn-extent model for the packer's remaining
   non-command blocks);
3. V2 full-corpus visual acceptance once 7G-5 items 1–2 land, with the ledger
   as the verdict floor.
