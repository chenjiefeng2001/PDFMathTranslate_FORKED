"""CLI — dual-track ingestion comparison: existing backend vs Marker JSON.

Usage::

    python -m pdf2zh.v3.ingestion --pdf book.pdf --marker-json book.json [--out diff.json]

Runs the existing pdfminer backend on ``--pdf``, ingests the Marker JSON
(offline, no models required) and prints the INGESTION_DIFF.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pdf2zh.v3.ingestion",
        description="Compare the existing pdfminer ingestion with a Marker JSON output.",
    )
    ap.add_argument("--pdf", required=True, help="path to the source PDF")
    ap.add_argument(
        "--marker-json",
        required=True,
        help="path to marker.json (marker output_format=json)",
    )
    ap.add_argument(
        "--marker-version", default=None, help="marker revision to record (e.g. v2.0.0)"
    )
    ap.add_argument(
        "--out", default=None, help="write the INGESTION_DIFF JSON to this path"
    )
    ap.add_argument(
        "--pages",
        type=int,
        default=None,
        help="cap on parsed pages for the existing backend",
    )
    args = ap.parse_args(argv)

    from pdf2zh.v3.ingestion import ExistingBackend, MarkerBackend
    from pdf2zh.v3.ingestion.comparator import compare

    existing = ExistingBackend(max_pages=args.pages).ingest(args.pdf)
    print(
        f"existing backend: {existing.page_count} pages, {existing.block_count} blocks"
    )
    marker = MarkerBackend(marker_version=args.marker_version).ingest_json(
        args.marker_json, pdf_path=args.pdf
    )
    print(f"marker backend:   {marker.page_count} pages, {marker.block_count} blocks")

    diff = compare(existing, marker)
    print()
    print(diff.render_text())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(diff.to_dict(), fh, ensure_ascii=False, indent=1)
        print(f"\ndiff json written to {args.out}")
    return 0 if diff.max_severity is None else 1


if __name__ == "__main__":
    sys.exit(_main())
