# Release Notes — Adaptive Ingestion v1.1

> Draft for the release owner. Feature-complete and interface-frozen; full
> contract in `doc/7o/adaptive_ingestion_v1_1_contract.md`.

## What this release delivers

* **`auto` is now the default ingestion backend** for the magicpdf engine —
  MinerU/magic-pdf stays the primary parser; Marker becomes an explainable
  fallback instead of a manual switch.
* **MinerU and Marker share one canonical `IngestDocument`** — downstream
  (translate → plan → fixup → render → audit) never knows which backend
  produced the blocks, and every block carries provenance plus declared
  coordinate semantics.
* **Raw + canonical ingest evidence in the Flight Recorder** —
  `ingest.raw.*` captures the raw MinerU facts (bbox, kind, page size,
  normalization status) *before* canonicalization; `ingest.block` carries
  the post-adapter `v3_box`. `trace_audit explain` can now answer whether
  MinerU itself lacked geometry or the adapter dropped it.
* **Both quality failures and parse failures fall back to Marker** — a
  MinerU run that fails the canonical ingest gate, or crashes outright,
  automatically retries with Marker (live or `--marker-json`) and records
  `reason=primary_ingest_quality_fail` / `reason=mineru_parse_failed` in
  the trace. Parse failures are never disguised as quality failures.
* **Fallback failure never fakes success** — if the Marker fallback run
  itself fails, `ingest.select` keeps `selected_backend=mineru` with
  `fallback_attempted=true / fallback_succeeded=false /
  reason=fallback_ingest_failed`; the failed chain (mineru FAIL → marker
  FAIL → engine legacy/BabelDOC degrade) stays fully in the trace.
* **Ingestion failure can be the `first_divergence`** — the pipeline stage
  order now starts at `ingest`, so a parser that lost coordinates is
  qualified at the ingest layer instead of silently poisoning the renderer.
* **Six routing/degrade scenarios are covered** — normal MinerU, quality-
  fail fallback, parse-crash fallback, fallback crash, forced Marker,
  forced MinerU (see contract §3).

## Verification performed

| check | result |
|-------|--------|
| Full affected test suites | 352 passed |
| Real corpus (3 PDFs from `tests/file` × auto/forced-mineru) | 6/6 stories reconstructed, audit qualification PASS, 0 rule fails |
| Marker-dependent scenarios (2–5) | unit-tested; live run blocked by Marker not being installed (`vendor/marker` submodule present) |

## Known limitation

**Marker is not installed in this environment** — live Marker conversion
requires `pip install -e vendor/marker` plus its model weights (multi-GB
first-run download). Until then, `--ingest-backend marker` and the auto
fallback paths are covered by tests but not exercised on a live Marker run;
offline `--marker-json` ingestion is pure-Python and always available.