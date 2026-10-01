from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

BACKEND_EXISTING = "existing_v3"
BACKEND_MINERU = "mineru"
BACKEND_MARKER = "marker"
BACKEND_JINA = "jina"
BACKEND_IDS = (
    BACKEND_EXISTING,
    BACKEND_MINERU,
    BACKEND_MARKER,
    BACKEND_JINA,
)

REQUEST_AUTO = "auto"
REQUEST_MINERU = BACKEND_MINERU
REQUEST_MARKER = BACKEND_MARKER
REQUEST_JINA = BACKEND_JINA
INGEST_REQUEST_CHOICES = (
    REQUEST_AUTO,
    REQUEST_MINERU,
    REQUEST_MARKER,
    REQUEST_JINA,
)
AUTO_CANDIDATES = (BACKEND_MINERU, BACKEND_MARKER)
DEFAULT_CANDIDATES = AUTO_CANDIDATES

JINA_MODEL_ID = "jinaai/jina-ocr-v1"
JINA_REVISION = "904f815deed0fbeab02d82d4648b70106272ffe3"
JINA_PROMPT = "Transcribe the provided document image into a clean Markdown format, preserving the natural reading order."
JINA_MIN_COVERAGE = 0.20
JINA_DEFAULT_DPI = 200
JINA_DEFAULT_MAX_PIXELS = 4_000_000
JINA_DEFAULT_MAX_NEW_TOKENS = 4096
JINA_DEFAULT_TIMEOUT = 3600.0
JINA_MIN_DPI = 72
JINA_MAX_DPI = 600
JINA_MIN_PIXELS = 1_000_000
JINA_MAX_PIXELS = 64_000_000
JINA_MIN_OUTPUT_TOKENS = 1
JINA_MAX_OUTPUT_TOKENS = 32_768
JINA_MAX_PAGE_SELECTION = 10_000
JINA_MIN_TIMEOUT = 1.0
JINA_MAX_TIMEOUT = 86_400.0

PARSE_ENGINE_CHOICES = ("auto", "legacy", "babeldoc", "magicpdf")

WORKER_MANIFEST_SCHEMA = "pdf2zh.jina-ocr.manifest"
WORKER_RESULT_SCHEMA = "pdf2zh.jina-ocr.result"
WORKER_SCHEMA_VERSION = 1


def normalize_ingest_backend(raw: Optional[str]) -> str:
    value = str(raw or "").strip().lower()
    return value if value in INGEST_REQUEST_CHOICES else REQUEST_AUTO


def normalize_parse_engine(raw: Optional[str]) -> str:
    value = str(raw or "auto").strip().lower() or "auto"
    if value not in PARSE_ENGINE_CHOICES:
        raise ValueError(
            "parse_engine must be one of: " + ", ".join(PARSE_ENGINE_CHOICES)
        )
    return value


def normalize_jina_device(raw: Optional[str]) -> str:
    value = (
        "auto"
        if raw is None or (isinstance(raw, str) and not raw.strip())
        else str(raw).strip().lower()
    )
    if value == "gpu":
        value = "cuda"
    if value == "cuda":
        value = "cuda:0"
    if re.fullmatch(r"cuda:\d+", value):
        return value
    if value in {"auto", "cpu"}:
        return value
    raise ValueError("Jina OCR device must be auto, cpu, cuda, or cuda:<index>")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _parse_page_token(part: Any, *, max_range: int) -> set[int]:
    token = str(part).strip()
    if not token:
        raise ValueError("page selection contains an empty item")
    if re.fullmatch(r"-?\d+", token):
        value = int(token)
        if value < 0:
            raise ValueError(f"page index must be non-negative: {value}")
        return {value}
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
    if match is None:
        raise ValueError(f"invalid page index: {token!r}")
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise ValueError(f"page range must be ascending: {token!r}")
    if end - start + 1 > max_range:
        raise ValueError(
            f"page range is too large; maximum supported pages is {max_range}"
        )
    return set(range(start, end + 1))


def normalize_page_indices(
    pages: Any,
    *,
    strict: bool = False,
    max_range: int = JINA_MAX_PAGE_SELECTION,
) -> Optional[list[int]]:
    if pages is None or pages == "" or str(pages).strip().lower() == "all":
        return None
    if isinstance(pages, str):
        parts: Any = pages.split(",")
    elif isinstance(pages, (list, tuple, set)):
        if strict and not pages:
            raise ValueError("page selection must not be empty")
        parts = pages
    else:
        parts = [pages]
    if strict and isinstance(parts, (list, tuple, set)) and not parts:
        raise ValueError("page selection must not be empty")
    selected: set[int] = set()
    for part in parts:
        try:
            values = _parse_page_token(part, max_range=max_range)
            if len(selected) + len(values) > max_range:
                raise ValueError(
                    f"page selection is too large; maximum supported pages is {max_range}"
                )
            selected.update(values)
        except (TypeError, ValueError):
            if strict:
                raise
    return sorted(selected) if selected else None


def normalize_user_page_indices(
    pages: Any,
    *,
    max_range: int = JINA_MAX_PAGE_SELECTION,
) -> Optional[list[int]]:
    if pages is None or pages == "" or str(pages).strip().lower() == "all":
        return None
    if not isinstance(pages, str):
        raw = normalize_page_indices(pages, strict=True, max_range=max_range)
        if raw is None:
            return None
        if any(index < 1 for index in raw):
            raise ValueError("page numbers are one-based and must be positive")
        return [index - 1 for index in raw]
    selected: set[int] = set()
    for part in pages.split(","):
        token = part.strip()
        if not token:
            raise ValueError("page selection contains an empty item")
        if re.fullmatch(r"\d+", token):
            value = int(token)
            if value < 1:
                raise ValueError("page numbers are one-based and must be positive")
            selected.add(value - 1)
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if match is None:
            raise ValueError(f"invalid page range: {token!r}")
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            raise ValueError(f"page range must be ascending and one-based: {token!r}")
        if end - start + 1 > max_range:
            raise ValueError(
                f"page range is too large; maximum supported pages is {max_range}"
            )
        selected.update(range(start - 1, end))
        if len(selected) > max_range:
            raise ValueError(
                f"page selection is too large; maximum supported pages is {max_range}"
            )
    if not selected:
        raise ValueError("page selection must select at least one page")
    return sorted(selected)


def normalize_jina_page_indices(
    pages: Any,
    *,
    max_range: int = JINA_MAX_PAGE_SELECTION,
) -> Optional[list[int]]:
    return normalize_page_indices(pages, strict=True, max_range=max_range)


@dataclass(frozen=True)
class JinaOcrOptions:
    model: str = JINA_MODEL_ID
    revision: str = JINA_REVISION
    prompt: str = JINA_PROMPT
    max_new_tokens: int = JINA_DEFAULT_MAX_NEW_TOKENS
    device: str = "auto"
    offline: bool = False
    dpi: int = JINA_DEFAULT_DPI
    max_pixels: int = JINA_DEFAULT_MAX_PIXELS
    timeout: float = JINA_DEFAULT_TIMEOUT
    cache_dir: Optional[str] = None
    min_coverage: float = JINA_MIN_COVERAGE
    pages: Any = None

    def __post_init__(self) -> None:
        model = str(self.model or "").strip()
        if not model:
            raise ValueError("Jina OCR model must not be empty")
        revision = str(self.revision or "").strip()
        try:
            model_path = Path(model).expanduser()
            resolved_model = model_path.resolve()
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid Jina OCR model path: {model!r}") from exc
        try:
            local_model = model_path.exists()
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid Jina OCR model path: {model!r}") from exc
        if local_model:
            model = str(resolved_model)
        elif model != JINA_MODEL_ID and revision in {"", JINA_REVISION}:
            raise ValueError("remote Jina OCR models require an explicit revision")
        elif not revision:
            raise ValueError("remote Jina OCR models require an explicit revision")
        device = normalize_jina_device(self.device)
        dpi = _bounded_int(self.dpi, "dpi", JINA_MIN_DPI, JINA_MAX_DPI)
        max_pixels = _bounded_int(
            self.max_pixels, "max_pixels", JINA_MIN_PIXELS, JINA_MAX_PIXELS
        )
        max_new_tokens = _bounded_int(
            self.max_new_tokens,
            "max_new_tokens",
            JINA_MIN_OUTPUT_TOKENS,
            JINA_MAX_OUTPUT_TOKENS,
        )
        timeout = _bounded_float(
            self.timeout, "timeout", JINA_MIN_TIMEOUT, JINA_MAX_TIMEOUT
        )
        min_coverage = _bounded_float(self.min_coverage, "min_coverage", 0.0, 1.0)
        prompt = str(self.prompt if self.prompt is not None else JINA_PROMPT)
        if not prompt.strip():
            raise ValueError("Jina OCR prompt must not be empty")
        cache_dir = str(self.cache_dir).strip() if self.cache_dir else None
        if cache_dir:
            cache_dir = str(Path(cache_dir).expanduser().resolve())
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "offline", _as_bool(self.offline))
        object.__setattr__(self, "dpi", dpi)
        object.__setattr__(self, "max_pixels", max_pixels)
        object.__setattr__(self, "max_new_tokens", max_new_tokens)
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "cache_dir", cache_dir)
        object.__setattr__(self, "min_coverage", min_coverage)
        object.__setattr__(self, "pages", normalize_jina_page_indices(self.pages))

    @property
    def render_policy(self) -> dict[str, Any]:
        return {
            "dpi": self.dpi,
            "max_pixels": self.max_pixels,
            "color_space": "RGB",
            "format": "PNG",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "revision": self.revision,
            "prompt": self.prompt,
            "max_new_tokens": self.max_new_tokens,
            "device": self.device,
            "offline": self.offline,
            "dpi": self.dpi,
            "max_pixels": self.max_pixels,
            "timeout": self.timeout,
            "cache_dir": self.cache_dir,
            "min_coverage": self.min_coverage,
            "pages": self.pages,
        }

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "JinaOcrOptions":
        return cls(**dict(value or {}))

    @classmethod
    def from_env(cls, **overrides: Any) -> "JinaOcrOptions":
        data: dict[str, Any] = {
            "model": os.environ.get("PDF2ZH_JINA_MODEL", JINA_MODEL_ID),
            "revision": os.environ.get("PDF2ZH_JINA_REVISION", JINA_REVISION),
            "prompt": os.environ.get("PDF2ZH_JINA_PROMPT", JINA_PROMPT),
            "max_new_tokens": os.environ.get(
                "PDF2ZH_JINA_MAX_NEW_TOKENS", JINA_DEFAULT_MAX_NEW_TOKENS
            ),
            "device": os.environ.get("PDF2ZH_JINA_DEVICE", "auto"),
            "offline": os.environ.get("PDF2ZH_JINA_OFFLINE", ""),
            "dpi": os.environ.get("PDF2ZH_JINA_DPI", JINA_DEFAULT_DPI),
            "max_pixels": os.environ.get(
                "PDF2ZH_JINA_MAX_PIXELS", JINA_DEFAULT_MAX_PIXELS
            ),
            "timeout": os.environ.get("PDF2ZH_JINA_TIMEOUT", JINA_DEFAULT_TIMEOUT),
            "cache_dir": os.environ.get("PDF2ZH_JINA_CACHE_DIR"),
            "min_coverage": os.environ.get(
                "PDF2ZH_JINA_MIN_COVERAGE", JINA_MIN_COVERAGE
            ),
        }
        data.update(overrides)
        return cls.from_mapping(data)

    def replace(self, **changes: Any) -> "JinaOcrOptions":
        data = self.to_dict()
        data.update(changes)
        return type(self).from_mapping(data)


__all__ = [
    "BACKEND_EXISTING",
    "BACKEND_MINERU",
    "BACKEND_MARKER",
    "BACKEND_JINA",
    "BACKEND_IDS",
    "REQUEST_AUTO",
    "REQUEST_MINERU",
    "REQUEST_MARKER",
    "REQUEST_JINA",
    "INGEST_REQUEST_CHOICES",
    "AUTO_CANDIDATES",
    "DEFAULT_CANDIDATES",
    "JINA_MODEL_ID",
    "JINA_REVISION",
    "JINA_PROMPT",
    "JINA_MIN_COVERAGE",
    "JINA_DEFAULT_DPI",
    "JINA_DEFAULT_MAX_PIXELS",
    "JINA_DEFAULT_MAX_NEW_TOKENS",
    "JINA_DEFAULT_TIMEOUT",
    "JINA_MIN_DPI",
    "JINA_MAX_DPI",
    "JINA_MIN_PIXELS",
    "JINA_MAX_PIXELS",
    "JINA_MIN_OUTPUT_TOKENS",
    "JINA_MAX_OUTPUT_TOKENS",
    "JINA_MAX_PAGE_SELECTION",
    "JINA_MIN_TIMEOUT",
    "JINA_MAX_TIMEOUT",
    "PARSE_ENGINE_CHOICES",
    "normalize_parse_engine",
    "WORKER_MANIFEST_SCHEMA",
    "WORKER_RESULT_SCHEMA",
    "WORKER_SCHEMA_VERSION",
    "normalize_ingest_backend",
    "normalize_jina_device",
    "normalize_page_indices",
    "normalize_user_page_indices",
    "normalize_jina_page_indices",
    "JinaOcrOptions",
]
