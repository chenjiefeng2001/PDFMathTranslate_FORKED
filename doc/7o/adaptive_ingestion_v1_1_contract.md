# Adaptive Ingestion v1.1 — Frozen Contract

> Status: **feature complete / frozen for interface** (2026-09-03).
> No new backend-specific quality heuristics, no GUI work — this layer is
> closed until a real corpus blind spot is promoted to a backend-agnostic
> invariant.

## 1. Scope

PDF understanding is behind one canonical `IngestDocument`
(`pdf2zh/v3/ingestion/ir.py`). Every backend (MinerU/magic-pdf, Marker,
the existing pdfminer path) adapts into it, so translate → plan → fixup →
render → raster never know which backend produced the blocks. Each block
carries provenance (`source_backend` / `source_id`) and **declared**
coordinate semantics; a bare tuple of numbers is never treated as geometry.

```
              ┌─ MinerU raw telemetry (ingest.raw.*)
              │
PDF ──► ingestion ──► canonical IR ──► quality gate ──┐
              │                                        │
              └─ Marker ───────────────────────────────┤
                                                       ▼
                                                   Selector
                                                       │
                                                       ▼
                                       translate → plan → fixup
                                                       │
                                                render → erase
                                                       │
                                                    raster
                                                       │
                                                    audit
                                                       │
                                                first_divergence
                                                       │
                                                    explain
```

## 2. Frozen semantics

### 2.1 `ingest.select` is the FINAL selection — never "what was attempted"

The decision payload must always answer "which backend actually served this
file, and why", and must never claim a backend that did not serve:

| field                  | meaning                                                          |
|------------------------|------------------------------------------------------------------|
| `requested_backend`    | `auto` / `mineru` / `marker` (what the user asked for)           |
| `selected_backend`     | **final** serving backend — never a failed fallback candidate    |
| `candidates`           | candidate order (primary first)                                  |
| `fallback`             | a non-primary backend was selected                               |
| `fallback_attempted`   | a fallback run was attempted (decision-level truth)              |
| `fallback_succeeded`   | the attempted fallback run actually served (run-level truth)     |
| `reason`               | one of the codes below — never fabricated                        |
| `quality`              | the *selected* run's own gate result                             |
| `primary_backend`      | the primary (== `fallback_from` semantics for fallback stories)  |
| `failed_rules`         | canonical invariants the primary broke (the "why fall back?")    |

**Hard rule**: a failed fallback run must keep `selected_backend = primary`
with `reason = fallback_ingest_failed`, `fallback_attempted = true`,
`fallback_succeeded = false`. It is forbidden to emit
`selected_backend = marker` for a Marker run that failed — qualification /
explain would misread an attempt as a success.

### 2.2 reason codes (closed set)

| code                           | meaning                                            |
|--------------------------------|----------------------------------------------------|
| `forced_backend`               | user forced this backend; no fallback ever         |
| `primary_ingest_pass`          | auto; primary passed the gate                      |
| `primary_ingest_quality_fail`  | auto; primary failed the gate → fallback selected  |
| `primary_ingest_parse_fail`    | auto; primary crashed → fallback selected          |
| `primary_failed_no_fallback`   | auto; primary failed, no fallback available        |
| `fallback_ingest_failed`       | fallback attempted but failed → primary retained   |

### 2.3 Quality gate = canonical invariants only

Only backend-agnostic invariants gate the fallback
(`pdf2zh/v3/ingestion/rules.py`): geometry must be declared
(`INGEST_GEOMETRY_DECLARED`) and Marker geometry must be normalized into v3
(`MARKER_GEOMETRY_NORMALIZED`). Per-backend heuristics (block counts,
confidence thresholds, …) are backend policy and never live in the gate or
in `trace_rules`.

### 2.4 Raw events are evidence, never verdicts

`ingest.raw.begin / block / end` (`pdf2zh/v3/ingestion/base.py`) record the
raw MinerU facts before canonicalization — `source_backend`, `source_id`,
raw kind, raw bbox with declared semantics (`space=page_tl, origin=top-left,
unit=pt`), page dimensions, `normalized` flag and `normalization_reason`
(`page_height_missing` / `box_missing`). Raw and canonical blocks share
`trace_id` (`p{page}_{i}`), so explain can answer "did MinerU itself lack
geometry, or did the adapter drop it?" — without a second rule set.

### 2.5 Pipeline stage order (first_divergence ordering)

```
ingest → normalize → translate → plan → fixup → layout → render → erase → raster
```

An ingest FAIL therefore outranks every later stage as `first_divergence`
(verified by `test_first_divergence_ranks_ingest_before_plan` and the audit
tests).

## 3. The six routing scenarios (frozen expectations)

| # | scenario                         | expected trace story                                              |
|---|----------------------------------|-------------------------------------------------------------------|
| 1 | normal MinerU (auto)             | raw → canonical PASS → `selected=mineru, reason=primary_ingest_pass` |
| 2 | MinerU quality FAIL              | raw → canonical FAIL → Marker PASS → `selected=marker, fallback_from=mineru, reason=primary_ingest_quality_fail` |
| 3 | MinerU parse crash               | `ingest.begin mineru` + `end FAIL` → Marker PASS → `selected=marker, reason=primary_ingest_parse_fail` |
| 4 | Marker fallback crash            | mineru FAIL → marker FAIL → engine legacy/BabelDOC degrade — **both failures retained in trace**, no `ingest.select` |
| 5 | forced Marker                    | Marker PASS/FAIL → `selected=marker, reason=forced_backend`       |
| 6 | forced MinerU                    | MinerU PASS/FAIL → `selected=mineru, reason=forced_backend`, never auto-fallback |

Invariant chain every trace must preserve:

```
raw ingest → canonical ingest → quality decision → fallback attempt
→ actual fallback result → ingest.select → first_divergence → engine degrade (if any)
```

## 4. CLI surface (frozen)

```
--ingest-backend {auto,mineru,marker}   default auto
--marker-json PATH                       offline Marker JSON ingestion
--marker-version TAG                     provenance tag for Marker runs
--trace                                  record flight-recorder trace + audit (default off)
--trace-dir DIR                          trace/ + audit/ root (default: output dir)
--log-file PATH                          rotating runtime log (env fallback PDF2ZH_LOG_FILE)
```

## 5. Acceptance evidence

- **Tests**: 352 pass across the 15 affected suites (ingestion IR/backends/
  comparator/rules/selector, CLI wiring, task log channel, flight recorder,
  trace audit, v3, services, magicpdf CLI).
- **Real corpus (this session, `doc/7o/ingestion_corpus_probe.py`)**: 3 PDFs
  from `tests/file` (2× 1-page + 1× 17-page paper) × scenarios 1 & 6 —
  6/6 runs reconstructed the expected story; raw block count == canonical
  block count per page; `ingest.select` semantics correct; audit
  qualification PASS with 0 rule fails on every run (including 980-event /
  184-block / 534-plan-event 17-page book).
- **Scenarios 2–5** require Marker (vendored at `vendor/marker`, not pip-
  installed in this environment) and are covered by unit tests
  (`test_cli_auto_quality_fail_falls_back_to_marker`,
  `test_cli_parse_crash_falls_back_to_marker`,
  `test_cli_parse_crash_marker_fails_degrades_with_trace`,
  `test_cli_auto_fallback_failure_keeps_mineru`).