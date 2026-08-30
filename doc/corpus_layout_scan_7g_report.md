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
