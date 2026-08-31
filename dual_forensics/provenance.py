"""provenance — stage registry, node identity, and evidence record constructors.

Single source of truth for the forensic chain.  A **node** is a stable unit of
evidence that threads the whole pipe.  The model/plan layer already carries a
stable id ``p{page}_{index}`` (``pdf2zh.v3.document_model.block_id``); the
parser layer uses geometry block index; the render layer has no id and is
**matched back** to a model node by geometry (see :mod:`.diff`).

Stages (in divergence order)::

    STAGE_SOURCE       the original PDF page as authored
    STAGE_PARSER       the parsed canonical page tree (glyphs/blocks)
    STAGE_MODEL        the document model (kind/role/style relations)
    STAGE_TRANSLATION  source→translated text, per translation unit
    STAGE_LAYOUT       the settled render plan (src/dst box, font, path)
    STAGE_RENDER       the actual objects drawn in the target PDF
    STAGE_PDF          the visual/readable final PDF

``evidence_record`` builds the per-stage dict documented in 7H-1 §2.
"""

from __future__ import annotations

from typing import Any, Dict

STAGE_SOURCE = "source"
STAGE_PARSER = "parser"
STAGE_MODEL = "model"
STAGE_TRANSLATION = "translation"
STAGE_LAYOUT = "layout"
STAGE_RENDER = "render"
STAGE_PDF = "pdf"

STAGES = [
    STAGE_SOURCE,
    STAGE_PARSER,
    STAGE_MODEL,
    STAGE_TRANSLATION,
    STAGE_LAYOUT,
    STAGE_RENDER,
    STAGE_PDF,
]

_STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}

# dictionary of json-safe text constants
ID_SEP = "_"


def stage_index(stage: str) -> int:
    """0-based index of a stage (``model`` is 2, ``render`` is 5)."""
    return _STAGE_INDEX[stage]


def node_id(page: int, index: int) -> str:
    """Stable model-layer node id — identical to ``block_id`` upstream."""
    return f"p{page}{ID_SEP}{index}"


def make_id(*parts: Any) -> str:
    """Join id parts so embedded ``_page_index`` strings stay unambiguous."""
    return ID_SEP.join(str(p) for p in parts)


def evidence_record(schema_version: int = 1) -> Dict[str, Any]:
    """A per-node, per-stage evidence payload (7H-1 §2)."""
    return {
        "schema_version": schema_version,
        "node": {
            "node_id": None,  # p{page}_{index} when known
            "page_id": None,
            "primitive_index": None,  # parser-layer block index (0-based)
        },
        "source": {
            "text": None,
            "bbox": None,  # [x0,y0,x1,y1] v3 y-up
            "font": None,
            "font_size": None,
            "color": None,
            "z_order": None,
            "object_type": None,
            "source_span": None,
        },
        "model": {
            "node_type": None,  # kind (paragraph/heading/caption/code/...)
            "semantic_role": None,
            "parent": None,
            "reading_order": None,
            "bbox": None,
            "style": None,
            "translation_unit_id": None,
        },
        "translation": {
            "source_text": None,
            "translated_text": None,
            "segmentation": None,
            "translation_status": None,  # translated/partial/preserved/skip
        },
        "layout": {
            "target_bbox": None,
            "target_font": None,
            "target_font_size": None,
            "scale": None,
            "clipping": None,
            "collision": None,  # False / {"upper":..,"lower":..,"shift":..}
            "recovery": None,  # SHIFT_DOWN / NEXT_PAGE / preserved ...
            "render_path": None,
        },
        "render": {
            "pdf_object_id": None,  # fill 0-based object ref in target PDF
            "operator": None,  # Tj / TJ / XObject / path draw
            "final_bbox": None,  # y-down PDF coords as extracted
            "final_font": None,
            "final_font_size": None,
            "final_text": None,
            "is_image": False,
            "is_path": False,
        },
    }


def verdict_empty():
    return {
        "source": None,
        "parser": None,
        "model": None,
        "translation": None,
        "layout": None,
        "render": None,
        "pdf": None,
    }


__all__ = [
    "STAGE_SOURCE",
    "STAGE_PARSER",
    "STAGE_MODEL",
    "STAGE_TRANSLATION",
    "STAGE_LAYOUT",
    "STAGE_RENDER",
    "STAGE_PDF",
    "STAGES",
    "stage_index",
    "node_id",
    "make_id",
    "evidence_record",
    "verdict_empty",
]
