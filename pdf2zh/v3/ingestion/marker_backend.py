"""MarkerBackend — datalab-to/marker (v2, vendored at ``vendor/marker``) as an
ingestion backend.

Two entry points:

- :meth:`MarkerBackend.ingest_json` — offline conversion of an already
  produced ``marker.json`` (JSONOutput schema, ``marker_single x.pdf
  --output_format json``) into the canonical :class:`IngestDocument`.  Pure
  Python + stdlib; this is the deterministic, testable path.
- :meth:`MarkerBackend.ingest` — live run: executes Marker's own
  ``PdfConverter`` with the ``JSONRenderer`` in-process and converts the
  result.  Requires Marker installed (``pip install -e vendor/marker``) plus
  its model weights; guarded so importing this module never depends on them.

Coordinate policy (see ``pdf2zh/v3/ingestion/adapter``): Marker JSON bboxes
are page-image pixels, top-left origin, y down.  They are recorded verbatim
as ``IngestBox(space="marker_image")`` and additionally projected into v3
(PDF points, lower-left, y up) only when the real PDF page size is known —
never guessed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from pdf2zh.v3.ingestion.adapter import marker_json_to_document, read_pdf_page_sizes
from pdf2zh.v3.ingestion.base import (
    BACKEND_MARKER,
    IngestionBackendUnavailable,
    IngestionError,
    emit_ingest_events,
)
from pdf2zh.v3.ingestion.ir import IngestDocument

log = logging.getLogger(__name__)

MARKER_RENDERER = "marker.renderers.json.JSONRenderer"


class MarkerBackend:
    """``pdf / marker.json -> IngestDocument`` with full provenance."""

    name: str = BACKEND_MARKER

    def __init__(self, marker_version: Optional[str] = None) -> None:
        #: pinned marker revision (submodule commit/tag) when known.
        self.marker_version = marker_version

    # ── live run (needs marker + models installed) ────────────────────

    def ingest(
        self,
        pdf_path: str,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> IngestDocument:
        """Run Marker in-process on ``pdf_path`` and convert its JSON output."""
        try:
            import marker  # noqa: F401  -- presence check only
        except Exception as exc:  # noqa: BLE001
            raise IngestionBackendUnavailable(
                "marker is not importable. Install it from the vendored "
                f"submodule first (cd vendor/marker && pip install -e .): {exc}"
            ) from exc
        try:
            from marker.config.parser import ConfigParser  # noqa: F401
            from marker.converters.pdf import PdfConverter
            from marker.models import create_model_dict
        except Exception as exc:  # noqa: BLE001
            raise IngestionBackendUnavailable(
                f"marker import failed (submodule out of sync?): {exc}"
            ) from exc

        rendered = None
        try:
            models = create_model_dict()
            converter = PdfConverter(
                config={"output_format": "json"},
                artifact_dict=models,
                renderer=MARKER_RENDERER,
            )
            rendered = converter(pdf_path)
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- marker/model failure is a real, reported error
            raise IngestionBackendUnavailable(
                f"marker conversion failed for {pdf_path!r}: {exc}"
            ) from exc

        try:
            payload = json.loads(
                rendered.model_dump_json(exclude=["metadata"], indent=2)
            )
            metadata = getattr(rendered, "metadata", None)
            if isinstance(metadata, dict):
                payload["metadata"] = metadata
        except Exception as exc:  # noqa: BLE001
            raise IngestionBackendUnavailable(
                f"marker JSON payload malformed: {exc}"
            ) from exc
        doc = self._convert_payload(payload, pdf_path=pdf_path)
        emit_ingest_events(doc, trace, pdf_path=pdf_path)
        return doc

    # ── offline path (deterministic, no models) ───────────────────────

    def ingest_json(
        self,
        json_source: Any,
        pdf_path: Optional[str] = None,
        trace: Optional[Any] = None,
        *,
        title: str = "",
    ) -> IngestDocument:
        """Convert an existing Marker JSON output (path or parsed dict)."""
        data, meta = self._load_json(json_source)
        doc = self._convert_payload(data, pdf_path=pdf_path, title=title)
        if isinstance(meta, dict) and meta:
            doc.set_env(marker_json_metadata=meta)
        emit_ingest_events(doc, trace, pdf_path=pdf_path or "")
        return doc

    # ── internals ─────────────────────────────────────────────────────

    def _convert_payload(
        self,
        data: Dict[str, Any],
        *,
        pdf_path: Optional[str] = None,
        title: str = "",
    ) -> IngestDocument:
        pdf_sizes = None
        if pdf_path and os.path.exists(pdf_path):
            try:
                pdf_sizes = read_pdf_page_sizes(pdf_path)
            except (
                Exception
            ) as exc:  # noqa: BLE001 -- normalization is optional, never fatal
                log.debug("marker_backend: page sizes unavailable: %s", exc)
        elif pdf_path:
            raise IngestionError(f"cannot open pdf {pdf_path!r} for page sizes")
        doc = marker_json_to_document(
            data, pdf_page_sizes=pdf_sizes, title=title or (pdf_path or "")
        )
        doc.set_env(pdf_path=pdf_path or "", backend="datalab-to/marker")
        if self.marker_version:
            doc.set_env(marker_version=self.marker_version)
        return doc

    @staticmethod
    def _load_json(json_source: Any) -> tuple:
        if isinstance(json_source, (str, os.PathLike)):
            path = os.fspath(json_source)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except OSError as exc:
                raise IngestionError(
                    f"cannot open marker json {path!r}: {exc}"
                ) from exc
            except ValueError as exc:
                raise IngestionError(
                    f"marker json {path!r} is not valid JSON: {exc}"
                ) from exc
            return data, data.get("metadata")
        if isinstance(json_source, dict):
            return json_source, json_source.get("metadata")
        raise IngestionError("marker json_source must be a path or a dict")


__all__ = ["MarkerBackend", "MARKER_RENDERER"]
