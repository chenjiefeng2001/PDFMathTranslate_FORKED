"""MarkerBackend — datalab-to/marker (v2, vendored at ``vendor/marker``) as an
ingestion backend.

Two entry points:

- :meth:`MarkerBackend.ingest_json` — offline conversion of an already
  produced ``marker.json`` (JSONOutput schema, ``marker_single x.pdf
  --output_format json``) into the canonical :class:`IngestDocument`.  Pure
  Python + stdlib; this is the deterministic, testable path.
- :meth:`MarkerBackend.ingest` — live run: converts a PDF through Marker's
  own pipeline and converts the result.  Marker's dependency tree cannot
  coexist with the pdf2zh main env (pydantic gradio×google-genai conflict),
  so production runs go through the isolated venv built by
  ``pdf2zh-setup-marker`` (:mod:`pdf2zh.kernel.marker_env`) via the
  stdlib-only :mod:`pdf2zh.kernel.marker_worker` subprocess — the same
  pattern as MinerU's ``PDF2ZH_MINERU_PYTHON`` path.  In-process conversion
  remains as the dev-environment fallback when ``import marker`` works here.

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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from pdf2zh.v3.ingestion.adapter import marker_json_to_document, read_pdf_page_sizes
from pdf2zh.v3.ingestion.base import (
    BACKEND_MARKER,
    IngestionBackendUnavailable,
    IngestionError,
    emit_ingest_events,
)
from pdf2zh.v3.ingestion.config import normalize_page_indices as _normalize_page_indices
from pdf2zh.v3.ingestion.ir import IngestDocument

log = logging.getLogger(__name__)

MARKER_RENDERER = "marker.renderers.json.JSONRenderer"


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_page_indices(pages: Any) -> Optional[List[int]]:
    """Normalize a page selection to a sorted list of 0-based indices.

    Mirrors the MinerU slice path's selection semantics (same grammar for
    str / list / tuple / set; a ``"1-5, 8"``-style string is treated as
    ​0-based indices, identical to ``magicpdf_adapter._normalize_page_selection``)
    so a Marker page subset behaves exactly like a MinerU page slice.
    ``None`` / ``""`` / ``"all"`` return ``None`` (all pages).
    """
    return _normalize_page_indices(pages)


def _validate_selected_pages(
    pdf_path: Optional[str], selected: Optional[List[int]]
) -> None:
    if not pdf_path or not selected:
        return
    try:
        page_count = len(read_pdf_page_sizes(pdf_path))
    except Exception:
        return
    invalid = [page for page in selected if page < 0 or page >= page_count]
    if invalid:
        raise IngestionError(
            f"Marker page selection out of range for {page_count} pages: {invalid}"
        )


def _filter_json_pages(data: Dict[str, Any], sel: List[int]) -> Dict[str, Any]:
    """Keep only the Page children whose original position is in ``sel``.

    Marker's ``JSONOutput`` stores pages under root ``children`` (nodes with
    ``block_type == "Page"``); non-page scaffolding children are preserved.
    Page numbering after conversion is positional (0..k-1), so the caller
    renumbers back to the original numbers with the same mapping.
    """
    keep = set(sel)
    out = dict(data)
    children = list(data.get("children") or [])
    new_children: List[Dict[str, Any]] = []
    page_index = 0
    for child in children:
        if not isinstance(child, dict):
            new_children.append(child)
            continue
        if child.get("block_type") == "Page":
            if page_index in keep:
                new_children.append(child)
            page_index += 1
        else:
            new_children.append(child)
    out["children"] = new_children
    return out


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
        *,
        pages: Any = None,
        ocr: Optional[bool] = None,
    ) -> IngestDocument:
        """Run Marker on ``pdf_path`` and convert its JSON output.

        ``pages``: optional page subset (same selection grammar as the
        MinerU slice path, 0-based) — Marker converts only those pages
        (``page_range``) and the result is renumbered back to the original
        page numbers, exactly like MinerU's ``page_map`` remap.

        ``ocr``: force full-page OCR (marker ``force_ocr``) when ``True``;
        ``False``/``None`` leave Marker's default OCR behaviour.

        Two execution forms, tried in order:

        1. **Isolated venv subprocess** (production path): marker's dependency
           tree cannot coexist with the pdf2zh main env (pydantic
           gradio×google-genai conflict, see pyproject.toml [tool.uv])， so
           when ``pdf2zh-setup-marker`` has built the isolated venv
           (auto-detected, or ``PDF2ZH_MARKER_PYTHON``), live conversion runs
           through :mod:`pdf2zh.kernel.marker_worker` — same pattern as
           MinerU's ``PDF2ZH_MINERU_PYTHON`` subprocess path.
        2. **In-process** (dev convenience): if no isolated env exists but
           ``import marker`` works in this interpreter, convert directly.
        """
        sel = normalize_page_indices(pages)
        _validate_selected_pages(pdf_path, sel)
        force_ocr = _as_bool(ocr)
        isolated_python = self._isolated_python()
        if isolated_python:
            payload = self._ingest_subprocess(
                isolated_python,
                pdf_path,
                pages=sel,
                force_ocr=force_ocr,
            )
            doc = self._convert_payload(payload, pdf_path=pdf_path, pages=sel)
            emit_ingest_events(doc, trace, pdf_path=pdf_path)
            return doc
        return self._ingest_inprocess(
            pdf_path,
            trace=trace,
            pages=sel,
            force_ocr=force_ocr,
        )

    def _isolated_python(self) -> Optional[str]:
        """Isolated-venv interpreter for live conversion, if one is ready.

        Existence is validated **here** (``marker_python_override`` returns
        the raw ``PDF2ZH_MARKER_PYTHON`` value verbatim): a missing/broken
        override degrades to the in-process dev path — same semantics as
        MinerU's ``probe_mineru_override``. Deep importability is the
        worker's first failure mode and is reported verbatim by the
        subprocess run, so no extra probe here.
        """
        try:
            from pdf2zh.kernel.marker_env import marker_python_override

            python = marker_python_override()
            if python and os.path.exists(python):
                return python
        except Exception:  # noqa: BLE001 -- env module absent ⇒ in-process path
            pass
        return None

    def _ingest_subprocess(
        self,
        python_exe: str,
        pdf_path: str,
        *,
        pages: Optional[List[int]] = None,
        force_ocr: bool = False,
    ) -> Dict[str, Any]:
        """Run ``kernel/marker_worker.py`` under the isolated interpreter.

        The worker writes ``{stem}.json`` (JSONOutput schema) into a
        one-shot temp dir (plus ``{stem}_meta.json``), which we parse and
        merge back into one payload — byte-identical to what
        :meth:`ingest_json` consumes offline.  ``pages`` (0-based subset)
        and ``force_ocr`` map onto the worker's ``--pages`` / ``--force-ocr``
        (marker ``page_range`` / ``force_ocr``).
        """
        import shutil
        import tempfile

        worker = Path(__file__).resolve().parents[2] / "kernel" / "marker_worker.py"
        if not worker.exists():
            raise IngestionBackendUnavailable(
                f"marker worker missing: {worker} (broken install?)"
            )
        work_dir = tempfile.mkdtemp(prefix="pdf2zh_marker_sub_")
        try:
            mode = os.environ.get("PDF2ZH_MARKER_MODE", "").strip()
            cmd = [python_exe, str(worker), pdf_path, work_dir]
            if mode:
                cmd.append(mode)
            if pages:
                cmd += ["--pages", ",".join(str(p) for p in pages)]
            if force_ocr:
                cmd.append("--force-ocr")
            try:
                timeout = int(
                    os.environ.get("PDF2ZH_MARKER_TIMEOUT", "").strip() or 3600
                )
            except (TypeError, ValueError):
                timeout = 3600
            timeout = max(1, timeout)
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise IngestionBackendUnavailable(
                    f"marker worker timed out after {timeout}s: {pdf_path!r}"
                ) from exc
            except OSError as exc:
                raise IngestionBackendUnavailable(
                    f"marker worker interpreter {python_exe!r} failed to "
                    f"start: {exc}"
                ) from exc
            if completed.returncode != 0:
                stderr = (completed.stderr or "")[-2000:]
                raise IngestionBackendUnavailable(
                    f"marker worker failed (exit {completed.returncode}): "
                    f"{stderr or '(no stderr)'}"
                )
            return self._load_worker_payload(pdf_path, work_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _load_worker_payload(pdf_path: str, work_dir: str) -> Dict[str, Any]:
        """Parse ``{stem}.json`` (+ optional ``{stem}_meta.json``) from work_dir."""
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        json_path = os.path.join(work_dir, stem, f"{stem}.json")
        if not os.path.exists(json_path):
            raise IngestionBackendUnavailable(
                f"marker worker produced no {stem}.json under {work_dir}"
            )
        try:
            with open(json_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise IngestionBackendUnavailable(
                f"marker worker output {json_path!r} unreadable: {exc}"
            ) from exc
        meta_path = os.path.join(work_dir, stem, f"{stem}_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
                if isinstance(meta, dict):
                    payload.setdefault("metadata", meta)
            except (OSError, ValueError):  # noqa: BLE001 -- metadata is optional
                pass
        if not isinstance(payload, dict):
            raise IngestionBackendUnavailable(
                f"marker worker output {json_path!r} is not a JSON object"
            )
        return payload

    def _ingest_inprocess(
        self,
        pdf_path: str,
        trace: Optional[Any] = None,
        *,
        pages: Optional[List[int]] = None,
        force_ocr: bool = False,
    ) -> IngestDocument:
        """In-process conversion (dev envs with marker importable here)."""
        try:
            import marker  # noqa: F401  -- presence check only
        except Exception as exc:  # noqa: BLE001
            raise IngestionBackendUnavailable(
                "marker is not importable and no isolated venv is built. "
                "Run `pdf2zh-setup-marker` first (builds an isolated venv; "
                "marker cannot be installed into the main pdf2zh env — "
                "pydantic conflict), or install the vendored submodule for "
                "in-process dev use (cd vendor/marker && pip install -e .): "
                f"{exc}"
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
            config: Dict[str, Any] = {"output_format": "json"}
            if pages:
                config["page_range"] = pages
            if force_ocr:
                config["force_ocr"] = True
            models = create_model_dict()
            converter = PdfConverter(
                config=config,
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
        pages: Any = None,
    ) -> IngestDocument:
        """Convert an existing Marker JSON output (path or parsed dict).

        ``pages``: optional 0-based page subset — the offline equivalent of
        the live ``page_range``; page numbers are remapped back to the
        original document numbers.
        """
        data, meta = self._load_json(json_source)
        sel = normalize_page_indices(pages)
        _validate_selected_pages(pdf_path, sel)
        if sel:
            data = _filter_json_pages(data, sel)
        doc = self._convert_payload(data, pdf_path=pdf_path, title=title, pages=sel)
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
        pages: Optional[List[int]] = None,
    ) -> IngestDocument:
        pdf_sizes = None
        if pdf_path and os.path.exists(pdf_path):
            try:
                pdf_sizes = list(read_pdf_page_sizes(pdf_path))
            except (
                Exception
            ) as exc:  # noqa: BLE001 -- normalization is optional, never fatal
                log.debug("marker_backend: page sizes unavailable: %s", exc)
        elif pdf_path:
            raise IngestionError(f"cannot open pdf {pdf_path!r} for page sizes")
        # Page-sliced payloads (live ``page_range`` or offline filter) are
        # numbered 0..k-1 positionally; slice the size table to match and
        # renumber the document back to the original page numbers (same
        # convention as the MinerU slice path's ``page_map``).
        if pages:
            if pdf_sizes:
                pdf_sizes = [pdf_sizes[i] for i in pages if 0 <= i < len(pdf_sizes)]
        doc = marker_json_to_document(
            data, pdf_page_sizes=pdf_sizes, title=title or (pdf_path or "")
        )
        if pages:
            doc.renumber_pages({i: orig for i, orig in enumerate(pages)})
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
            if not isinstance(data, dict):
                raise IngestionError("marker json root must be an object")
            return data, data.get("metadata")
        if isinstance(json_source, dict):
            return json_source, json_source.get("metadata")
        raise IngestionError("marker json_source must be a path or a dict")


__all__ = ["MarkerBackend", "MARKER_RENDERER"]
