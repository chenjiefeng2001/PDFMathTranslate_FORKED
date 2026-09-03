"""Ingestion layer contracts — backend protocol, stage names, trace emission.

Every PDF-understanding backend (the existing pdfminer path, Marker, later
MinerU/magic-pdf) implements :class:`IngestionBackend` and returns the same
canonical :class:`~pdf2zh.v3.ingestion.ir.IngestDocument`, so everything
downstream (translate → plan → fixup → render) never needs to know which
backend produced the blocks.

Each backend also emits ``ingest.*`` events into the Flight Recorder with the
same ``TraceContext`` shape as plan/render/raster events, so the first
divergence of a bad output can point at *ingestion* (e.g. "2 source
paragraphs merged into 1 block by Marker") instead of always blaming the
renderer.

Pipeline stage order (extended in ``trace_rules.PIPELINE_STAGES``):

    ingest → normalize → translate → plan → fixup → layout → render → erase → raster
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pdf2zh.v3.ingestion.ir import IngestDocument

#: pipeline 阶段名（ingest 是新的第一站）。
STAGE_INGEST = "ingest"
STAGE_NORMALIZE = "normalize"
STAGE_TRANSLATE = "translate"

#: backend 标识（写进每个 block 的 source_backend / provenance）。
BACKEND_EXISTING = "existing_v3"
BACKEND_MARKER = "marker"
#: MinerU/magic-pdf 主链路（生产 magicpdf 引擎的 canonical 页模型同样能
#: 经 ``existing_pages_to_document`` 适配成 IR —— 见 magicpdf_cli 的摄入阶段）。
BACKEND_MINERU = "mineru"

#: ingest 事件名。
EVENT_INGEST_BLOCK = "ingest.block"
EVENT_INGEST_BEGIN = "ingest.begin"
EVENT_INGEST_END = "ingest.end"
#: raw 证据事件 —— canonicalization 之前的原始解析产物（见
#: :func:`emit_raw_ingest_events`；只记事实，不产判定）。
EVENT_INGEST_RAW_BEGIN = "ingest.raw.begin"
EVENT_INGEST_RAW_BLOCK = "ingest.raw.block"
EVENT_INGEST_RAW_END = "ingest.raw.end"
#: 选择性事件 —— 一次摄入决策的最终结果（auto 回退 / 强制 backend）。
EVENT_INGEST_SELECT = "ingest.select"

#: raw MinerU bbox 的坐标语义（与 ``magicpdf_bridge.flip_bbox`` 的输入约定
#: 一致）：页面 PDF 点、左上原点、y 向下。
RAW_SPACE = "page_tl"
RAW_ORIGIN = "top-left"
RAW_UNIT = "pt"


class IngestionError(RuntimeError):
    """Base error for the ingestion layer."""


class IngestionBackendUnavailable(IngestionError):
    """The backend could not run (missing dependency / model / file)."""


@runtime_checkable
class IngestionBackend(Protocol):
    """A PDF-understanding backend: ``pdf path -> canonical IngestDocument``."""

    name: str

    def ingest(
        self,
        pdf_path: str,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> IngestDocument:
        """Parse ``pdf_path`` into a canonical :class:`IngestDocument`.

        ``trace`` is an optional Flight Recorder; when given, the backend
        emits ``ingest.begin`` / ``ingest.block`` / ``ingest.end`` events
        (see :func:`emit_ingest_events`).
        """
        ...


def ingest_block_events(
    doc: IngestDocument,
    *,
    ctx_block_ids: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """One ``ingest.block`` event dict per block — pure, recorder-free.

    Shared by :func:`emit_ingest_events` and the ingestion selector's quality
    gate, so a fallback decision can be computed over the exact same event
    shape the Flight Recorder will later persist.

    ``ctx_block_ids`` maps an IR block_id to the trace ``block_id`` used in
    the TraceContext when it differs (by default the IR id is used as-is).

    Coordinates are emitted with explicit semantics (never bare numbers):
    the declared raw box plus the normalized ``v3_box`` when available.
    """
    out: List[Dict[str, Any]] = []
    for block in doc.blocks():
        box = block.box
        payload: Dict[str, Any] = {
            "backend": block.source_backend,
            "kind": block.block_type,
            "text": (block.text or "")[:200],
            "source_id": block.source_id,
            "confidence": block.confidence,
        }
        if box is not None:
            payload["box"] = box.to_dict()
            payload["box_meaning"] = box.meaning
        if block.v3_box is not None:
            payload["v3_box"] = [round(float(v), 2) for v in block.v3_box]
            payload["v3_space"] = "v3"
            payload["v3_origin"] = "lower-left"
            payload["v3_y1_meaning"] = "box_top"
        else:
            payload["v3_box"] = None
        bid = (ctx_block_ids or {}).get(block.block_id, block.block_id)
        out.append(
            {
                "event": EVENT_INGEST_BLOCK,
                "page": block.page_no,
                "block_id": bid,
                "trace_id": f"{block.page_no}/{bid}",
                "stage": STAGE_INGEST,
                "payload": payload,
            }
        )
    return out


def emit_ingest_events(
    doc: IngestDocument,
    trace,
    *,
    pdf_path: str = "",
    ctx_block_ids: Optional[Dict[str, str]] = None,
    status: Optional[str] = None,
    fallback_from: Optional[str] = None,
) -> None:
    """Write one ``ingest.block`` event per block into ``trace`` (Flight Recorder),
    bracketed by ``ingest.begin`` / ``ingest.end``.

    ``status`` (PASS/FAIL) and ``fallback_from`` let a selector run tell the
    audit *why* a backend was attempted — e.g. the fallback Marker run
    declares ``status=PASS, fallback_from=mineru`` so the trace reads as a
    story (begin mineru → end FAIL → begin marker fallback_from=mineru →
    end PASS → ingest.select selected=marker), not as two anonymous parses.
    """
    if trace is None or not getattr(trace, "enabled", False):
        return
    begin: Dict[str, Any] = {
        "pdf": pdf_path,
        "backend": doc.source_backend,
        "pages": doc.page_count,
        "blocks": doc.block_count,
        "env": dict(doc.metadata),
    }
    if fallback_from is not None:
        begin["fallback_from"] = fallback_from
    trace.emit(EVENT_INGEST_BEGIN, trace.ctx(-1, "*", STAGE_INGEST), begin)
    for ev in ingest_block_events(doc, ctx_block_ids=ctx_block_ids):
        trace.emit(
            EVENT_INGEST_BLOCK,
            trace.ctx(ev["page"], ev["block_id"], STAGE_INGEST),
            ev["payload"],
        )
    end: Dict[str, Any] = {"backend": doc.source_backend, "blocks": doc.block_count}
    if status is not None:
        end["status"] = status
    trace.emit(EVENT_INGEST_END, trace.ctx(-1, "*", STAGE_INGEST), end)


def emit_raw_ingest_events(
    results,
    trace,
    *,
    pdf_path: str = "",
    backend: str = BACKEND_MINERU,
) -> None:
    """One ``ingest.raw.block`` per raw MinerU block, *before* any
    canonicalization — bracketed by ``ingest.raw.begin`` / ``ingest.raw.end``.

    Raw events carry facts only, never verdicts: ``source_backend`` /
    ``source_id`` / raw kind / raw bbox with **declared** coordinate
    semantics (page points, top-left origin, y down) / page dimensions /
    whether v3 normalization was even possible (page height > 0).  Together
    with the canonical ``ingest.block`` events (post-adapter ``v3_box``)
    they answer the audit's question — "did MinerU itself lack geometry, or
    did the adapter drop it during canonicalization?" — without a second
    quality-rule set.

    ``block_id`` reuses the canonical convention ``p{page}_{i}`` (raw and
    canonical blocks share ``trace_id``), so ``explain`` can walk raw →
    canonical facts for the same block.  ``results`` are
    :class:`~pdf2zh.magicpdf_adapter.MagicPdfParseResult` (``blocks`` =
    ``[{type, cls, bbox, text, ...}]``).
    """
    if trace is None or not getattr(trace, "enabled", False):
        return
    results = list(results or [])
    trace.emit(
        EVENT_INGEST_RAW_BEGIN,
        trace.ctx(-1, "*", STAGE_INGEST),
        {
            "pdf": pdf_path,
            "backend": backend,
            "pages": len(results),
            "space": RAW_SPACE,
            "origin": RAW_ORIGIN,
            "unit": RAW_UNIT,
        },
    )
    total = 0
    for res in results:
        pno = int(getattr(res, "page_num", 0))
        w = float(getattr(res, "width", 0.0) or 0.0)
        h = float(getattr(res, "height", 0.0) or 0.0)
        for i, blk in enumerate(getattr(res, "blocks", []) or []):
            if not isinstance(blk, dict):
                continue
            bbox = blk.get("bbox")
            payload: Dict[str, Any] = {
                "source_backend": backend,
                "source_id": f"{pno}/{i}",
                "kind": blk.get("cls") or blk.get("type") or "",
                "raw_type": blk.get("type", ""),
                "text": (blk.get("text") or "")[:200],
                "box": [round(float(v), 2) for v in bbox] if bbox else None,
                "box_meaning": "block",
                "box_space": RAW_SPACE,
                "box_origin": RAW_ORIGIN,
                "box_unit": RAW_UNIT,
                "page_width": round(w, 2),
                "page_height": round(h, 2),
                "normalized": bool(bbox) and h > 0,
            }
            if not (h > 0):
                payload["normalization_reason"] = "page_height_missing"
            elif not bbox:
                payload["normalization_reason"] = "box_missing"
            trace.emit(
                EVENT_INGEST_RAW_BLOCK,
                trace.ctx(pno, f"p{pno}_{i}", STAGE_INGEST),
                payload,
            )
            total += 1
    trace.emit(
        EVENT_INGEST_RAW_END,
        trace.ctx(-1, "*", STAGE_INGEST),
        {"backend": backend, "pages": len(results), "blocks": total},
    )


def emit_ingest_selection(decision, trace, *, pdf_path: str = "") -> None:
    """Write one ``ingest.select`` event carrying the final ingestion decision
    (``requested_backend`` / ``selected_backend`` / fallback reason ...).
    """
    if trace is None or not getattr(trace, "enabled", False):
        return
    payload: Dict[str, Any] = {"pdf": pdf_path}
    if hasattr(decision, "to_dict"):
        payload["decision"] = decision.to_dict()
    else:
        payload["decision"] = dict(decision or {})
    trace.emit(EVENT_INGEST_SELECT, trace.ctx(-1, "*", STAGE_INGEST), payload)


def emit_ingest_run_failure(
    backend: str,
    reason: str,
    trace,
    *,
    pdf_path: str = "",
    fallback_from: Optional[str] = None,
) -> None:
    """Record a backend run that never produced a document (parse exception):
    ``ingest.begin`` + ``ingest.end status=FAIL reason=...``.  Keeps the trace
    story complete for a primary that crashed before any block existed.
    """
    if trace is None or not getattr(trace, "enabled", False):
        return
    begin: Dict[str, Any] = {"pdf": pdf_path, "backend": backend, "blocks": 0}
    if fallback_from is not None:
        begin["fallback_from"] = fallback_from
    trace.emit(EVENT_INGEST_BEGIN, trace.ctx(-1, "*", STAGE_INGEST), begin)
    trace.emit(
        EVENT_INGEST_END,
        trace.ctx(-1, "*", STAGE_INGEST),
        {"backend": backend, "blocks": 0, "status": "FAIL", "reason": reason},
    )


__all__ = [
    "STAGE_INGEST",
    "STAGE_NORMALIZE",
    "STAGE_TRANSLATE",
    "BACKEND_EXISTING",
    "BACKEND_MARKER",
    "BACKEND_MINERU",
    "EVENT_INGEST_BLOCK",
    "EVENT_INGEST_BEGIN",
    "EVENT_INGEST_END",
    "EVENT_INGEST_RAW_BEGIN",
    "EVENT_INGEST_RAW_BLOCK",
    "EVENT_INGEST_RAW_END",
    "EVENT_INGEST_SELECT",
    "RAW_SPACE",
    "RAW_ORIGIN",
    "RAW_UNIT",
    "emit_raw_ingest_events",
    "IngestionError",
    "IngestionBackendUnavailable",
    "IngestionBackend",
    "ingest_block_events",
    "emit_ingest_events",
    "emit_ingest_selection",
    "emit_ingest_run_failure",
]
