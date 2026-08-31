# -*- coding: utf-8 -*-
"""Commit 7A architecture assertion tests.

Verifies the architecture consolidation:

1. Unified TranslationUnit dispatch: list/toc/flow/preserve all go through
   ``block_translation_unit`` - one block, one TranslationUnit.
2. Unified render_payload: the render plan carries an explicit ``kind`` and
   the renderer dispatches on it (legacy fields kept as compatibility).
3. Geometry is consumed, never recomputed: payload geometry comes from the
   block/parse stage; render_payload never re-infers it
   (no ``level * constant`` / ``index * width`` patterns).
4. converter stays orchestration-only: no new semantic feature imports.
5. Renderers contain no ``looks_like`` heuristics: semantic detection ends
   in the semantic layer.
"""

import inspect

from pdf2zh.semantic.models import ListNode, TOCEntryNode
from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel
from pdf2zh.v3.document_model import (
    DocumentModel,
    render_plan_from_model,
    translate_document,
)
from pdf2zh.v3.render_payload import block_translation_unit, build_render_payload


def _code_lines(src: str) -> list:
    """Return code lines, excluding docstrings (incl. multi-line bodies)."""
    out = []
    in_doc = None
    for ln in src.splitlines():
        s = ln.strip()
        if not s:
            continue
        if in_doc:
            if s.endswith(in_doc):
                in_doc = None
            continue
        if s.startswith('"""') or s.startswith("'''"):
            in_doc = '"""' if s.startswith('"""') else "'''"
            if s.endswith(in_doc) and len(s) > 3:
                in_doc = None
            continue
        if s.startswith("#"):
            continue
        out.append(ln)
    return out


def _mk_page(blocks):
    page = PageModel(page_num=1)
    for b in blocks:
        page.blocks.append(b)
    return page


def _mk_model(blocks):
    m = DocumentModel()
    m.pages = [_mk_page(blocks)]
    return m


def _mk_block(text, kind="paragraph", **md):
    b = BlockModel(text=text, kind=kind, x0=40, y0=40, x1=500, y1=100)
    b.metadata.update(md)
    if md.get("toc_entries"):
        b.metadata["toc_entries"] = md["toc_entries"]
    return b


# -- 1. Unified TranslationUnit dispatch ---------------------------------


def test_unit_dispatch_flow():
    b = _mk_block("A normal paragraph")
    unit = block_translation_unit(b, lambda s: f"T_{s}")
    assert unit["kind"] == "flow"
    assert unit["translated"] == "T_A normal paragraph"
    assert unit["translate"] is True
    assert unit["payload"] is None


def test_unit_dispatch_preserve():
    b = _mk_block("x = a + b", kind="formula")
    unit = block_translation_unit(b, lambda s: f"T_{s}")
    assert unit["kind"] == "preserve"
    assert unit["translated"] == "x = a + b"
    assert unit["translate"] is False


def test_unit_dispatch_skip_empty():
    b = _mk_block("")
    unit = block_translation_unit(b, lambda s: s)
    assert unit["kind"] == "skip"


def test_unit_dispatch_toc():
    b = _mk_block(
        "2.1 Dataset",
        kind="toc",
        toc_entries=[
            {
                "title": "2.1 Dataset",
                "number": "",
                "title_only": "",
                "level": 1,
                "page_number": "15",
                "title_x": 96.0,
                "page_x": 540.0,
                "indent": 96.0,
                "dot_leader": "......",
                "leader_present": True,
                "continuation": [],
                "bbox": [96.0, 40.0, 540.0, 60.0],
            }
        ],
    )
    calls = []
    unit = block_translation_unit(b, lambda s: calls.append(s) or f"T_{s}")
    assert unit["kind"] == "toc"
    # only title_only ("Dataset") reaches the translator
    assert unit["payload"]["entries"][0]["translated_title"] == "T_Dataset"
    assert calls == ["Dataset"]
    assert unit["payload"]["entries"][0]["page_number"] == "15"


def test_unit_dispatch_toc_heading_ref():
    head = _mk_block("Dataset", kind="heading")
    head.x0, head.x1, head.y0, head.y1 = 40, 300, 300, 315
    toc_b = _mk_block(
        "2.1 Dataset",
        kind="toc",
        toc_entries=[
            {
                "title": "2.1 Dataset",
                "number": "",
                "title_only": "",
                "level": 1,
                "page_number": "15",
                "title_x": 96.0,
                "page_x": 540.0,
                "indent": 96.0,
                "dot_leader": "",
                "leader_present": False,
                "continuation": [],
                "bbox": [96.0, 40.0, 540.0, 60.0],
            }
        ],
    )
    model = _mk_model([toc_b, head])
    unit = block_translation_unit(toc_b, lambda s: f"T_{s}", model=model)
    assert unit["kind"] == "toc"
    # heading_ref resolved (heading block id = p1_1)
    assert unit["payload"]["entries"][0]["heading_ref"] == "p1_1"


def test_unit_dispatch_list():
    b = _mk_block("1. First item\n2. Second item", kind="list")
    b.lines = [
        LineModel(text="1. First item", x0=40, x1=200, y0=40, y1=50),
        LineModel(text="2. Second item", x0=40, x1=210, y0=52, y1=62),
    ]
    calls = []
    unit = block_translation_unit(b, lambda s: calls.append(s) or f"T_{s}")
    assert unit["kind"] == "list"
    assert unit["payload"]["commands"]
    # markers never reach the translator (only content does)
    assert not any(c.startswith("1.") or c.startswith("2.") for c in calls)


# -- 2. Unified render_payload (render plan carries kind) -----------------


def test_render_plan_carries_payload_kind():
    flow = _mk_block("Hello")
    toc_b = _mk_block(
        "1. Intro",
        kind="toc",
        toc_entries=[
            {
                "title": "1. Intro",
                "number": "",
                "title_only": "",
                "level": 0,
                "page_number": "3",
                "title_x": 72.0,
                "page_x": 500.0,
                "indent": 72.0,
                "dot_leader": "......",
                "leader_present": True,
                "continuation": [],
                "bbox": [72.0, 40.0, 500.0, 60.0],
            }
        ],
    )
    model = _mk_model([flow, toc_b])
    translate_document(model, lambda s: f"T_{s}")
    plan = render_plan_from_model(model)
    by_kind = {p["render_payload"]["kind"] for p in plan}
    assert "flow" in by_kind
    assert "toc" in by_kind
    toc = [p for p in plan if p["kind"] == "toc"][0]
    assert toc["render_payload"]["kind"] == "toc"
    assert toc["render_payload"]["commands"]
    # legacy fields kept for compatibility
    assert "toc_entries" in toc
    assert "toc_commands" in toc
    # render_payload entries carry translated_title (no double translation)
    assert toc["render_payload"]["entries"][0]["translated_title"] == "T_Intro"


def test_render_payload_builder():
    unit = {
        "kind": "toc",
        "payload": {"entries": [{"title": "A"}], "commands": [{"text": "A"}]},
    }
    rp = build_render_payload(unit)
    assert rp["kind"] == "toc"
    assert len(rp["commands"]) == 1
    flow = {"kind": "flow", "payload": None}
    assert build_render_payload(flow)["commands"] == []


# -- 3. Geometry is consumed, never recomputed ----------------------------


def test_render_payload_geometry_is_passthrough():
    """render_payload must not re-infer geometry (no level*const / index*width).

    ``page_width`` is a legit parameter name in build_page_toc_payload; the
    assertion only targets multiplication-based recomputation in code lines.
    """
    import pdf2zh.v3.render_payload as mod

    src = inspect.getsource(mod)
    code_lines = _code_lines(src)
    assert not any(
        "level *" in ln or "* level" in ln or "index *" in ln for ln in code_lines
    )


def test_toc_geometry_not_recomputed_in_renderer():
    """TocRenderer must not recompute title_x/page_x from level/index (6C)."""
    from pdf2zh.semantic.renderer import toc as toc_mod

    src = inspect.getsource(toc_mod)
    code_lines = _code_lines(src)
    assert not any("level *" in ln or "* level" in ln for ln in code_lines)


# -- 4. converter stays orchestration-only --------------------------------


def test_converter_imports_only_semantic_sidechannel():
    """converter adds no list/toc/style feature imports; the unified dispatch
    entry (render_payload) and semantic feature side-channels are absent.

    paragraph_batch / mainline_wiring are pre-existing orchestration infra
    and are allowed to remain.
    """
    import pdf2zh.converter as conv

    src = inspect.getsource(conv)
    imports = [
        ln
        for ln in src.splitlines()
        if "from pdf2zh.v3" in ln or "import pdf2zh.v3" in ln
    ]
    forbidden = ["list_sidechannel", "toc_sidechannel", "render_payload"]
    assert not any(any(f in ln for f in forbidden) for ln in imports)


def test_converter_line_count_budget():
    """converter.py stays <= 1108 lines (7A 预算，7I-3B 钩子后浮动)。"""
    import os

    here = os.path.dirname(__file__)
    conv_path = os.path.join(here, "..", "pdf2zh", "converter.py")
    with open(conv_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) <= 1108, f"converter.py over budget: {len(lines)} lines"


# -- 5. Renderers have no looks_like heuristics ---------------------------


def test_renderers_no_looks_like_heuristics():
    """Semantic detection (looks_like_list / looks_like_toc) ends in the
    semantic layer; renderers must not do shape probing."""
    import pdf2zh.semantic.renderer.list as list_mod
    import pdf2zh.semantic.renderer.toc as toc_mod

    for mod in (list_mod, toc_mod):
        src = inspect.getsource(mod)
        assert "looks_like" not in src
        assert "isinstance(" not in src


# -- Unified entry equivalence: legacy fields still written back -----------


def test_legacy_fields_still_written():
    toc_b = _mk_block(
        "1. Intro",
        kind="toc",
        toc_entries=[
            {
                "title": "1. Intro",
                "number": "",
                "title_only": "",
                "level": 0,
                "page_number": "3",
                "title_x": 72.0,
                "page_x": 500.0,
                "indent": 72.0,
                "dot_leader": "......",
                "leader_present": True,
                "continuation": [],
                "bbox": [72.0, 40.0, 500.0, 60.0],
            }
        ],
    )
    model = _mk_model([toc_b])
    translate_document(model, lambda s: f"T_{s}")
    md = model.pages[0].blocks[0].metadata
    assert "toc_entries" in md
    assert "toc_commands" in md
    # real-pipeline format ("1. Intro" -> number=1. / title=Intro)
    assert md["toc_entries"][0]["translated_title"] == "T_Intro"
    assert md["toc_entries"][0]["number"].strip() == "1."
    assert md["toc_entries"][0]["page_number"] == "3"
