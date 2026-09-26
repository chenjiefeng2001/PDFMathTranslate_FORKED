from __future__ import annotations

import difflib
import html as html_lib
import json
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Mapping, Optional, Sequence

from pdf2zh.v3.ingestion.base import (
    BACKEND_JINA,
    JinaOcrCoverageError,
    JinaOcrSchemaError,
)
from pdf2zh.v3.ingestion.config import JinaOcrOptions
from pdf2zh.v3.ingestion.ir import (
    KIND_HEADING,
    KIND_IMAGE,
    KIND_LIST_ITEM,
    KIND_PARAGRAPH,
    KIND_TABLE,
    KIND_TABLE_CELL,
    IngestBlock,
    IngestBox,
    IngestDocument,
)


@dataclass
class JinaSemanticBlock:
    text: str
    kind: str = KIND_PARAGRAPH
    page_no: int = 0
    order: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "page_no": self.page_no,
            "order": self.order,
            "metadata": dict(self.metadata),
        }


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: Optional[list[str]] = None
        self.cell: Optional[list[str]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.cell is not None:
            value = _clean_inline("".join(self.cell))
            if self.row is None:
                self.row = []
            self.row.append(value)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.cell is not None:
            self.cell.append(html_lib.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self.cell is not None:
            self.cell.append(html_lib.unescape(f"&#{name};"))


class _InlineHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"br", "p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(html_lib.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.parts.append(html_lib.unescape(f"&#{name};"))


_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^\n]*?\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\n]*?\)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_JINA_REF_DET_RE = re.compile(
    r"<\|ref\|>(.*?)<\|/ref\|>\s*<\|det\|>.*?<\|/det\|>",
    flags=re.DOTALL,
)
_JINA_SPECIAL_RE = re.compile(r"<\|/?(?:ref|det)\|>")


def _clean_jina_special_tokens(value: str) -> str:
    text = str(value or "")

    def replace(match: re.Match[str]) -> str:
        return str(match.group(1) or "").strip()

    text = _JINA_REF_DET_RE.sub(replace, text)
    if _JINA_SPECIAL_RE.search(text):
        raise JinaOcrSchemaError("Jina OCR Markdown contains malformed ref/det tokens")
    return text


def _clean_inline(value: str) -> str:
    text = _clean_jina_special_tokens(value)
    text = _IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    protected: list[str] = []
    protect_pattern = re.compile(
        r"\$\$.+?\$\$|\$[^$\n]+\$|\\\(.+?\\\)|\\\[.+?\\\]|`[^`\n]+`",
        flags=re.DOTALL,
    )

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = protect_pattern.sub(protect, text)
    parser = _InlineHTMLParser()
    try:
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(
        r"(?<![A-Za-z0-9_])\*\*(?=\w)(.+?)(?<=\w)\*\*" r"(?![A-Za-z0-9_])",
        r"\1",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_*])\*(?=\w)(.+?)(?<=\w)\*(?![A-Za-z0-9_*])",
        r"\1",
        text,
        flags=re.DOTALL,
    )
    for index, protected_value in enumerate(protected):
        text = text.replace(f"\x00{index}\x00", protected_value)
    return " ".join(text.split())


def _table_text(source: str) -> tuple[str, list[list[str]]]:
    parser = _TableParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        rows = []
    rows = parser.rows
    if not rows:
        return _clean_inline(source), []
    normalized_rows = [
        [_clean_inline(cell) for cell in row]
        for row in rows
        if any(_clean_inline(cell) for cell in row)
    ]
    text = "\n".join(" | ".join(row) for row in normalized_rows)
    return text, normalized_rows


def _pipe_table(
    lines: Sequence[str], start: int
) -> Optional[tuple[str, list[list[str]], int]]:
    if start + 1 >= len(lines):
        return None
    if "|" not in lines[start] or not _TABLE_SEPARATOR_RE.match(lines[start + 1]):
        return None

    def cells(line: str) -> list[str]:
        value = line.strip()
        if value.startswith("|"):
            value = value[1:]
        if value.endswith("|"):
            value = value[:-1]
        return [_clean_inline(part) for part in value.split("|")]

    rows = [cells(lines[start])]
    end = start + 2
    while end < len(lines) and "|" in lines[end] and lines[end].strip():
        rows.append(cells(lines[end]))
        end += 1
    return "\n".join(" | ".join(row) for row in rows), rows, end


def _add_block(
    blocks: list[JinaSemanticBlock],
    text: str,
    kind: str,
    page_no: int,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    if kind == "code":
        value = (
            _clean_jina_special_tokens(text).replace("\r\n", "\n").replace("\r", "\n")
        )
    elif kind in {KIND_TABLE, KIND_TABLE_CELL} and "\n" in str(text or ""):
        value = "\n".join(
            _clean_inline(line)
            for line in str(text).splitlines()
            if _clean_inline(line)
        )
    else:
        value = _clean_inline(text)
    if not value:
        return
    blocks.append(
        JinaSemanticBlock(
            text=value,
            kind=kind,
            page_no=page_no,
            order=len(blocks),
            metadata=dict(metadata or {}),
        )
    )


def split_markdown(
    markdown: str,
    page_no: int = 0,
    *,
    table_cells: bool = False,
) -> list[JinaSemanticBlock]:
    text = (
        _clean_jina_special_tokens(markdown).replace("\r\n", "\n").replace("\r", "\n")
    )
    if text.strip().lower() == "null":
        return []
    lines = text.split("\n")
    blocks: list[JinaSemanticBlock] = []
    paragraph: list[str] = []
    list_open = False
    fence: Optional[str] = None
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            _add_block(blocks, " ".join(paragraph), KIND_PARAGRAPH, page_no)
            paragraph = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if fence is not None:
            if (
                _FENCE_RE.match(stripped)
                and stripped.startswith(fence[0])
                and len(stripped) >= len(fence)
            ):
                _add_block(blocks, "\n".join(code_lines), "code", page_no)
                code_lines = []
                fence = None
            else:
                code_lines.append(line)
            i += 1
            continue
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            flush_paragraph()
            list_open = False
            fence = fence_match.group(1)
            i += 1
            continue
        if not stripped:
            flush_paragraph()
            list_open = False
            i += 1
            continue
        if stripped.lower().startswith("<table"):
            flush_paragraph()
            end = i
            while end < len(lines) and "</table>" not in lines[end].lower():
                end += 1
            if end < len(lines):
                end += 1
            table_source = "\n".join(lines[i:end])
            table_text, rows = _table_text(table_source)
            if table_cells:
                for row_index, row in enumerate(rows):
                    for col_index, cell in enumerate(row):
                        _add_block(
                            blocks,
                            cell,
                            KIND_TABLE_CELL,
                            page_no,
                            {"row": row_index, "column": col_index},
                        )
            else:
                _add_block(
                    blocks,
                    table_text,
                    KIND_TABLE,
                    page_no,
                    {"rows": rows, "cells": [cell for row in rows for cell in row]},
                )
            i = end
            continue
        pipe = _pipe_table(lines, i)
        if pipe is not None:
            flush_paragraph()
            table_text, rows, end = pipe
            if table_cells:
                for row_index, row in enumerate(rows):
                    for col_index, cell in enumerate(row):
                        _add_block(
                            blocks,
                            cell,
                            KIND_TABLE_CELL,
                            page_no,
                            {"row": row_index, "column": col_index},
                        )
            else:
                _add_block(
                    blocks,
                    table_text,
                    KIND_TABLE,
                    page_no,
                    {"rows": rows, "cells": [cell for row in rows for cell in row]},
                )
            i = end
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            list_open = False
            level = len(heading.group(1))
            _add_block(
                blocks,
                heading.group(2),
                KIND_HEADING,
                page_no,
                {"level": level},
            )
            i += 1
            continue
        if re.match(r"^\s*(?:=+|-{3,})\s*$", line) and paragraph:
            title = " ".join(paragraph)
            paragraph = []
            list_open = False
            _add_block(blocks, title, KIND_HEADING, page_no)
            i += 1
            continue
        list_match = _LIST_RE.match(line)
        if list_match:
            flush_paragraph()
            _add_block(
                blocks,
                list_match.group(1),
                KIND_LIST_ITEM,
                page_no,
                {"marker": line.strip()[:1]},
            )
            list_open = True
            i += 1
            continue
        if re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", line):
            flush_paragraph()
            list_open = False
            i += 1
            continue
        if re.match(r"^\s*>\s?", line):
            flush_paragraph()
            paragraph.append(re.sub(r"^\s*>\s?", "", line))
            i += 1
            continue
        if _IMAGE_RE.fullmatch(stripped):
            flush_paragraph()
            alt = _IMAGE_RE.match(stripped)
            _add_block(blocks, alt.group(1) if alt else "", KIND_IMAGE, page_no)
            list_open = False
            i += 1
            continue
        if list_open and line.startswith((" ", "\t")):
            if blocks and blocks[-1].kind == KIND_LIST_ITEM:
                blocks[-1].text = _clean_inline(f"{blocks[-1].text} {stripped}")
            else:
                paragraph.append(stripped)
            i += 1
            continue
        paragraph.append(stripped)
        i += 1
    flush_paragraph()
    if code_lines:
        _add_block(blocks, "\n".join(code_lines), "code", page_no)
    for order, block in enumerate(blocks):
        block.order = order
    return blocks


def parse_markdown(
    markdown: str,
    page_no: int = 0,
    *,
    table_cells: bool = False,
) -> list[JinaSemanticBlock]:
    return split_markdown(markdown, page_no, table_cells=table_cells)


def _normal_text(value: Any) -> str:
    text = _clean_inline(str(value or ""))
    return " ".join(text.split()).strip()


def _tokens(value: Any) -> list[str]:
    text = _normal_text(value)
    found = re.findall(r"\w+", text, flags=re.UNICODE)
    result: list[str] = []
    for token in found:
        if all(ord(char) >= 0x2E80 for char in token):
            result.extend(char.casefold() for char in token)
        else:
            result.append(token.casefold())
    for match in re.findall(r"\$[^$]+\$|\\\(.+?\\\)|\\\[.+?\\\]", text):
        result.append(re.sub(r"\s+", "", match))
    return result


def _token_score(left: Any, right: Any) -> float:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 0.0
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    common = sum(size for _, _, size in matcher.get_matching_blocks())
    recall = common / max(1, min(len(a), len(b)))
    precision = common / max(1, max(len(a), len(b)))
    return max(0.0, min(1.0, 0.65 * recall + 0.35 * precision))


def _fuzzy_score(left: Any, right: Any) -> float:
    a = _normal_text(left)
    b = _normal_text(right)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()
    length_ratio = min(len(a), len(b)) / max(len(a), len(b))
    return max(ratio, _token_score(a, b)) * (0.5 + 0.5 * length_ratio)


def _block_value(block: Any, name: str, default: Any = "") -> Any:
    if isinstance(block, Mapping):
        return block.get(name, default)
    return getattr(block, name, default)


def _base_text(block: Any) -> str:
    return str(_block_value(block, "text", "") or "")


def _as_v3_box(value: Any) -> Optional[tuple[float, float, float, float]]:
    if isinstance(value, Mapping):
        value = value.get("v3_box")
    if isinstance(value, IngestBlock):
        value = value.v3_box
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in box):
        return None
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _block_geometry(block: Any) -> bool:
    return _as_v3_box(_block_value(block, "v3_box", None)) is not None


def _match_quality(base: Any, candidate: Any) -> tuple[float, str]:
    base_text = _base_text(base)
    candidate_text = _base_text(candidate)
    base_normal = _normal_text(base_text)
    candidate_normal = _normal_text(candidate_text)
    if base_normal == candidate_normal:
        return 1.0, "exact"
    if not base_normal or not candidate_normal:
        return 0.0, "none"
    length_ratio = min(len(base_normal), len(candidate_normal)) / max(
        len(base_normal), len(candidate_normal)
    )
    if length_ratio < 0.62:
        return 0.0, "none"
    base_tokens = _tokens(base_text)
    candidate_tokens = _tokens(candidate_text)
    if base_tokens and base_tokens == candidate_tokens:
        return 0.99, "token_exact"
    token_score = _token_score(base_text, candidate_text)
    common_tokens = len(set(base_tokens).intersection(set(candidate_tokens)))
    if token_score >= 0.64 and common_tokens >= 2:
        return token_score, "token_anchor"
    fuzzy = _fuzzy_score(base_text, candidate_text)
    if fuzzy >= 0.62:
        return fuzzy, "fuzzy"
    return fuzzy, "none"


def align_markdown_blocks(
    base_blocks: Sequence[Any],
    markdown_blocks: Sequence[Any],
    *,
    min_score: float = 0.62,
) -> dict[int, tuple[int, str]]:
    base = [
        block for block in base_blocks if _base_text(block) and _block_geometry(block)
    ]
    candidates = [block for block in markdown_blocks if _base_text(block)]
    matches: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    priorities = {"exact": 4, "token_exact": 4, "token_anchor": 3, "fuzzy": 2}
    for jina_index, candidate in enumerate(candidates):
        scored = []
        for base_index, block in enumerate(base):
            if base_index in used:
                continue
            score, method = _match_quality(block, candidate)
            if method != "none" or score >= min_score:
                scored.append((priorities.get(method, 1), score, method, base_index))
        if not scored:
            continue
        scored.sort(key=lambda item: (-item[0], -item[1], item[3]))
        _, score, method, base_index = scored[0]
        if method == "none" and score < min_score:
            continue
        matches[jina_index] = (base_index, method)
        used.add(base_index)
    ordered: dict[int, tuple[int, str]] = {}
    last = -1
    for jina_index in sorted(matches):
        base_index, method = matches[jina_index]
        if base_index < last:
            continue
        ordered[jina_index] = (base_index, method)
        last = base_index
    return ordered


def _validated_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError) as exc:
            raise JinaOcrSchemaError(f"invalid Jina OCR result JSON: {exc}") from exc
    try:
        from pdf2zh.kernel.jina_ocr_worker import validate_result
    except Exception as exc:
        raise JinaOcrSchemaError(
            f"Jina OCR result validator is unavailable: {exc}"
        ) from exc
    try:
        return validate_result(result)
    except Exception as exc:
        raise JinaOcrSchemaError(f"invalid Jina OCR result: {exc}") from exc


def _page_entries(result: Any) -> dict[int, str]:
    data = _validated_result(result)
    return {int(item["page"]): item["markdown"] for item in data["pages"]}


def _as_box(value: Any) -> Optional[IngestBox]:
    if isinstance(value, IngestBox):
        return value
    if isinstance(value, Mapping) and "space" in value:
        try:
            return IngestBox.from_dict(dict(value))
        except (TypeError, ValueError):
            return None
    return None


def _page_number(page: Any, fallback: int) -> int:
    value = _block_value(
        page,
        "page_no",
        _block_value(
            page,
            "page_num",
            _block_value(page, "page", _block_value(page, "page_index", fallback)),
        ),
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _page_dimensions(page: Any) -> tuple[float, float]:
    width_value = _block_value(page, "width", None)
    height_value = _block_value(page, "height", None)
    if width_value is None or height_value is None:
        size = _block_value(page, "page_size", None)
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            width_value, height_value = size[0], size[1]
    try:
        width = float(width_value or 0.0)
        height = float(height_value or 0.0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if not math.isfinite(width) or not math.isfinite(height):
        return 0.0, 0.0
    return width, height


def _raw_mineru_box(value: Any) -> Optional[IngestBox]:
    box = _as_v3_box(value)
    if box is None:
        return None
    return IngestBox(
        box[0],
        box[1],
        box[2],
        box[3],
        space="page_tl",
        origin="top-left",
        unit="pt",
        meaning="block",
        semantics={"y_direction": "down"},
    )


def _raw_mineru_v3_box(
    value: Any, height: float
) -> Optional[tuple[float, float, float, float]]:
    raw = _as_v3_box(value)
    if raw is None or height <= 0:
        return None
    projected = (raw[0], height - raw[3], raw[2], height - raw[1])
    return _as_v3_box(projected)


def _declared_v3_box(value: Any) -> Optional[IngestBox]:
    raw = _as_v3_box(value)
    return IngestBox(*raw) if raw is not None else None


def _base_document(base_pages: Any, title: str) -> tuple[IngestDocument, str]:
    if isinstance(base_pages, IngestDocument):
        return base_pages, base_pages.source_backend or "mineru"
    if base_pages is None:
        return IngestDocument(source_backend="mineru", title=title), "mineru"
    if isinstance(base_pages, Mapping) and "pages" in base_pages:
        try:
            return (
                IngestDocument.from_dict(dict(base_pages)),
                str(base_pages.get("source_backend") or "mineru"),
            )
        except (KeyError, TypeError, ValueError):
            base_pages = base_pages.get("pages") or []
    if isinstance(base_pages, Mapping):
        if (
            "pages" not in base_pages
            and "blocks" not in base_pages
            and "text" in base_pages
        ):
            base_pages = [
                {"page_num": 0, "width": 0.0, "height": 0.0, "blocks": [base_pages]}
            ]
        else:
            base_pages = [base_pages]
    if isinstance(base_pages, (list, tuple)) and base_pages:
        first = base_pages[0]
        from pdf2zh.v3.canonical_page import PageModel

        if isinstance(first, PageModel):
            from pdf2zh.v3.ingestion.adapter import existing_pages_to_document

            return existing_pages_to_document(base_pages), "existing_v3"
        if all(isinstance(item, IngestBlock) for item in base_pages):
            doc = IngestDocument(source_backend="mineru", title=title)
            doc.add_page(0, 0.0, 0.0)
            for index, block in enumerate(base_pages):
                doc.add_leaf(
                    block_id=block.block_id,
                    page_no=0,
                    block_type=block.block_type,
                    text=block.text,
                    box=block.box or _declared_v3_box(block.v3_box),
                    v3_box=_as_v3_box(block.v3_box),
                    source_backend="mineru",
                    source_id=block.source_id or f"mineru:0:{index}",
                    confidence=block.confidence,
                    metadata=dict(block.metadata),
                )
            return doc, "mineru"
        if all(
            isinstance(item, Mapping) and "blocks" not in item and "text" in item
            for item in base_pages
        ):
            base_pages = [
                {"page_num": 0, "width": 0.0, "height": 0.0, "blocks": list(base_pages)}
            ]
    from pdf2zh.v3.canonical_page import PageModel

    if isinstance(base_pages, PageModel):
        from pdf2zh.v3.ingestion.adapter import existing_pages_to_document

        return existing_pages_to_document([base_pages]), "existing_v3"
    if hasattr(base_pages, "page_num") and hasattr(base_pages, "blocks"):
        base_pages = [base_pages]
    doc = IngestDocument(source_backend="mineru", title=title)
    for fallback, page in enumerate(base_pages or []):
        page_no = _page_number(page, fallback)
        width, height = _page_dimensions(page)
        doc.add_page(page_no, width, height)
        raw_page_geometry = str(_block_value(page, "backend", "")).lower() in {
            "mineru",
            "magicpdf",
            "offline",
        }
        page_metadata = _block_value(page, "metadata", {}) or {}
        doc.page(page_no).metadata.update(dict(page_metadata))
        for index, block in enumerate(_block_value(page, "blocks", []) or []):
            if isinstance(block, IngestBlock):
                block_id = block.block_id
                kind = block.block_type
                text_value = block.text
                box = block.box
                v3_box = _as_v3_box(block.v3_box)
                if box is None:
                    box = _declared_v3_box(v3_box)
                source_id = block.source_id
                confidence = block.confidence
                metadata = dict(block.metadata)
            elif isinstance(block, Mapping):
                block_id = str(block.get("block_id") or f"p{page_no}_{index}")
                kind = str(
                    block.get("block_type") or block.get("kind") or KIND_PARAGRAPH
                )
                text_value = str(block.get("text") or "")
                box = _as_box(block.get("box"))
                v3_box = _as_v3_box(block.get("v3_box"))
                if v3_box is None and raw_page_geometry:
                    bbox_value = block.get("bbox")
                    v3_box = _raw_mineru_v3_box(bbox_value, height)
                    if box is None:
                        box = _raw_mineru_box(bbox_value)
                if box is None:
                    box = _declared_v3_box(v3_box)
                source_id = str(block.get("source_id") or "")
                confidence = block.get("confidence")
                metadata = dict(block.get("metadata") or {})
            else:
                block_id = f"p{page_no}_{index}"
                kind = str(_block_value(block, "kind", KIND_PARAGRAPH))
                text_value = str(_block_value(block, "text", "") or "")
                box = _as_box(_block_value(block, "box", None))
                v3_box = _as_v3_box(_block_value(block, "v3_box", None))
                if v3_box is None and raw_page_geometry:
                    bbox_value = _block_value(block, "bbox", None)
                    v3_box = _raw_mineru_v3_box(bbox_value, height)
                    if box is None:
                        box = _raw_mineru_box(bbox_value)
                if box is None:
                    box = _declared_v3_box(v3_box)
                source_id = str(_block_value(block, "source_id", "") or "")
                confidence = _block_value(block, "confidence", None)
                metadata = dict(_block_value(block, "metadata", {}) or {})
            doc.add_leaf(
                block_id=block_id,
                page_no=page_no,
                block_type=kind,
                text=text_value,
                box=box,
                v3_box=v3_box,
                source_backend="mineru",
                source_id=source_id or f"mineru:{page_no}:{index}",
                confidence=confidence,
                metadata=metadata,
            )
    return doc, "mineru"


def _device_matches(requested: str, actual: str) -> bool:
    requested_value = str(requested or "auto").lower()
    actual_value = str(actual or "auto").lower()
    if requested_value == "auto":
        return actual_value in {"auto", "cpu", "cuda", "cuda:0"}
    if requested_value == "cuda":
        return actual_value in {"cuda", "cuda:0"}
    return actual_value == requested_value


def _validate_result_options(data: dict[str, Any], options: JinaOcrOptions) -> None:
    for key, expected in (
        ("model", options.model),
        ("revision", options.revision),
        ("prompt", options.prompt),
        ("offline", options.offline),
        ("max_new_tokens", options.max_new_tokens),
    ):
        if data[key] != expected:
            raise JinaOcrSchemaError(f"Jina OCR result {key} does not match request")
    if not _device_matches(options.device, data["device"]):
        raise JinaOcrSchemaError("Jina OCR result device does not match request")


def jina_result_to_document(
    result: Any,
    base_pages: Any,
    options: Optional[JinaOcrOptions | Mapping[str, Any]] = None,
    title: str = "",
    *,
    expected_page_nos: Optional[set[int]] = None,
) -> IngestDocument:
    opts = (
        options
        if isinstance(options, JinaOcrOptions)
        else JinaOcrOptions.from_mapping(options)
    )
    title = str(title or "")
    checked_result = _validated_result(result)
    _validate_result_options(checked_result, opts)
    source_doc, source_backend = _base_document(base_pages, title)
    result_pages = _page_entries(checked_result)
    actual_pages = set(result_pages)
    expected_pages = (
        set(expected_page_nos)
        if expected_page_nos is not None
        else {page.page_no for page in source_doc.pages()}
    )
    if any(page < 0 for page in expected_pages):
        raise JinaOcrSchemaError("Jina OCR expected page numbers must be non-negative")
    if actual_pages != expected_pages:
        missing = sorted(expected_pages - actual_pages)
        extra = sorted(actual_pages - expected_pages)
        raise JinaOcrSchemaError(
            f"Jina OCR result page set mismatch; missing={missing}, extra={extra}"
        )
    doc = IngestDocument(source_backend=BACKEND_JINA, title=title)
    coverage_by_page: dict[int, float] = {}
    total_chars = 0
    matched_chars = 0
    total_blocks = 0
    matched_blocks = 0
    geometry_backend = (
        "mineru" if source_backend in {"", "existing_v3", "mineru"} else source_backend
    )
    source_pages = [page for page in source_doc.pages() if page.page_no in result_pages]
    if not source_pages:
        raise JinaOcrSchemaError("Jina OCR has no matching base pages")
    geometry_blocks = [
        block
        for page in source_pages
        for block in source_doc.page_blocks(page.page_no)
        if _block_geometry(block)
    ]
    if not geometry_blocks:
        raise JinaOcrSchemaError(
            "Jina OCR has no projectable v3 geometry in the base pages"
        )
    for source_page in source_pages:
        page_no = source_page.page_no
        if source_page.width_pt <= 0 or source_page.height_pt <= 0:
            raise JinaOcrSchemaError(
                f"Jina OCR page {page_no} has no valid page geometry"
            )
        raw_source_blocks = source_doc.page_blocks(page_no)
        source_blocks = [block for block in raw_source_blocks if _block_geometry(block)]
        if (
            any(_base_text(block).strip() for block in raw_source_blocks)
            and not source_blocks
        ):
            raise JinaOcrSchemaError(
                f"Jina OCR page {page_no} has no projectable v3 geometry"
            )
        page_text_blocks = [
            block for block in source_blocks if _base_text(block).strip()
        ]
        total_blocks += len(page_text_blocks)
        page_total_chars = sum(
            len(_normal_text(_base_text(block))) for block in page_text_blocks
        )
        total_chars += page_total_chars
        semantic = split_markdown(result_pages.get(page_no, ""), page_no)
        eligible_base = [
            (index, block)
            for index, block in enumerate(source_blocks)
            if _base_text(block).strip()
        ]
        local_matches = align_markdown_blocks(
            [block for _, block in eligible_base], semantic
        )
        matches = {
            jina_index: (eligible_base[base_index][0], method)
            for jina_index, (base_index, method) in local_matches.items()
        }
        inverse = {
            base_index: (jina_index, method)
            for jina_index, (base_index, method) in matches.items()
        }
        page_matched_chars = 0
        page_matched_blocks = 0
        doc.add_page(
            page_no,
            source_page.width_pt,
            source_page.height_pt,
            raw_box=source_page.raw_box,
            metadata=dict(source_page.metadata),
        )
        for index, source_block in enumerate(source_blocks):
            box = source_block.box or _declared_v3_box(source_block.v3_box)
            v3_box = _as_v3_box(source_block.v3_box)
            text_value = source_block.text
            metadata = dict(source_block.metadata)
            matched = inverse.get(index)
            if matched is not None and page_text_blocks:
                jina_index, method = matched
                text_value = semantic[jina_index].text
                metadata["text_source"] = "jina"
                metadata["jina_match"] = method
                metadata["jina_block_index"] = jina_index
                page_matched_blocks += 1
                match_score, _ = _match_quality(source_block, semantic[jina_index])
                page_matched_chars += min(
                    len(_normal_text(_base_text(source_block))),
                    len(_normal_text(text_value)),
                ) * max(0.0, min(1.0, match_score))
            else:
                metadata["text_source"] = "mineru_fallback"
                metadata["jina_match"] = "none"
            metadata["geometry_source"] = geometry_backend
            metadata["geometry_provenance"] = geometry_backend
            doc.add_leaf(
                block_id=source_block.block_id,
                page_no=page_no,
                block_type=source_block.block_type,
                text=text_value,
                box=box,
                v3_box=v3_box,
                parent_id=source_block.parent_id,
                source_backend=BACKEND_JINA,
                source_id=source_block.source_id
                or f"{geometry_backend}:{page_no}:{index}",
                confidence=source_block.confidence,
                metadata=metadata,
            )
        coverage = page_matched_chars / page_total_chars if page_total_chars else 0.0
        coverage_by_page[page_no] = round(coverage, 6)
        doc.page(page_no).metadata["jina_coverage"] = round(coverage, 6)
        matched_blocks += page_matched_blocks
        matched_chars += min(page_total_chars, page_matched_chars)
    coverage = matched_chars / total_chars if total_chars else 0.0
    if not result_pages or not any(value.strip() for value in result_pages.values()):
        raise JinaOcrCoverageError("Jina OCR provider returned no Markdown text")
    if total_chars <= 0 or not source_doc.blocks():
        raise JinaOcrSchemaError(
            "Jina OCR has no projectable v3 geometry in the base pages"
        )
    if coverage < opts.min_coverage:
        raise JinaOcrCoverageError(
            f"Jina OCR coverage {coverage:.3f} is below minimum {opts.min_coverage:.3f}"
        )
    doc.set_env(
        model=opts.model,
        revision=opts.revision,
        prompt=opts.prompt,
        max_new_tokens=opts.max_new_tokens,
        device=opts.device,
        offline=opts.offline,
        jina_coverage=round(coverage, 6),
        jina_coverage_by_page=coverage_by_page,
        jina_matched_blocks=matched_blocks,
        jina_base_blocks=total_blocks,
        geometry_source=geometry_backend,
        source_geometry_backend=source_backend,
    )
    return doc


__all__ = [
    "JinaSemanticBlock",
    "split_markdown",
    "parse_markdown",
    "align_markdown_blocks",
    "jina_result_to_document",
]
