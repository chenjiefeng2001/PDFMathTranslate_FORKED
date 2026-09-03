"""ExistingBackend — the v3 pdfminer parse path as an ingestion backend.

Runs exactly the parser the v3 chain has always used (pdfminer LTChar stream
→ ``canonical_page.build_page_model`` structure recovery) and adapts its
``PageModel`` list into the canonical :class:`IngestDocument`.  No semantic
annotation passes run here: classifying roles/kind is the ``normalize``
stage, *after* ingestion.  Block ids keep the v3 convention
``p<page>_<reading_index>`` so trace identity survives end-to-end.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from pdf2zh.v3.ingestion.adapter import existing_pages_to_document
from pdf2zh.v3.ingestion.base import (
    BACKEND_EXISTING,
    IngestionBackendUnavailable,
    IngestionError,
    emit_ingest_events,
)
from pdf2zh.v3.ingestion.ir import IngestDocument

log = logging.getLogger(__name__)


class ExistingBackend:
    """``ingest(pdf_path) -> IngestDocument`` over the existing pdfminer path."""

    name: str = BACKEND_EXISTING

    def __init__(self, max_pages: Optional[int] = None, max_glyphs: int = 2000) -> None:
        self.max_pages = max_pages
        self.max_glyphs = max_glyphs

    def ingest(
        self,
        pdf_path: str,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> IngestDocument:
        pages = self._parse_pages(pdf_path)
        doc = existing_pages_to_document(
            pages, source_backend=self.name, title=pdf_path
        )
        doc.set_env(pdf_path=pdf_path, backend="pdfminer+canonical_page")
        emit_ingest_events(doc, trace, pdf_path=pdf_path)
        return doc

    # ── internals ─────────────────────────────────────────────────────

    def _parse_pages(self, pdf_path: str) -> List[Any]:
        """pdfminer LT stream → v3 canonical PageModel list (structure only)."""
        from pdfminer.converter import PDFPageAggregator
        from pdfminer.layout import LAParams
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser

        from pdf2zh.v3.canonical_page import build_page_model

        pages: List[Any] = []
        try:
            fh = open(pdf_path, "rb")
        except OSError as exc:
            raise IngestionError(f"cannot open pdf {pdf_path!r}: {exc}") from exc
        try:
            parser = PDFParser(fh)
            doc = PDFDocument(parser)
            rsrcmgr = PDFResourceManager()
            device = PDFPageAggregator(rsrcmgr, laparams=LAParams())
            interpreter = PDFPageInterpreter(rsrcmgr, device)
            for page_no, pdfpage in enumerate(PDFPage.create_pages(doc)):
                if self.max_pages is not None and page_no >= self.max_pages:
                    break
                interpreter.process_page(pdfpage)
                ltpage = device.get_result()
                if ltpage is None:
                    continue
                try:
                    page = build_page_model(
                        ltpage, page_num=page_no, max_glyphs=self.max_glyphs
                    )
                except (
                    Exception
                ) as exc:  # noqa: BLE001 -- one page never kills ingestion
                    log.debug("existing_backend: page %s failed: %s", page_no, exc)
                    continue
                pages.append(page)
        except Exception as exc:  # noqa: BLE001
            raise IngestionBackendUnavailable(
                f"existing ingestion failed for {pdf_path!r}: {exc}"
            ) from exc
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        if not pages:
            raise IngestionBackendUnavailable(
                f"existing ingestion produced no pages for {pdf_path!r}"
            )
        return pages

    @staticmethod
    def from_pages(pages: Sequence[Any], *, title: str = "") -> IngestDocument:
        """Adapter-only entry for callers that already parsed PageModels."""
        return existing_pages_to_document(
            pages, source_backend=BACKEND_EXISTING, title=title
        )


__all__ = ["ExistingBackend"]
