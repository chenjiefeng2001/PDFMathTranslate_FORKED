from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pdf2zh.v3.ingestion.base import (
    BACKEND_JINA,
    JinaOcrBackendUnavailable,
    JinaOcrCoverageError,
    JinaOcrDeviceError,
    JinaOcrError,
    JinaOcrModelUnavailable,
    JinaOcrOfflineError,
    JinaOcrSchemaError,
    JinaOcrTimeoutError,
    JinaOcrWorkerError,
    IngestionError,
    emit_ingest_events,
)
from pdf2zh.v3.ingestion.config import (
    JinaOcrOptions,
    normalize_jina_page_indices,
)
from pdf2zh.v3.ingestion.jina_adapter import jina_result_to_document


class JinaOcrInputError(IngestionError):
    pass


_INFERENCE_LOCK = threading.Lock()


@dataclass
class RenderedJinaPage:
    page_no: int
    path: str
    image_sha256: str
    width_px: int
    height_px: int
    policy: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page_no,
            "path": self.path,
            "image_sha256": self.image_sha256,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "policy": dict(self.policy),
        }


def _open_pymupdf():
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError as exc:
            raise JinaOcrBackendUnavailable("PyMuPDF is required for Jina OCR") from exc
    return pymupdf


def _page_indices(document: Any, pages: Any) -> list[int]:
    count = int(getattr(document, "page_count", 0) or 0)
    try:
        selected = normalize_jina_page_indices(pages)
    except (TypeError, ValueError) as exc:
        raise JinaOcrInputError(str(exc)) from exc
    if selected is None:
        return list(range(count))
    if any(index < 0 or index >= count for index in selected):
        raise JinaOcrInputError(
            f"page selection out of range for {count} pages: {selected}"
        )
    if not selected:
        raise JinaOcrInputError("page selection must select at least one page")
    return selected


def _rect_values(rect: Any) -> tuple[float, float, float, float]:
    if hasattr(rect, "x0"):
        return tuple(float(getattr(rect, name)) for name in ("x0", "y0", "x1", "y1"))
    return tuple(float(value) for value in rect)


def _maybe_rect_values(rect: Any) -> Optional[list[float]]:
    if rect is None:
        return None
    try:
        return [float(value) for value in _rect_values(rect)]
    except (TypeError, ValueError):
        return None


def _visual_rect(page: Any) -> tuple[float, float, float, float]:
    return _rect_values(page.rect)


def _render_policy(page: Any, options: JinaOcrOptions, scale: float) -> dict[str, Any]:
    x0, y0, x1, y1 = _visual_rect(page)
    crop = getattr(page, "cropbox", None)
    media = getattr(page, "mediabox", None)
    return {
        "dpi": options.dpi,
        "max_pixels": options.max_pixels,
        "scale": round(float(scale), 8),
        "rotation": int(getattr(page, "rotation", 0) or 0),
        "visual_rect": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
        "cropbox": _maybe_rect_values(crop),
        "mediabox": _maybe_rect_values(media),
        "color_space": "RGB",
        "format": "PNG",
    }


def render_page(
    document: Any,
    page_index: int,
    output_dir: str | os.PathLike[str],
    options: JinaOcrOptions,
) -> RenderedJinaPage:
    pymupdf = _open_pymupdf()
    try:
        if hasattr(document, "load_page"):
            page = document.load_page(page_index)
        else:
            page = document[page_index]
    except Exception as exc:
        raise JinaOcrInputError(f"cannot load PDF page {page_index}: {exc}") from exc
    x0, y0, x1, y1 = _visual_rect(page)
    width_pt = abs(x1 - x0)
    height_pt = abs(y1 - y0)
    if width_pt <= 0 or height_pt <= 0:
        raise JinaOcrInputError(f"PDF page {page_index} has an empty visual rectangle")
    requested_scale = options.dpi / 72.0
    area = width_pt * height_pt
    max_scale = math.sqrt(options.max_pixels / area) if area > 0 else requested_scale
    scale = (
        min(requested_scale, max_scale) if options.max_pixels > 0 else requested_scale
    )
    scale = max(scale, 0.001)
    try:
        while True:
            matrix = pymupdf.Matrix(scale, scale)
            try:
                pixmap = page.get_pixmap(
                    matrix=matrix,
                    clip=page.rect,
                    alpha=False,
                    colorspace=pymupdf.csRGB,
                )
            except TypeError:
                pixmap = page.get_pixmap(
                    matrix=matrix,
                    alpha=False,
                    colorspace=pymupdf.csRGB,
                )
            image_bytes = pixmap.tobytes("png")
            width_px = int(pixmap.width)
            height_px = int(pixmap.height)
            if (
                options.max_pixels <= 0
                or width_px * height_px <= options.max_pixels
                or scale <= 0.001
            ):
                break
            scale = max(
                0.001,
                scale * math.sqrt(options.max_pixels / (width_px * height_px)) * 0.99,
            )
    except Exception as exc:
        raise JinaOcrInputError(f"cannot render PDF page {page_index}: {exc}") from exc
    if width_px <= 0 or height_px <= 0:
        raise JinaOcrInputError(f"rendered PDF page {page_index} is empty")
    if options.max_pixels > 0 and width_px * height_px > options.max_pixels:
        raise JinaOcrInputError(
            f"rendered PDF page {page_index} exceeds max_pixels={options.max_pixels}"
        )
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"page-{page_index:06d}.png"
    target.write_bytes(image_bytes)
    digest = hashlib.sha256(image_bytes).hexdigest()
    return RenderedJinaPage(
        page_no=page_index,
        path=str(target),
        image_sha256=digest,
        width_px=width_px,
        height_px=height_px,
        policy=_render_policy(page, options, scale),
    )


def render_pdf_pages(
    pdf_path: str,
    output_dir: str | os.PathLike[str],
    pages: Any = None,
    options: Optional[JinaOcrOptions] = None,
) -> list[RenderedJinaPage]:
    opts = options or JinaOcrOptions()
    if not os.path.isfile(pdf_path):
        raise JinaOcrInputError(f"PDF not found: {pdf_path}")
    pymupdf = _open_pymupdf()
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise JinaOcrInputError(f"cannot open PDF {pdf_path!r}: {exc}") from exc
    try:
        indices = _page_indices(document, pages)
        if not indices:
            raise JinaOcrInputError(f"PDF has no pages: {pdf_path}")
        return [render_page(document, index, output_dir, opts) for index in indices]
    finally:
        document.close()


def render_pdf_page(
    pdf_path: str,
    page_index: int,
    output_dir: str | os.PathLike[str],
    options: Optional[JinaOcrOptions] = None,
) -> RenderedJinaPage:
    return render_pdf_pages(pdf_path, output_dir, [page_index], options)[0]


def make_cache_key(
    image_sha256: Any,
    options: Optional[JinaOcrOptions] = None,
    render_policy: Optional[Mapping[str, Any]] = None,
    page_no: Optional[int] = None,
) -> str:
    opts = options or JinaOcrOptions()
    if isinstance(image_sha256, RenderedJinaPage):
        if page_no is None:
            page_no = image_sha256.page_no
        image_sha256 = image_sha256.image_sha256
    if page_no is None:
        page_no = 0
    try:
        page_value = int(page_no)
    except (TypeError, ValueError) as exc:
        raise ValueError("cache page number must be an integer") from exc
    if page_value < 0:
        raise ValueError("cache page number must be non-negative")
    if isinstance(image_sha256, bytes):
        image_digest = hashlib.sha256(image_sha256).hexdigest()
    elif isinstance(image_sha256, (str, os.PathLike)) and len(str(image_sha256)) != 64:
        candidate = Path(image_sha256)
        if candidate.is_file():
            image_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        else:
            image_digest = str(image_sha256)
    else:
        image_digest = str(image_sha256)
    payload = {
        "model": opts.model,
        "revision": opts.revision,
        "prompt": opts.prompt,
        "max_new_tokens": opts.max_new_tokens,
        "device": opts.device,
        "offline": opts.offline,
        "image_sha256": image_digest,
        "render_policy": dict(render_policy or {}),
        "page_no": page_value,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_file(cache_dir: str, key: str) -> Path:
    return Path(cache_dir) / f"jina-{key}.json"


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _worker_path() -> Path:
    source = Path(__file__).resolve().parents[2] / "kernel" / "jina_ocr_worker.py"
    if source.is_file():
        return source
    bundled = getattr(sys, "_MEIPASS", "")
    if bundled:
        candidate = Path(bundled) / "pdf2zh" / "kernel" / "jina_ocr_worker.py"
        if candidate.is_file():
            return candidate
    return source


def _validate_result(data: Any) -> dict[str, Any]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise JinaOcrSchemaError(f"invalid Jina OCR result JSON: {exc}") from exc
    try:
        from pdf2zh.kernel.jina_ocr_worker import validate_result
    except Exception as exc:
        raise JinaOcrBackendUnavailable(
            f"Jina OCR worker is unavailable: {exc}"
        ) from exc
    try:
        return validate_result(data)
    except Exception as exc:
        raise JinaOcrSchemaError(f"invalid Jina OCR result: {exc}") from exc


def _device_matches(requested: str, actual: str) -> bool:
    requested_value = str(requested or "auto").lower()
    actual_value = str(actual or "auto").lower()
    if requested_value == "auto":
        return actual_value in {"auto", "cpu", "cuda", "cuda:0"}
    if requested_value == "cuda":
        return actual_value in {"cuda", "cuda:0"}
    return actual_value == requested_value


def _validate_result_binding(
    data: Any,
    options: JinaOcrOptions,
    *,
    expected_pages: Optional[set[int]] = None,
    rendered_by_page: Optional[Mapping[int, RenderedJinaPage]] = None,
) -> dict[str, Any]:
    checked = _validate_result(data)
    for key, expected in (
        ("model", options.model),
        ("revision", options.revision),
        ("prompt", options.prompt),
        ("offline", options.offline),
        ("max_new_tokens", options.max_new_tokens),
    ):
        if checked[key] != expected:
            raise JinaOcrSchemaError(f"Jina OCR result {key} does not match request")
    if not _device_matches(options.device, checked["device"]):
        raise JinaOcrSchemaError("Jina OCR result device does not match request")
    actual_pages = {int(item["page"]) for item in checked["pages"]}
    if expected_pages is not None and actual_pages != expected_pages:
        missing = sorted(expected_pages - actual_pages)
        extra = sorted(actual_pages - expected_pages)
        raise JinaOcrSchemaError(
            f"Jina OCR result page set mismatch; missing={missing}, extra={extra}"
        )
    if rendered_by_page is not None:
        for item in checked["pages"]:
            page_no = int(item["page"])
            rendered = rendered_by_page.get(page_no)
            if rendered is None:
                raise JinaOcrSchemaError(
                    f"Jina OCR result contains unexpected page {page_no}"
                )
            if item["image_sha256"] != rendered.image_sha256:
                raise JinaOcrSchemaError(
                    f"Jina OCR result page {page_no} image digest mismatch"
                )
            if item["render_policy"] != rendered.policy:
                raise JinaOcrSchemaError(
                    f"Jina OCR result page {page_no} render policy mismatch"
                )
    return checked


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise JinaOcrSchemaError(
            f"cannot read Jina OCR result {str(path)!r}: {exc}"
        ) from exc
    return _validate_result(data)


def validate_result(data: Any) -> dict[str, Any]:
    return _validate_result(data)


def _classify_worker_failure(
    stderr: str, returncode: int, options: JinaOcrOptions
) -> IngestionError:
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace").strip()
    else:
        text = str(stderr or "").strip()
    lower = text.lower()
    if "timeout" in lower:
        return JinaOcrTimeoutError(f"Jina OCR worker timed out: {text[-2000:]}")
    if "device_error" in lower or "cuda" in lower and "unavailable" in lower:
        return JinaOcrDeviceError(f"Jina OCR device error: {text[-2000:]}")
    if "offline_error" in lower or (
        options.offline
        and any(
            token in lower
            for token in ("connection", "local entry", "not found", "offline")
        )
    ):
        return JinaOcrOfflineError(f"Jina OCR offline error: {text[-2000:]}")
    if (
        "model_error" in lower
        or "model" in lower
        and any(
            token in lower
            for token in ("not found", "missing", "download", "repository")
        )
    ):
        return JinaOcrModelUnavailable(f"Jina OCR model unavailable: {text[-2000:]}")
    if "dependency_error" in lower or "modulenotfounderror" in lower:
        return JinaOcrBackendUnavailable(
            f"Jina OCR dependency unavailable: {text[-2000:]}"
        )
    if "schema_error" in lower or "generation_truncated" in lower or returncode == 2:
        return JinaOcrSchemaError(f"Jina OCR worker schema error: {text[-2000:]}")
    return JinaOcrWorkerError(
        f"Jina OCR worker failed with exit {returncode}: {text[-2000:] or '(no stderr)'}"
    )


class JinaOcrBackend:
    name: str = BACKEND_JINA

    def __init__(
        self,
        options: Optional[JinaOcrOptions | Mapping[str, Any]] = None,
        base_pages: Any = None,
    ) -> None:
        if isinstance(options, JinaOcrOptions):
            self.options = options
        elif options is None:
            self.options = JinaOcrOptions.from_env()
        else:
            self.options = JinaOcrOptions.from_mapping(options)
        self.base_pages = base_pages

    def _isolated_python(self) -> str:
        try:
            from pdf2zh.kernel.jina_ocr_env import jina_python_override
        except Exception as exc:
            raise JinaOcrBackendUnavailable(
                f"Jina OCR environment probe failed: {exc}"
            ) from exc
        value = jina_python_override()
        if value and os.path.isdir(value):
            try:
                from pdf2zh.kernel.jina_ocr_env import venv_python

                value = venv_python(Path(value))
            except Exception:
                value = ""
        if not value or not os.path.isfile(value):
            raise JinaOcrBackendUnavailable(
                "Jina OCR isolated environment is missing; run pdf2zh-setup-jina"
            )
        return value

    def _python(self) -> str:
        return self._isolated_python()

    def _render_pages(
        self,
        pdf_path: str,
        output_dir: str | os.PathLike[str],
        pages: Any = None,
        options: Optional[JinaOcrOptions] = None,
    ) -> list[RenderedJinaPage]:
        return render_pdf_pages(pdf_path, output_dir, pages, options or self.options)

    def render(
        self,
        pdf_path: str,
        output_dir: str | os.PathLike[str],
        pages: Any = None,
        options: Optional[JinaOcrOptions] = None,
    ) -> list[RenderedJinaPage]:
        return self._render_pages(pdf_path, output_dir, pages, options)

    def _cache_key(
        self,
        image_sha256: Any,
        options: Optional[JinaOcrOptions] = None,
        render_policy: Optional[Mapping[str, Any]] = None,
        page_no: Optional[int] = None,
    ) -> str:
        return make_cache_key(
            image_sha256,
            options or self.options,
            render_policy,
            page_no=page_no,
        )

    def _ingest_subprocess(
        self,
        python_exe: str,
        manifest_path: str | os.PathLike[str],
        result_path: str | os.PathLike[str],
        options: Optional[JinaOcrOptions] = None,
    ) -> dict[str, Any]:
        return self._run_worker(
            python_exe,
            Path(manifest_path),
            Path(result_path),
            options or self.options,
        )

    def _run_worker(
        self,
        python_exe: str,
        manifest_path: Path,
        result_path: Path,
        options: JinaOcrOptions,
    ) -> dict[str, Any]:
        worker = _worker_path()
        if not worker.is_file():
            raise JinaOcrBackendUnavailable(f"Jina OCR worker is missing: {worker}")
        command = [python_exe, str(worker), str(manifest_path), str(result_path)]
        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        if options.offline:
            child_env["HF_HUB_OFFLINE"] = "1"
            child_env["TRANSFORMERS_OFFLINE"] = "1"
        else:
            child_env.pop("HF_HUB_OFFLINE", None)
            child_env.pop("TRANSFORMERS_OFFLINE", None)
        started = time.monotonic()
        acquired = _INFERENCE_LOCK.acquire(timeout=options.timeout)
        if not acquired:
            raise JinaOcrTimeoutError(
                f"Jina OCR worker timed out after {options.timeout:g}s waiting for inference capacity"
            )
        try:
            try:
                remaining = max(0.001, options.timeout - (time.monotonic() - started))
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=remaining,
                    env=child_env,
                )
            finally:
                _INFERENCE_LOCK.release()
        except subprocess.TimeoutExpired as exc:
            raise JinaOcrTimeoutError(
                f"Jina OCR worker timed out after {options.timeout:g}s"
            ) from exc
        except OSError as exc:
            raise JinaOcrBackendUnavailable(
                f"Jina OCR interpreter {python_exe!r} failed to start: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise _classify_worker_failure(
                getattr(completed, "stderr", ""), int(completed.returncode), options
            )
        if not result_path.is_file():
            raise JinaOcrSchemaError("Jina OCR worker produced no result file")
        return _load_json(result_path)

    def _collect_result(
        self,
        rendered: Sequence[RenderedJinaPage],
        options: JinaOcrOptions,
        work_dir: Path,
    ) -> dict[str, Any]:
        if not rendered:
            raise JinaOcrSchemaError("Jina OCR has no rendered pages")
        rendered_by_page = {page.page_no: page for page in rendered}
        if len(rendered_by_page) != len(rendered):
            raise JinaOcrSchemaError("rendered Jina OCR pages must be unique")
        expected_pages = set(rendered_by_page)
        cached_pages: dict[int, dict[str, Any]] = {}
        missing: list[RenderedJinaPage] = []
        for page in rendered:
            if not options.cache_dir:
                missing.append(page)
                continue
            key = make_cache_key(
                page.image_sha256,
                options,
                page.policy,
                page_no=page.page_no,
            )
            path = _cache_file(options.cache_dir, key)
            if not path.is_file():
                missing.append(page)
                continue
            try:
                cached = _validate_result_binding(
                    _load_json(path),
                    options,
                    expected_pages={page.page_no},
                    rendered_by_page={page.page_no: page},
                )
                candidates = [
                    item for item in cached["pages"] if item["page"] == page.page_no
                ]
                if len(candidates) != 1:
                    continue
                cached_pages[page.page_no] = candidates[0]
            except JinaOcrSchemaError:
                continue
        worker_pages: list[dict[str, Any]] = []
        missing_by_page = {page.page_no: page for page in missing}
        if missing:
            python_exe = self._python()
            manifest = {
                "schema": "pdf2zh.jina-ocr.manifest",
                "version": 1,
                "model": options.model,
                "revision": options.revision,
                "prompt": options.prompt,
                "options": {
                    "max_new_tokens": options.max_new_tokens,
                    "device": options.device,
                    "offline": options.offline,
                },
                "pages": [
                    {
                        "page": item.page_no,
                        "image": item.path,
                        "image_sha256": item.image_sha256,
                        "render_policy": item.policy,
                    }
                    for item in missing
                ],
            }
            try:
                from pdf2zh.kernel.jina_ocr_worker import validate_manifest

                manifest = validate_manifest(manifest)
            except Exception as exc:
                raise JinaOcrSchemaError(f"invalid Jina OCR manifest: {exc}") from exc
            manifest_path = work_dir / "manifest.json"
            result_path = work_dir / "result.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = self._ingest_subprocess(
                python_exe, manifest_path, result_path, options
            )
            checked_result = _validate_result_binding(
                result,
                options,
                expected_pages=set(missing_by_page),
                rendered_by_page=missing_by_page,
            )
            worker_pages = list(checked_result["pages"])
        pages_by_number = dict(cached_pages)
        pages_by_number.update({int(item["page"]): item for item in worker_pages})
        actual_pages = set(pages_by_number)
        if actual_pages != expected_pages:
            missing_numbers = sorted(expected_pages - actual_pages)
            extra = sorted(actual_pages - expected_pages)
            raise JinaOcrSchemaError(
                f"Jina OCR result page set mismatch; missing={missing_numbers}, extra={extra}"
            )
        ordered = [pages_by_number[page.page_no] for page in rendered]
        combined = {
            "schema": "pdf2zh.jina-ocr.result",
            "version": 1,
            "backend": BACKEND_JINA,
            "model": options.model,
            "revision": options.revision,
            "prompt": options.prompt,
            "device": options.device,
            "offline": options.offline,
            "max_new_tokens": options.max_new_tokens,
            "pages": ordered,
        }
        _validate_result_binding(
            combined,
            options,
            expected_pages=expected_pages,
            rendered_by_page=rendered_by_page,
        )
        if options.cache_dir:
            for item in worker_pages:
                source = missing_by_page.get(int(item["page"]))
                if source is None:
                    raise JinaOcrSchemaError(
                        f"Jina OCR result contains uncached page {item['page']}"
                    )
                key = make_cache_key(
                    source.image_sha256,
                    options,
                    source.policy,
                    page_no=source.page_no,
                )
                entry = {
                    "schema": combined["schema"],
                    "version": combined["version"],
                    "backend": combined["backend"],
                    "model": combined["model"],
                    "revision": combined["revision"],
                    "prompt": combined["prompt"],
                    "device": combined["device"],
                    "offline": combined["offline"],
                    "max_new_tokens": combined["max_new_tokens"],
                    "pages": [item],
                }
                _validate_result_binding(
                    entry,
                    options,
                    expected_pages={source.page_no},
                    rendered_by_page={source.page_no: source},
                )
                try:
                    _atomic_json(_cache_file(options.cache_dir, key), entry)
                except OSError:
                    pass
        return combined

    def ingest(
        self,
        pdf_path: str,
        trace: Optional[Any] = None,
        *,
        pages: Any = None,
        base_pages: Any = None,
        options: Optional[JinaOcrOptions | Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        pdf_path = os.fspath(pdf_path)
        opts = self.options if options is None else options
        if not isinstance(opts, JinaOcrOptions):
            opts = JinaOcrOptions.from_mapping(opts)
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError(f"unknown JinaOcrBackend.ingest fields: {unknown}")
        selected_pages = pages if pages is not None else opts.pages
        source_pages = base_pages if base_pages is not None else self.base_pages
        if not os.path.isfile(pdf_path):
            raise JinaOcrInputError(f"PDF not found: {pdf_path}")
        work_dir = Path(tempfile.mkdtemp(prefix="pdf2zh_jina_ocr_"))
        try:
            if not opts.cache_dir:
                self._python()
            rendered = render_pdf_pages(pdf_path, work_dir, selected_pages, opts)
            result = self._collect_result(rendered, opts, work_dir)
            doc = jina_result_to_document(
                result,
                source_pages,
                opts,
                title=pdf_path,
                expected_page_nos={page.page_no for page in rendered},
            )
            emit_ingest_events(doc, trace, pdf_path=pdf_path)
            return doc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def ingest_result(
        self,
        result: Any,
        base_pages: Any,
        *,
        options: Optional[JinaOcrOptions | Mapping[str, Any]] = None,
        title: str = "",
    ) -> Any:
        opts = self.options if options is None else options
        if not isinstance(opts, JinaOcrOptions):
            opts = JinaOcrOptions.from_mapping(opts)
        checked = _validate_result_binding(result, opts)
        return jina_result_to_document(checked, base_pages, opts, title=title)

    def ingest_result_file(
        self,
        result_path: str | os.PathLike[str],
        base_pages: Any,
        *,
        options: Optional[JinaOcrOptions | Mapping[str, Any]] = None,
        title: str = "",
    ) -> Any:
        return self.ingest_result(
            _load_json(Path(result_path)), base_pages, options=options, title=title
        )

    @staticmethod
    def _load_worker_result(result_path: str | os.PathLike[str]) -> dict[str, Any]:
        return _load_json(Path(result_path))


__all__ = [
    "JinaOcrInputError",
    "JinaOcrBackendUnavailable",
    "JinaOcrModelUnavailable",
    "JinaOcrTimeoutError",
    "JinaOcrSchemaError",
    "JinaOcrOfflineError",
    "JinaOcrDeviceError",
    "JinaOcrWorkerError",
    "JinaOcrError",
    "JinaOcrCoverageError",
    "validate_result",
    "RenderedJinaPage",
    "render_page",
    "render_pdf_pages",
    "render_pdf_page",
    "make_cache_key",
    "sha256_file",
    "JinaOcrBackend",
    "jina_result_to_document",
]
