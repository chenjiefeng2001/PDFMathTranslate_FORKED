"""pdf2zh.v3.ingestion — pluggable PDF-understanding backends into one canonical IR.

Two backends implement the same :class:`~.base.IngestionBackend` protocol
and both return :class:`~.ir.IngestDocument`:

- :class:`~.existing_backend.ExistingBackend` — the v3 pdfminer/canonical-page path;
- :class:`~.marker_backend.MarkerBackend` — datalab-to/marker (``vendor/marker``),
  offline JSON ingestion or live in-process conversion.

Downstream code never asks "was this Marker or the old parser?" — it only
knows the canonical IR, and every block carries provenance
(``source_backend`` / ``source_id``) plus declared coordinate semantics.

CLI::

    python -m pdf2zh.v3.ingestion --pdf book.pdf --marker-json book.json

runs both backends and prints the ``INGESTION_DIFF``.
"""

from pdf2zh.v3.ingestion.adapter import (
    existing_pages_to_document,
    marker_json_to_document,
)
from pdf2zh.v3.ingestion.base import (
    BACKEND_EXISTING,
    BACKEND_MARKER,
    BACKEND_MINERU,
    IngestionBackend,
    IngestionBackendUnavailable,
    IngestionError,
)
from pdf2zh.v3.ingestion.selector import (
    IngestionDecision,
    REQUEST_AUTO,
    decide,
    gate_quality,
)
from pdf2zh.v3.ingestion.comparator import IngestionDiff, compare
from pdf2zh.v3.ingestion.existing_backend import ExistingBackend
from pdf2zh.v3.ingestion.ir import (
    IngestBlock,
    IngestBox,
    IngestDocument,
    IngestPage,
)
from pdf2zh.v3.ingestion.marker_backend import MarkerBackend

from pdf2zh.v3.ingestion.bridge import (
    ingest_document_to_pages,
    model_from_ingest_document,
)

__all__ = [
    "BACKEND_EXISTING",
    "BACKEND_MARKER",
    "BACKEND_MINERU",
    "REQUEST_AUTO",
    "IngestionDecision",
    "decide",
    "gate_quality",
    "IngestBox",
    "IngestBlock",
    "IngestPage",
    "IngestDocument",
    "IngestionDiff",
    "IngestError",
    "IngestBackend",
    "IngestBackendUnavailable",
    "ExistingBackend",
    "MarkerBackend",
    "existing_pages_to_document",
    "marker_json_to_document",
    "ingest_document_to_pages",
    "model_from_ingest_document",
    "compare",
]
