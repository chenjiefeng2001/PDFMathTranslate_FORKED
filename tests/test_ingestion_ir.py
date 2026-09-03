"""Tests for pdf2zh/v3/ingestion — canonical IR, backends, comparator, rules.

Covers the Marker-ingestion plan's core claims:
- Marker JSON (JSONOutput schema) converts into a canonical IR with full
  provenance and *declared* coordinate semantics;
- marker image-pixel boxes are normalized into v3 only via an explicit
  page-size projection (never guessed);
- both backends emit ingest.* events compatible with the first_divergence
  engine, which now ranks ingest before plan.
"""

from __future__ import annotations

import json

import pytest

from pdf2zh.v3.flight_recorder import FlightRecorder, read_events
from pdf2zh.v3.ingestion import (
    BACKEND_EXISTING,
    BACKEND_MARKER,
    ExistingBackend,
    IngestDocument,
    MarkerBackend,
    compare,
)
from pdf2zh.v3.ingestion.adapter import (
    html_to_text,
    marker_json_to_document,
    normalize_marker_box,
)
from pdf2zh.v3.ingestion.base import (
    EVENT_INGEST_BEGIN,
    EVENT_INGEST_BLOCK,
    EVENT_INGEST_END,
    STAGE_INGEST,
)
from pdf2zh.v3.ingestion.comparator import SEVERITY_HIGH, SEVERITY_MEDIUM
from pdf2zh.v3.ingestion.ir import (
    KIND_FOOTER,
    KIND_HEADING,
    KIND_PARAGRAPH,
    KIND_TABLE,
    KIND_TABLE_CELL,
    ORIGIN_TOP_LEFT,
    SPACE_MARKER_IMAGE,
    UNIT_PT,
    UNIT_PX,
    IngestBox,
)
from pdf2zh.v3.ingestion.rules import (
    RULE_INGEST_GEOMETRY_DECLARED,
    RULE_MARKER_GEOMETRY_NORMALIZED,
    run_ingest_rules,
)
from pdf2zh.v3.trace_rules import RuleResult, annotate_first_divergence

# ── fixtures ─────────────────────────────────────────────────────────────


def marker_page(blocks, page_no: int = 0, w: float = 1000.0, h: float = 1400.0) -> dict:
    return {
        "id": f"/page/{page_no}",
        "block_type": "Page",
        "html": "",
        "bbox": [0, 0, w, h],
        "children": blocks,
    }


def marker_block(
    block_type: str,
    text: str,
    idx: int,
    page_no: int = 0,
    bbox=(100.0, 200.0, 500.0, 250.0),
    children=None,
) -> dict:
    node = {
        "id": f"/page/{page_no}/{block_type}/{idx}",
        "block_type": block_type,
        "html": text,
        "bbox": list(bbox),
        "children": children or [],
    }
    return node


def sample_marker_json() -> dict:
    """Two paragraphs + one footer on page 0; one heading on page 1."""
    return {
        "block_type": "Document",
        "metadata": {"marker_version": "v2.0.0"},
        "children": [
            marker_page(
                [
                    marker_block(
                        "SectionHeader",
                        "Chapter &lt;1&gt;",
                        3,
                        bbox=(100, 100, 400, 130),
                    ),
                    marker_block(
                        "Text",
                        "First <b>paragraph</b> with math",
                        1,
                        bbox=(100, 200, 500, 240),
                    ),
                    marker_block(
                        "Text", "Second paragraph", 2, bbox=(100, 260, 480, 300)
                    ),
                    marker_block(
                        "PageFooter", "page 1", 4, bbox=(100, 1300, 200, 1340)
                    ),
                    {
                        "id": "/page/0/Table/5",
                        "block_type": "Table",
                        "html": "",
                        "bbox": [100, 400, 600, 700],
                        "children": [
                            marker_block(
                                "TableCell", "1 | 2", 6, bbox=(110, 410, 590, 450)
                            ),
                        ],
                    },
                ]
            ),
            marker_page(
                [
                    marker_block(
                        "SectionHeader",
                        "Second chapter",
                        0,
                        page_no=1,
                        bbox=(100, 100, 400, 130),
                    )
                ],
                page_no=1,
            ),
        ],
    }


@pytest.fixture
def sample_json() -> dict:
    return sample_marker_json()


@pytest.fixture
def marker_json_file(tmp_path, sample_json) -> str:
    p = tmp_path / "sample.json"
    p.write_text(json.dumps(sample_json), encoding="utf-8")
    return str(p)


# ── IR basics & serialization ────────────────────────────────────────────


def test_marker_json_to_document_provenance_and_kinds(sample_json):
    doc = marker_json_to_document(sample_json)
    assert doc.source_backend == BACKEND_MARKER
    assert doc.page_count == 2
    # reading order preserved per page
    page0 = [b.block_id for b in doc.page_blocks(0)]
    assert page0[0].startswith("m0_") and len(page0) == 6
    blocks = {b.block_id: b for b in doc.blocks()}
    kinds = {b.block_type for b in doc.blocks()}
    assert KIND_HEADING in kinds
    assert KIND_PARAGRAPH in kinds
    assert KIND_FOOTER in kinds
    assert KIND_TABLE in kinds and KIND_TABLE_CELL in kinds
    # provenance survives
    text_block = next(b for b in blocks.values() if b.block_type == KIND_PARAGRAPH)
    assert text_block.source_backend == BACKEND_MARKER
    assert text_block.source_id.startswith("/page/0/Text/")
    # marker kinds recorded verbatim in metadata
    table = next(b for b in blocks.values() if b.block_type == KIND_TABLE)
    assert table.metadata["marker_block_type"] == "Table"
    assert table.children, "table container keeps child links"


def test_marker_text_extraction(sample_json):
    doc = marker_json_to_document(sample_json)
    heading = next(b for b in doc.blocks() if b.block_type == KIND_HEADING)
    # html entity unescaped, tags stripped
    assert heading.text == "Chapter <1>"
    para = next(b for b in doc.blocks() if "paragraph" in b.text)
    assert para.text == "First paragraph with math"


def test_marker_coordinate_semantics_and_normalization(sample_json):
    pdf_sizes = [(500.0, 700.0), (500.0, 700.0)]  # same page, in points
    doc = marker_json_to_document(sample_json, pdf_page_sizes=pdf_sizes)
    para = next(b for b in doc.blocks() if "Second paragraph" in b.text)
    # declared box: raw marker image pixels, top-left origin
    assert para.box is not None
    assert para.box.space == SPACE_MARKER_IMAGE
    assert para.box.origin == ORIGIN_TOP_LEFT
    assert para.box.unit == UNIT_PX
    # normalized v3 box: px 1000x1400 -> pt 500x700 (scale .5, y-flip):
    # raw (100, 260, 480, 300) top-left -> v3 (50, 550, 240, 570) lower-left
    assert para.v3_box == pytest.approx((50.0, 550.0, 240.0, 570.0))
    # y1 in v3 is box_top
    assert para.v3_box[3] > para.v3_box[1]


def test_marker_without_pdf_sizes_never_guesses(sample_json):
    doc = marker_json_to_document(sample_json)
    assert all(b.v3_box is None for b in doc.blocks() if b.box is not None)


def test_normalize_marker_box_math():
    box = normalize_marker_box(
        (100.0, 200.0, 300.0, 400.0),
        page_px_width=1000.0,
        page_px_height=1400.0,
        page_pt_width=500.0,
        page_pt_height=700.0,
    )
    assert box == pytest.approx((50.0, 500.0, 150.0, 600.0))


def test_html_to_text_br_and_entities():
    assert html_to_text("a<br>b") == "a b"
    assert html_to_text("<p><b>hi</b> &amp; bye</p>") == "hi & bye"


def test_ir_json_roundtrip(sample_json):
    doc = marker_json_to_document(sample_json, pdf_page_sizes=[(500, 700), (500, 700)])
    text = doc.to_json()
    restored = IngestDocument.from_json(text)
    assert restored.page_count == doc.page_count
    assert restored.block_count == doc.block_count
    assert {b.block_id for b in restored.blocks()} == {b.block_id for b in doc.blocks()}
    b = restored.block("m0_2")
    assert b is not None and b.v3_box == doc.block("m0_2").v3_box


def test_ingest_box_roundtrip():
    box = IngestBox(
        1,
        2,
        3,
        4,
        space="v3",
        origin="lower-left",
        meaning="block",
        semantics={"y1": "box_top"},
    )
    assert IngestBox.from_dict(box.to_dict()) == box


# ── existing backend (pdfminer path) ─────────────────────────────────────


def _write_tiny_pdf(path) -> None:
    import pymupdf  # noqa: PLC0415

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((50, 80), "Hello ingestion world", fontsize=12)
    page.insert_text((50, 120), "A second line", fontsize=12)
    doc.save(path)
    doc.close()


def test_existing_backend_ingest(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "tiny.pdf"
    _write_tiny_pdf(str(pdf))
    doc = ExistingBackend().ingest(str(pdf))
    assert doc.source_backend == BACKEND_EXISTING
    assert doc.page_count == 1
    assert doc.block_count >= 1
    page = doc.page(0)
    assert page.width_pt == pytest.approx(300.0, abs=1.0)
    assert page.height_pt == pytest.approx(400.0, abs=1.0)
    text_blocks = [b for b in doc.page_blocks(0) if b.text.strip()]
    assert text_blocks, "pdfminer path should extract text blocks"
    for b in text_blocks:
        # already in v3 space: declared box & normalized box agree
        assert b.box is not None and b.box.space == "v3"
        assert b.v3_box is not None
        assert b.source_id.startswith("existing:")
    assert doc.text_coverage(0)["chars"] > 0


def test_existing_backend_missing_file_raises(tmp_path):
    from pdf2zh.v3.ingestion import IngestionError

    with pytest.raises(IngestionError):
        ExistingBackend().ingest(str(tmp_path / "nope.pdf"))


# ── ingest events in the flight recorder ─────────────────────────────────


def test_ingest_events_emitted_existing(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "tiny.pdf"
    _write_tiny_pdf(str(pdf))
    trace_path = str(tmp_path / "trace.jsonl")
    with FlightRecorder(trace_path, book_id="ingest-test", level=1) as trace:
        doc = ExistingBackend().ingest(str(pdf), trace=trace)
        assert doc.block_count > 0
    events = list(read_events(trace_path))
    names = [e["event"] for e in events]
    assert EVENT_INGEST_BEGIN in names
    assert EVENT_INGEST_END in names
    assert EVENT_INGEST_BLOCK in names
    block_events = [e for e in events if e["event"] == EVENT_INGEST_BLOCK]
    assert all(e["stage"] == STAGE_INGEST for e in block_events)
    payload = block_events[0]["payload"]
    assert payload["backend"] == BACKEND_EXISTING
    assert payload["box"]["meaning"] == "block"
    assert payload["v3_space"] == "v3"
    assert payload["v3_y1_meaning"] == "box_top"


def test_ingest_events_emitted_marker_json(tmp_path, sample_json):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(sample_json), encoding="utf-8")
    trace_path = str(tmp_path / "trace.jsonl")
    with FlightRecorder(trace_path, book_id="ingest-marker", level=1) as trace:
        MarkerBackend().ingest_json(str(path), trace=trace)
    events = [e for e in read_events(trace_path) if e["event"] == EVENT_INGEST_BLOCK]
    assert events, "marker ingest must emit block events"
    assert all(e["payload"]["backend"] == BACKEND_MARKER for e in events)


def test_emit_raw_ingest_events_facts_and_reasons(tmp_path):
    """raw 事件只记事实：坐标语义声明 + 归一化可行性 + 失败原因（无新规则）。"""
    from pdf2zh.v3.flight_recorder import FlightRecorder, read_events
    from pdf2zh.v3.ingestion import BACKEND_MINERU
    from pdf2zh.v3.ingestion.base import (
        EVENT_INGEST_RAW_BEGIN,
        EVENT_INGEST_RAW_BLOCK,
        EVENT_INGEST_RAW_END,
        RAW_ORIGIN,
        RAW_SPACE,
        RAW_UNIT,
        emit_raw_ingest_events,
    )

    class _R:
        def __init__(self, pno, w, h, blocks):
            self.page_num = pno
            self.width = w
            self.height = h
            self.blocks = blocks

    results = [
        _R(
            0,
            612.0,
            792.0,
            [
                {
                    "type": "text",
                    "cls": "body",
                    "bbox": [0, 0, 300, 24],
                    "text": "hi",
                }
            ],
        ),
        _R(1, 612.0, 0.0, [{"type": "text", "cls": "body", "bbox": [0, 0, 10, 10]}]),
        _R(2, 612.0, 792.0, [{"type": "text", "cls": "title"}]),
    ]
    trace_path = tmp_path / "raw.jsonl"
    with FlightRecorder(str(trace_path), book_id="raw-test", level=1) as rec:
        emit_raw_ingest_events(
            results, rec, pdf_path="book.pdf", backend=BACKEND_MINERU
        )
    events = list(read_events(str(trace_path)))
    names = [e["event"] for e in events]
    assert EVENT_INGEST_RAW_BEGIN in names and EVENT_INGEST_RAW_END in names
    begin = next(e for e in events if e["event"] == EVENT_INGEST_RAW_BEGIN)
    assert begin["payload"]["backend"] == BACKEND_MINERU
    assert begin["payload"]["space"] == RAW_SPACE
    assert begin["payload"]["origin"] == RAW_ORIGIN
    assert begin["payload"]["unit"] == RAW_UNIT
    blocks = [e for e in events if e["event"] == EVENT_INGEST_RAW_BLOCK]
    assert len(blocks) == 3
    ok = blocks[0]["payload"]
    assert ok["source_id"] == "0/0"
    assert ok["box"] == [0.0, 0.0, 300.0, 24.0]
    assert ok["normalized"] is True and "normalization_reason" not in ok
    assert ok["box_space"] == RAW_SPACE and ok["kind"] == "body"
    no_h = blocks[1]["payload"]
    assert no_h["normalized"] is False
    assert no_h["normalization_reason"] == "page_height_missing"
    no_box = blocks[2]["payload"]
    assert no_box["normalized"] is False
    assert no_box["normalization_reason"] == "box_missing"
    end = next(e for e in events if e["event"] == EVENT_INGEST_RAW_END)
    assert end["payload"]["blocks"] == 3 and end["payload"]["pages"] == 3


# ── comparator ───────────────────────────────────────────────────────────


def _existing_doc_two_paragraphs() -> IngestDocument:
    doc = IngestDocument(source_backend=BACKEND_EXISTING)
    doc.add_page(0, width_pt=500, height_pt=700)
    doc.add_leaf(
        block_id="p0_0",
        page_no=0,
        block_type=KIND_PARAGRAPH,
        text="alpha beta",
        box=IngestBox(0, 0, 500, 60),
        v3_box=(0, 0, 500, 60),
    )
    doc.add_leaf(
        block_id="p0_1",
        page_no=0,
        block_type=KIND_PARAGRAPH,
        text="gamma",
        box=IngestBox(0, 80, 500, 140),
        v3_box=(0, 80, 500, 140),
    )
    return doc


def test_comparator_detects_merge_and_table(tmp_path):
    a = _existing_doc_two_paragraphs()
    b = marker_json_to_document(
        {
            "block_type": "Document",
            "metadata": {},
            "children": [
                marker_page(
                    [
                        marker_block(
                            "Text", "alpha beta gamma", 1, bbox=(0, 0, 500, 140)
                        ),
                        {
                            "id": "/page/0/Table/2",
                            "block_type": "Table",
                            "html": "",
                            "bbox": [0, 200, 500, 300],
                            "children": [
                                marker_block(
                                    "TableCell", "x", 3, bbox=(0, 210, 100, 250)
                                )
                            ],
                        },
                    ]
                )
            ],
        },
        pdf_page_sizes=[(500, 700)],
    )
    diff = compare(a, b)
    items = diff.items
    kinds = [i.kind for i in items]
    assert "merged" in kinds
    assert "table_detection" in kinds
    merged = next(i for i in items if i.kind == "merged")
    assert merged.severity == SEVERITY_MEDIUM
    assert "p0_0" in merged.message and "p0_1" in merged.message
    table_item = next(i for i in items if i.kind == "table_detection")
    assert table_item.severity == SEVERITY_MEDIUM
    assert diff.max_severity == SEVERITY_MEDIUM
    s = diff.summary()
    assert s["by_severity"]["MEDIUM"] >= 2


def test_comparator_page_count_mismatch(sample_json):
    a = IngestDocument(source_backend=BACKEND_EXISTING)
    a.add_page(0, 500, 700)
    b = marker_json_to_document(sample_json)
    diff = compare(a, b)
    page_items = [i for i in diff.items if i.kind == "page_count"]
    assert page_items and page_items[0].severity == SEVERITY_HIGH


# ── rules / first divergence ─────────────────────────────────────────────


def _ingest_event(page, block_id, payload) -> dict:
    return {
        "event": EVENT_INGEST_BLOCK,
        "stage": STAGE_INGEST,
        "page": page,
        "block_id": block_id,
        "trace_id": f"{page}/{block_id}",
        "payload": payload,
    }


def test_ingest_geometry_rule_passes_when_declared():
    ev = _ingest_event(
        1,
        "p1_0",
        {
            "backend": BACKEND_EXISTING,
            "kind": KIND_PARAGRAPH,
            "text": "hello",
            "box": {
                "space": "v3",
                "origin": "lower-left",
                "unit": UNIT_PT,
                "meaning": "block",
            },
            "v3_box": [0, 0, 10, 10],
        },
    )
    assert run_ingest_rules([ev]) == []


def test_ingest_geometry_rule_flags_missing_unit():
    """Generic invariant: a declared box without unit is not declared geometry."""
    ev = _ingest_event(
        1,
        "p1_0",
        {
            "backend": BACKEND_EXISTING,
            "kind": KIND_PARAGRAPH,
            "text": "hello",
            "box": {"space": "v3", "origin": "lower-left", "meaning": "block"},
            "v3_box": [0, 0, 10, 10],
        },
    )
    results = run_ingest_rules([ev])
    assert len(results) == 1
    assert results[0].rule == RULE_INGEST_GEOMETRY_DECLARED
    assert "unit" in results[0].evidence.get("missing", [])


def test_ingest_geometry_rule_flags_undeclared_box():
    ev = _ingest_event(
        1,
        "p1_0",
        {"backend": BACKEND_EXISTING, "kind": KIND_PARAGRAPH, "text": "hello"},
    )
    results = run_ingest_rules([ev])
    assert len(results) == 1
    assert results[0].rule == RULE_INGEST_GEOMETRY_DECLARED
    assert results[0].severity == "HIGH"
    assert results[0].stage == STAGE_INGEST


def test_marker_unormalized_rule_flags_missing_v3():
    ev = _ingest_event(
        1,
        "m1_0",
        {
            "backend": BACKEND_MARKER,
            "kind": KIND_PARAGRAPH,
            "text": "hello",
            "box": {
                "space": SPACE_MARKER_IMAGE,
                "origin": ORIGIN_TOP_LEFT,
                "unit": UNIT_PX,
                "meaning": "block",
            },
            "v3_box": None,
        },
    )
    results = run_ingest_rules([ev])
    rules = {r.rule for r in results}
    assert RULE_MARKER_GEOMETRY_NORMALIZED in rules


def test_first_divergence_ranks_ingest_before_plan():
    ingest_fail = run_ingest_rules(
        [
            _ingest_event(
                1,
                "p1_0",
                {"backend": BACKEND_EXISTING, "kind": KIND_PARAGRAPH, "text": "hello"},
            )
        ]
    )[0]
    plan_fail = RuleResult(
        rule="EMPTY_TRANSLATION",
        status="FAIL",
        severity="HIGH",
        block_id="p1_0",
        page=1,
        trace_id="1/p1_0",
        stage="plan",
    )
    first_map = annotate_first_divergence([ingest_fail, plan_fail])
    assert first_map["1/p1_0"] == STAGE_INGEST
    assert ingest_fail.first_divergence == STAGE_INGEST and not ingest_fail.downstream
    assert plan_fail.first_divergence == STAGE_INGEST and plan_fail.downstream


# ── MarkerBackend offline API ────────────────────────────────────────────


def test_marker_backend_ingest_json_file(marker_json_file):
    doc = MarkerBackend(marker_version="v2.0.0").ingest_json(marker_json_file)
    assert doc.source_backend == BACKEND_MARKER
    assert doc.page_count == 2
    assert doc.metadata.get("marker_version") == "v2.0.0"
    assert doc.metadata.get("backend") == "datalab-to/marker"


def test_marker_backend_ingest_json_with_pdf_sizes(marker_json_file, tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "pg.pdf"
    _write_tiny_pdf(str(pdf))
    doc = MarkerBackend().ingest_json(marker_json_file, pdf_path=str(pdf))
    # real PDF page size (300x400pt) drives the v3 normalization
    assert doc.page(0).width_pt == pytest.approx(300.0, abs=1.0)
    assert doc.page(0).height_pt == pytest.approx(400.0, abs=1.0)
    assert any(b.v3_box is not None for b in doc.blocks())


# ── chain bridge: Marker IR → v3 DocumentModel → render plan ──────────────


def test_bridge_marker_ir_to_document_model(sample_json):
    doc = marker_json_to_document(
        sample_json, pdf_page_sizes=[(500.0, 700.0), (500.0, 700.0)]
    )
    from pdf2zh.v3.ingestion.bridge import (
        ingest_document_to_pages,
        model_from_ingest_document,
    )

    pages = ingest_document_to_pages(doc)
    assert len(pages) == 2
    p0 = pages[0]
    kinds = {b.kind for b in p0.blocks}
    assert "table" in kinds and "paragraph" in kinds and "heading" in kinds
    # container (table) collapsed its cell subtree into one canonical block
    table = next(b for b in p0.blocks if b.kind == "table")
    assert table.text.strip()
    # provenance retained on every canonical block
    head = next(b for b in p0.blocks if b.kind == "heading")
    assert head.metadata.get("ingest_backend") == BACKEND_MARKER
    assert head.metadata.get("ingest_source_id", "").startswith(
        "/page/0/SectionHeader/"
    )

    model = model_from_ingest_document(doc)
    assert len(model.pages) == 2
    assert model.metadata.get("ingest_backend") == BACKEND_MARKER
    # geometry stayed in v3: block y1 (box_top) >= y0
    for page in model.pages:
        for b in page.blocks:
            assert b.y1 >= b.y0 - 1e-6, f"block {b.kind} has inverted v3 box"


def test_bridge_model_drives_render_plan(sample_json):
    doc = marker_json_to_document(
        sample_json, pdf_page_sizes=[(500.0, 700.0), (500.0, 700.0)]
    )
    from pdf2zh.v3.document_model import render_plan_from_model
    from pdf2zh.v3.ingestion.bridge import model_from_ingest_document

    model = model_from_ingest_document(doc)
    plan = render_plan_from_model(model)
    assert plan, "a translated model from Marker IR must produce a render plan"
    assert all("block_id" in e and "render_path" in e for e in plan)


def test_marker_backend_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    from pdf2zh.v3.ingestion import IngestionError

    with pytest.raises(IngestionError):
        MarkerBackend().ingest_json(str(bad))


# ── v1.1: ingest rules inside the production trace audit ──────────────────


def _write_jsonl(path, events) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return str(path)


def _plan_flow_event(page, block_id, text="", translated="", render_path=""):
    return {
        "event": "plan.flow",
        "stage": "plan",
        "page": page,
        "block_id": block_id,
        "trace_id": f"{page}/{block_id}",
        "payload": {"text": text, "translated": translated, "render_path": render_path},
    }


def test_audit_qualifies_ingest_rule_fails(tmp_path):
    """trace_audit 必须消费 ingest 规则：无坐标声明的文本块 → qualification FAIL。"""
    from pdf2zh.v3.trace_audit import _run_audit

    trace_path = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            _ingest_event(
                1,
                "p1_0",
                {"backend": BACKEND_EXISTING, "kind": KIND_PARAGRAPH, "text": "hello"},
            ),
            _ingest_event(
                1,
                "p1_1",
                {
                    "backend": BACKEND_EXISTING,
                    "kind": KIND_PARAGRAPH,
                    "text": "ok",
                    "box": {
                        "space": "v3",
                        "origin": "lower-left",
                        "unit": UNIT_PT,
                        "meaning": "block",
                    },
                    "v3_box": [0, 0, 10, 10],
                },
            ),
        ],
    )
    out = tmp_path / "audit"
    rc = _run_audit(trace_path, out=str(out))
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["qualification"] == "FAIL"
    assert summary["by_rule"][RULE_INGEST_GEOMETRY_DECLARED] == 1
    assert summary["first_divergence_by_stage"] == {"ingest": 1}
    rules = {r["rule"]: r for r in summary["rules"]}
    bad = rules[RULE_INGEST_GEOMETRY_DECLARED]
    assert bad["stage"] == "ingest"
    assert bad["first_divergence"] == "ingest"
    assert not bad["downstream"]
    ledger = (out / "defect-ledger.csv").read_text(encoding="utf-8")
    assert RULE_INGEST_GEOMETRY_DECLARED in ledger
    assert "1,p1_0,INGEST_GEOMETRY_DECLARED,ingest,HIGH,adapter-fix,ingest,0," in ledger
    md = (out / "qualification.md").read_text(encoding="utf-8")
    assert "ingest  FAIL" in md
    assert "first divergence (INGEST_GEOMETRY_DECLARED)" in md


def test_audit_first_divergence_ingest_outranks_plan(tmp_path):
    """同 trace_id 既有 ingest FAIL 又有 plan FAIL：ingest 是根因，plan 是下游。"""
    from pdf2zh.v3.trace_audit import _run_audit

    trace_path = _write_jsonl(
        tmp_path / "trace.jsonl",
        [
            _ingest_event(
                1,
                "p1_0",
                {"backend": BACKEND_EXISTING, "kind": KIND_PARAGRAPH, "text": "hello"},
            ),
            _plan_flow_event(
                1, "p1_0", text="hello", translated="", render_path="shift_down"
            ),
        ],
    )
    out = tmp_path / "audit"
    assert _run_audit(trace_path, out=str(out)) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["qualification"] == "FAIL"
    assert summary["first_divergence_by_stage"] == {"ingest": 1}
    assert summary["downstream_symptoms"] == 1
    rules = {r["rule"]: r for r in summary["rules"]}
    assert rules[RULE_INGEST_GEOMETRY_DECLARED]["first_divergence"] == "ingest"
    empty = rules["EMPTY_TRANSLATION"]
    assert empty["first_divergence"] == "ingest"
    assert empty["downstream"]
    md = (out / "qualification.md").read_text(encoding="utf-8")
    assert (
        "ingest  FAIL" in md and "← first divergence (INGEST_GEOMETRY_DECLARED)" in md
    )
    assert "← downstream symptom (EMPTY_TRANSLATION)" in md


# ── v1.1: IngestionSelector / IngestionDecision / ingest.select ───────────


def test_gate_quality_passes_and_fails():
    from pdf2zh.v3.ingestion.selector import QUALITY_FAIL, QUALITY_PASS, gate_quality

    bad = gate_quality(
        [
            _ingest_event(
                1,
                "p1_0",
                {"backend": BACKEND_EXISTING, "kind": KIND_PARAGRAPH, "text": "hello"},
            )
        ]
    )
    assert bad.quality == QUALITY_FAIL
    assert RULE_INGEST_GEOMETRY_DECLARED in bad.failed_rules
    assert bad.by_rule[RULE_INGEST_GEOMETRY_DECLARED] == "HIGH"
    good = gate_quality(
        [
            _ingest_event(
                1,
                "p1_0",
                {
                    "backend": BACKEND_EXISTING,
                    "kind": KIND_PARAGRAPH,
                    "text": "ok",
                    "box": {
                        "space": "v3",
                        "origin": "lower-left",
                        "unit": UNIT_PT,
                        "meaning": "block",
                    },
                    "v3_box": [0, 0, 10, 10],
                },
            )
        ]
    )
    assert good.quality == QUALITY_PASS and good.failed_rules == []


def test_decide_forced_and_auto_fallback():
    from pdf2zh.v3.ingestion import BACKEND_MARKER, BACKEND_MINERU
    from pdf2zh.v3.ingestion.selector import (
        QUALITY_FAIL,
        QUALITY_PASS,
        REASON_FALLBACK_UNAVAILABLE,
        REASON_FORCED,
        REASON_PRIMARY_OK,
        REASON_PRIMARY_QUALITY_FAIL,
        decide,
    )

    forced = decide("marker", primary=BACKEND_MARKER, primary_quality=QUALITY_PASS)
    assert forced.selected_backend == BACKEND_MARKER
    assert not forced.fallback and forced.reason == REASON_FORCED
    assert forced.requested_backend == "marker"

    ok = decide(
        "auto",
        primary=BACKEND_MINERU,
        primary_quality=QUALITY_PASS,
        fallback_available=True,
    )
    assert ok.selected_backend == BACKEND_MINERU
    assert not ok.fallback and ok.reason == REASON_PRIMARY_OK

    fallback = decide(
        "auto",
        primary=BACKEND_MINERU,
        primary_quality=QUALITY_FAIL,
        primary_failed_rules=[RULE_INGEST_GEOMETRY_DECLARED],
        fallback_available=True,
    )
    assert fallback.selected_backend == BACKEND_MARKER
    assert fallback.fallback and fallback.reason == REASON_PRIMARY_QUALITY_FAIL
    assert fallback.primary_backend == BACKEND_MINERU
    assert RULE_INGEST_GEOMETRY_DECLARED in fallback.failed_rules
    # 决策与 run 结局分离：attempted=True 由 decide 设置，succeeded 留给调用方
    assert fallback.fallback_attempted and not fallback.fallback_succeeded
    d = fallback.to_dict()
    assert d["selected_backend"] == BACKEND_MARKER
    assert d["fallback_attempted"] is True and d["fallback_succeeded"] is False

    stuck = decide(
        "auto",
        primary=BACKEND_MINERU,
        primary_quality=QUALITY_FAIL,
        primary_failed_rules=[RULE_INGEST_GEOMETRY_DECLARED],
        fallback_available=False,
    )
    assert stuck.selected_backend == BACKEND_MINERU
    assert not stuck.fallback and stuck.reason == REASON_FALLBACK_UNAVAILABLE
    assert not stuck.fallback_attempted and not stuck.fallback_succeeded
    assert stuck.quality == QUALITY_FAIL  # 失败可见，不静默吞掉


def test_selector_rejects_backend_heuristics_in_rules(sample_json):
    """回退只由 canonical ingest invariants 触发 —— marker 专属质量阈值不进规则。"""
    from pdf2zh.v3.ingestion.selector import QUALITY_PASS, gate_quality

    # 正常 marker 摄入（含 pdf 页尺寸 → v3 归一化）不触发任何规则
    doc = marker_json_to_document(
        sample_json, pdf_page_sizes=[(500.0, 700.0), (500.0, 700.0)]
    )
    from pdf2zh.v3.ingestion.base import ingest_block_events

    gate = gate_quality(ingest_block_events(doc))
    assert gate.quality == QUALITY_PASS
    assert gate.failed_rules == []


def test_ingest_selection_and_runs_land_in_trace(tmp_path, sample_json):
    """trace 故事：mineru FAIL → marker(PASS, fallback_from=mineru) → ingest.select。"""
    from pdf2zh.v3.flight_recorder import FlightRecorder, read_events
    from pdf2zh.v3.ingestion import BACKEND_MINERU, decide
    from pdf2zh.v3.ingestion.base import (
        EVENT_INGEST_BEGIN,
        EVENT_INGEST_END,
        EVENT_INGEST_SELECT,
        emit_ingest_events,
        emit_ingest_run_failure,
        emit_ingest_selection,
    )
    from pdf2zh.v3.ingestion.selector import (
        QUALITY_FAIL,
        REASON_PRIMARY_PARSE_FAIL,
    )

    doc = marker_json_to_document(
        sample_json, pdf_page_sizes=[(500.0, 700.0), (500.0, 700.0)]
    )
    decision = decide(
        "auto",
        primary=BACKEND_MINERU,
        primary_quality=QUALITY_FAIL,
        fallback_available=True,
    )
    decision.reason = REASON_PRIMARY_PARSE_FAIL  # 本次是解析崩溃而非质量门
    decision.failed_rules = []
    decision.quality = "PASS"  # marker run 自身的 gate 结果

    trace_path = tmp_path / "trace.jsonl"
    with FlightRecorder(str(trace_path), book_id="sel-test", level=1) as rec:
        emit_ingest_run_failure(BACKEND_MINERU, "boom", rec, pdf_path="book.pdf")
        emit_ingest_events(
            doc, rec, pdf_path="book.pdf", status="PASS", fallback_from=BACKEND_MINERU
        )
        emit_ingest_selection(decision, rec, pdf_path="book.pdf")

    names = [(e["event"], e["payload"]) for e in read_events(str(trace_path))]
    begins = [p for e, p in names if e == EVENT_INGEST_BEGIN]
    ends = [p for e, p in names if e == EVENT_INGEST_END]
    assert begins[0]["backend"] == BACKEND_MINERU
    assert ends[0] == {
        "backend": BACKEND_MINERU,
        "blocks": 0,
        "status": "FAIL",
        "reason": "boom",
    }
    assert begins[1]["backend"] == "marker"
    assert begins[1]["fallback_from"] == BACKEND_MINERU
    assert ends[1]["status"] == "PASS"
    select = next(p for e, p in names if e == EVENT_INGEST_SELECT)
    assert select["decision"]["selected_backend"] == "marker"
    assert select["decision"]["fallback"] is True
    assert select["decision"]["reason"] == REASON_PRIMARY_PARSE_FAIL
    assert select["pdf"] == "book.pdf"


# ── v1.1: selector wired into the magicpdf CLI (auto 模式) ──────────────────


def _cli_parse_results():
    """SAMPLE_MIDDLE → MagicPdfAdapter results（单页两文本块的解析产物）。"""
    from tests.test_magicpdf_cli import SAMPLE_MIDDLE
    from pdf2zh.magicpdf_adapter import MagicPdfAdapter

    return MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)


def _cli_marker_doc() -> IngestDocument:
    """正常的 Marker IR（几何已声明 + 经 pdf 页尺寸归一化）供回退路径使用。"""
    return marker_json_to_document(
        {
            "block_type": "Document",
            "metadata": {},
            "children": [
                {
                    "id": "/page/0",
                    "block_type": "Page",
                    "html": "",
                    "bbox": [0, 0, 1000, 1400],
                    "children": [
                        {
                            "id": "/page/0/Text/1",
                            "block_type": "Text",
                            "html": "Hello MagicPDF",
                            "bbox": [0, 0, 500, 100],
                            "children": [],
                        }
                    ],
                }
            ],
        },
        pdf_page_sizes=[(500.0, 700.0)],
    )


def _run_cli(tmp_path, ingest_backend="auto", **patches):
    """Mock 掉 adapter/translator 后直跑 run_magicpdf_main（SAMPLE_MIDDLE）。

    ``patches``：{目标: 补丁值} —— 普通值（bool / lambda）或带 side_effect
    的 Mock（如 gate_quality 的分次返回值）。
    """
    from contextlib import ExitStack
    from unittest.mock import Mock, patch

    from tests.test_magicpdf_cli import make_args
    from pdf2zh.magicpdf_cli import run_magicpdf_main

    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 placeholder")
    fake_translator = Mock()
    fake_translator.translate = Mock(side_effect=lambda t: "T:" + t)
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                return_value=True,
            )
        )
        stack.enter_context(
            patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                return_value=_cli_parse_results(),
            )
        )
        stack.enter_context(
            patch(
                "pdf2zh.translator.build_translator",
                return_value=fake_translator,
            )
        )
        for target, value in patches.items():
            stack.enter_context(patch(target, value))
        code = run_magicpdf_main(
            make_args(
                files=[str(pdf_path)],
                output=str(tmp_path),
                ingest_backend=ingest_backend,
                trace=True,
            )
        )
    return code, tmp_path


def _trace_events(tmp_path):
    from pdf2zh.v3.flight_recorder import read_events

    return list(read_events(str(tmp_path / "trace" / "paper_events.jsonl")))


def test_cli_auto_marker_unavailable_keeps_mineru(tmp_path):
    """auto + Marker 不可用 → selected=mineru；raw + ingest.* + select 落盘。"""
    from pdf2zh.v3.ingestion import BACKEND_MINERU
    from pdf2zh.v3.ingestion.base import (
        EVENT_INGEST_BLOCK,
        EVENT_INGEST_RAW_BLOCK,
        EVENT_INGEST_SELECT,
        RAW_ORIGIN,
        RAW_SPACE,
        RAW_UNIT,
    )
    from pdf2zh.v3.ingestion.selector import REASON_PRIMARY_OK

    code, tmp = _run_cli(tmp_path, ingest_backend="auto")
    assert code == 0
    # mineru 主链路产物不变；无 Marker IR dump
    assert (tmp / "magicpdf" / "paper_document.json").exists()
    assert not (tmp / "magicpdf" / "paper_ingest.json").exists()
    events = _trace_events(tmp)
    blocks = [e for e in events if e["event"] == EVENT_INGEST_BLOCK]
    assert blocks and all(e["payload"]["backend"] == BACKEND_MINERU for e in blocks)
    # raw 证据先于 canonical 落盘，且 raw 块与 canonical 块共享 trace_id ——
    # explain 能把「MinerU 没给 geometry」与「adapter 丢 geometry」分开。
    raw = [e for e in events if e["event"] == EVENT_INGEST_RAW_BLOCK]
    assert raw, "mineru parse 成功必须产出 raw block 证据"
    assert events.index(raw[0]) < events.index(blocks[0])
    assert {e["trace_id"] for e in raw} == {e["trace_id"] for e in blocks}
    p0 = raw[0]["payload"]
    assert p0["source_backend"] == BACKEND_MINERU
    assert p0["box"] and p0["normalized"] is True
    assert p0["box_space"] == RAW_SPACE
    assert p0["box_origin"] == RAW_ORIGIN
    assert p0["box_unit"] == RAW_UNIT
    assert p0["page_height"] > 0 and p0["source_id"]
    select = next(e for e in events if e["event"] == EVENT_INGEST_SELECT)
    assert select["payload"]["decision"]["selected_backend"] == BACKEND_MINERU
    assert select["payload"]["decision"]["reason"] == REASON_PRIMARY_OK
    assert select["payload"]["decision"]["fallback"] is False
    assert not select["payload"]["decision"]["fallback_attempted"]


def test_cli_auto_quality_fail_falls_back_to_marker(tmp_path):
    """auto 质量门 FAIL + Marker 可用 → 回退 Marker；trace 故事完整落盘。"""
    from unittest.mock import Mock

    from pdf2zh.v3.ingestion import BACKEND_MINERU
    from pdf2zh.v3.ingestion.base import EVENT_INGEST_BEGIN, EVENT_INGEST_SELECT
    from pdf2zh.v3.ingestion.selector import (
        QUALITY_FAIL,
        QUALITY_PASS,
        REASON_PRIMARY_QUALITY_FAIL,
        GateResult,
    )

    gate_results = [
        # mineru primary：门 FAIL（几何未声明）
        GateResult(
            quality=QUALITY_FAIL,
            failed_rules=[RULE_INGEST_GEOMETRY_DECLARED],
            by_rule={RULE_INGEST_GEOMETRY_DECLARED: "HIGH"},
        ),
        # marker 回退：门 PASS
        GateResult(quality=QUALITY_PASS, failed_rules=[], by_rule={}),
    ]

    code, tmp = _run_cli(
        tmp_path,
        ingest_backend="auto",
        **{
            "pdf2zh.magicpdf_cli._marker_live_available": lambda: True,
            "pdf2zh.v3.ingestion.selector.gate_quality": Mock(
                side_effect=lambda events: gate_results.pop(0)
            ),
            "pdf2zh.v3.ingestion.marker_backend.MarkerBackend.ingest": (
                lambda self, pdf, trace=None, **kw: _cli_marker_doc()
            ),
        },
    )
    assert code == 0
    # Marker 真正服务了本次摄入：IR dump 存在
    assert (tmp / "magicpdf" / "paper_ingest.json").exists()
    events = _trace_events(tmp)
    begins = [e["payload"] for e in events if e["event"] == EVENT_INGEST_BEGIN]
    assert [b["backend"] for b in begins] == ["mineru", "marker"]
    assert begins[1]["fallback_from"] == BACKEND_MINERU
    select = next(e for e in events if e["event"] == EVENT_INGEST_SELECT)
    decision = select["payload"]["decision"]
    assert decision["selected_backend"] == "marker"
    assert decision["fallback"] is True
    assert decision["reason"] == REASON_PRIMARY_QUALITY_FAIL
    assert decision["quality"] == QUALITY_PASS  # 回退 run 自身的 gate 结果
    assert decision["fallback_attempted"] is True
    assert decision["fallback_succeeded"] is True  # run 结局由 CLI 如实更新
    assert RULE_INGEST_GEOMETRY_DECLARED in decision["failed_rules"]


def test_cli_auto_fallback_failure_keeps_mineru(tmp_path):
    """auto 回退失败 → 保留 MinerU 结果；决策如实改为 fallback_ingest_failed。"""
    from pdf2zh.v3.ingestion import BACKEND_MINERU, IngestionError
    from pdf2zh.v3.ingestion.base import EVENT_INGEST_SELECT
    from pdf2zh.v3.ingestion.selector import (
        QUALITY_FAIL,
        REASON_FALLBACK_RUN_FAILED,
        GateResult,
    )

    def _boom(self, pdf, trace=None, **kw):
        raise IngestionError("marker boom")

    code, tmp = _run_cli(
        tmp_path,
        ingest_backend="auto",
        **{
            "pdf2zh.magicpdf_cli._marker_live_available": lambda: True,
            "pdf2zh.v3.ingestion.selector.gate_quality": lambda events: GateResult(
                quality=QUALITY_FAIL,
                failed_rules=[RULE_INGEST_GEOMETRY_DECLARED],
                by_rule={RULE_INGEST_GEOMETRY_DECLARED: "HIGH"},
            ),
            "pdf2zh.v3.ingestion.marker_backend.MarkerBackend.ingest": _boom,
        },
    )
    assert code == 0
    # Marker 未服务；MinerU 产物保留
    assert not (tmp / "magicpdf" / "paper_ingest.json").exists()
    assert (tmp / "magicpdf" / "paper_document.json").exists()
    events = _trace_events(tmp)
    select = next(e for e in events if e["event"] == EVENT_INGEST_SELECT)
    decision = select["payload"]["decision"]
    assert decision["selected_backend"] == BACKEND_MINERU
    assert decision["fallback"] is False
    assert decision["reason"] == REASON_FALLBACK_RUN_FAILED
    assert decision["quality"] == QUALITY_FAIL  # 失败可见，不静默吞掉
    # 尝试过但未成功：attempt/success 分离，统计不会把 attempt 当 success
    assert decision["fallback_attempted"] is True
    assert decision["fallback_succeeded"] is False


# ── v1.1: P1 parse-crash → Marker fallback（同一 selector 决策模型）───────────


def test_cli_parse_crash_falls_back_to_marker(tmp_path):
    """auto + MinerU parse crash + Marker 可用 → Marker 兜底；trace 如实标记
    reason=mineru_parse_failed（不伪装成 quality failure）。"""
    from pdf2zh.v3.ingestion import BACKEND_MINERU
    from pdf2zh.v3.ingestion.base import (
        EVENT_INGEST_BEGIN,
        EVENT_INGEST_END,
        EVENT_INGEST_SELECT,
    )
    from pdf2zh.v3.ingestion.selector import REASON_PRIMARY_PARSE_FAIL

    def _boom_parse(*a, **k):
        raise RuntimeError("mineru crashed")

    code, tmp = _run_cli(
        tmp_path,
        ingest_backend="auto",
        **{
            "pdf2zh.magicpdf_cli._adapter_parse": _boom_parse,
            "pdf2zh.magicpdf_cli._marker_live_available": lambda: True,
            "pdf2zh.v3.ingestion.marker_backend.MarkerBackend.ingest": (
                lambda self, pdf, trace=None, **kw: _cli_marker_doc()
            ),
        },
    )
    assert code == 0
    assert (tmp / "magicpdf" / "paper_ingest.json").exists()  # Marker 真正服务
    events = _trace_events(tmp)
    begins = [e["payload"] for e in events if e["event"] == EVENT_INGEST_BEGIN]
    ends = [e["payload"] for e in events if e["event"] == EVENT_INGEST_END]
    assert begins[0]["backend"] == BACKEND_MINERU and begins[0]["blocks"] == 0
    assert ends[0]["status"] == "FAIL" and ends[0]["reason"].startswith("parse failed")
    assert begins[1]["backend"] == "marker"
    assert begins[1]["fallback_from"] == BACKEND_MINERU
    assert ends[1]["status"] == "PASS"
    select = next(e for e in events if e["event"] == EVENT_INGEST_SELECT)
    decision = select["payload"]["decision"]
    assert decision["selected_backend"] == "marker"
    assert decision["reason"] == REASON_PRIMARY_PARSE_FAIL
    assert decision["fallback"] is True
    assert decision["fallback_attempted"] is True
    assert decision["fallback_succeeded"] is True


def test_cli_parse_crash_marker_fails_degrades_with_trace(tmp_path):
    """auto + parse crash + Marker 也失败 → engine 降级；完整失败链进 trace，
    降级不吞掉 ingestion failure（mineru FAIL → marker FAIL fallback_from）。"""
    from unittest.mock import patch

    from pdf2zh.v3.ingestion import BACKEND_MINERU, IngestionError
    from pdf2zh.v3.ingestion.base import EVENT_INGEST_BEGIN, EVENT_INGEST_END

    def _boom_parse(*a, **k):
        raise RuntimeError("mineru crashed")

    def _boom_marker(self, pdf, trace=None, **kw):
        raise IngestionError("marker boom")

    with patch("pdf2zh.magicpdf_cli._fallback_legacy", return_value=7) as fb:
        code, tmp = _run_cli(
            tmp_path,
            ingest_backend="auto",
            **{
                "pdf2zh.magicpdf_cli._adapter_parse": _boom_parse,
                "pdf2zh.magicpdf_cli._marker_live_available": lambda: True,
                "pdf2zh.v3.ingestion.marker_backend.MarkerBackend.ingest": _boom_marker,
            },
        )
    assert code == 7
    fb.assert_called_once()
    events = _trace_events(tmp)
    begins = [e["payload"] for e in events if e["event"] == EVENT_INGEST_BEGIN]
    ends = [e["payload"] for e in events if e["event"] == EVENT_INGEST_END]
    assert [b["backend"] for b in begins] == [BACKEND_MINERU, "marker"]
    assert begins[1]["fallback_from"] == BACKEND_MINERU
    assert ends[0]["status"] == "FAIL" and ends[0]["reason"].startswith("parse failed")
    assert ends[1]["status"] == "FAIL"
    assert ends[1]["reason"].startswith("fallback failed")
    # engine 级降级没有 ingest.select —— 没有选中任何 backend
    from pdf2zh.v3.ingestion.base import EVENT_INGEST_SELECT

    assert not [e for e in events if e["event"] == EVENT_INGEST_SELECT]


def test_cli_parse_crash_forced_mineru_degrades_with_trace(tmp_path):
    """强制 mineru + parse crash → 直接 engine 降级，mineru run_failure 仍进 trace。"""
    from unittest.mock import patch

    from pdf2zh.v3.ingestion import BACKEND_MINERU
    from pdf2zh.v3.ingestion.base import EVENT_INGEST_BEGIN, EVENT_INGEST_END

    def _boom_parse(*a, **k):
        raise RuntimeError("mineru crashed")

    with patch("pdf2zh.magicpdf_cli._fallback_legacy", return_value=7):
        code, tmp = _run_cli(
            tmp_path,
            ingest_backend="mineru",
            **{"pdf2zh.magicpdf_cli._adapter_parse": _boom_parse},
        )
    assert code == 7
    events = _trace_events(tmp)
    begins = [e["payload"] for e in events if e["event"] == EVENT_INGEST_BEGIN]
    ends = [e["payload"] for e in events if e["event"] == EVENT_INGEST_END]
    assert len(begins) == 1 and begins[0]["backend"] == BACKEND_MINERU
    assert ends[0]["status"] == "FAIL"
