"""pdf2zh.v3.ingestion — pluggable PDF-understanding backends into one canonical IR."""

from pdf2zh.v3.ingestion.adapter import (
    existing_pages_to_document,
    marker_json_to_document,
)
from pdf2zh.v3.ingestion.base import (
    BACKEND_EXISTING,
    BACKEND_IDS,
    BACKEND_JINA,
    BACKEND_MARKER,
    BACKEND_MINERU,
    INGEST_REQUEST_CHOICES,
    IngestionBackend,
    IngestionBackendUnavailable,
    IngestionError,
    JinaOcrBackendUnavailable,
    JinaOcrCoverageError,
    JinaOcrDeviceError,
    JinaOcrError,
    JinaOcrModelUnavailable,
    JinaOcrOfflineError,
    JinaOcrSchemaError,
    JinaOcrTimeoutError,
    JinaOcrWorkerError,
)
from pdf2zh.v3.ingestion.bridge import (
    ingest_document_to_pages,
    model_from_ingest_document,
)
from pdf2zh.v3.ingestion.comparator import IngestionDiff, compare
from pdf2zh.v3.ingestion.config import (
    AUTO_CANDIDATES,
    JINA_MODEL_ID,
    JINA_PROMPT,
    JINA_REVISION,
    JinaOcrOptions,
    normalize_ingest_backend,
)
from pdf2zh.v3.ingestion.existing_backend import ExistingBackend
from pdf2zh.v3.ingestion.ir import (
    IngestBlock,
    IngestBox,
    IngestDocument,
    IngestPage,
)
from pdf2zh.v3.ingestion.jina_adapter import (
    JinaSemanticBlock,
    jina_result_to_document,
    parse_markdown,
    split_markdown,
)
from pdf2zh.v3.ingestion.jina_backend import JinaOcrBackend, JinaOcrInputError
from pdf2zh.v3.ingestion.marker_backend import MarkerBackend
from pdf2zh.v3.ingestion.selector import (
    IngestionDecision,
    REQUEST_AUTO,
    decide,
    gate_quality,
)

__all__ = [
    "BACKEND_EXISTING",
    "BACKEND_MARKER",
    "BACKEND_MINERU",
    "BACKEND_JINA",
    "BACKEND_IDS",
    "INGEST_REQUEST_CHOICES",
    "AUTO_CANDIDATES",
    "JINA_MODEL_ID",
    "JINA_REVISION",
    "JINA_PROMPT",
    "JinaOcrOptions",
    "normalize_ingest_backend",
    "REQUEST_AUTO",
    "IngestionDecision",
    "decide",
    "gate_quality",
    "IngestBox",
    "IngestBlock",
    "IngestPage",
    "IngestDocument",
    "IngestionDiff",
    "IngestionError",
    "IngestionBackend",
    "IngestionBackendUnavailable",
    "ExistingBackend",
    "MarkerBackend",
    "JinaOcrBackend",
    "JinaOcrInputError",
    "JinaOcrBackendUnavailable",
    "JinaOcrError",
    "JinaOcrModelUnavailable",
    "JinaOcrCoverageError",
    "JinaOcrDeviceError",
    "JinaOcrOfflineError",
    "JinaOcrSchemaError",
    "JinaOcrTimeoutError",
    "JinaOcrWorkerError",
    "JinaSemanticBlock",
    "jina_result_to_document",
    "parse_markdown",
    "split_markdown",
    "existing_pages_to_document",
    "marker_json_to_document",
    "ingest_document_to_pages",
    "model_from_ingest_document",
    "compare",
]
