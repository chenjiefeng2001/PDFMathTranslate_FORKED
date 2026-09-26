from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

JINA_MODEL_ID = "jinaai/jina-ocr-v1"
JINA_REVISION = "904f815deed0fbeab02d82d4648b70106272ffe3"
JINA_PROMPT = "Transcribe the provided document image into a clean Markdown format, preserving the natural reading order."
JINA_DEFAULT_MAX_NEW_TOKENS = 4096
JINA_MIN_OUTPUT_TOKENS = 1
JINA_MAX_OUTPUT_TOKENS = 32_768
JINA_MAX_PAGES = 10_000
WORKER_MANIFEST_SCHEMA = "pdf2zh.jina-ocr.manifest"
WORKER_RESULT_SCHEMA = "pdf2zh.jina-ocr.result"
WORKER_SCHEMA_VERSION = 1

_MANIFEST_REQUIRED = {
    "schema",
    "version",
    "model",
    "revision",
    "prompt",
    "options",
    "pages",
}
_MANIFEST_ALLOWED = _MANIFEST_REQUIRED | {"metadata"}
_RESULT_REQUIRED = {
    "schema",
    "version",
    "backend",
    "model",
    "revision",
    "prompt",
    "device",
    "offline",
    "max_new_tokens",
    "pages",
}
_RESULT_ALLOWED = _RESULT_REQUIRED | {"metadata"}
_PAGE_REQUIRED = {"page", "image", "image_sha256", "render_policy"}
_PAGE_ALLOWED = _PAGE_REQUIRED
_OPTION_ALLOWED = {"max_new_tokens", "device", "offline"}
_RESULT_PAGE_REQUIRED = {"page", "markdown", "image_sha256", "render_policy"}
_RESULT_PAGE_ALLOWED = _RESULT_PAGE_REQUIRED


class ManifestValidationError(ValueError):
    pass


class ResultValidationError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _is_regular_file(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        path = Path(value).expanduser().resolve()
        return path.is_file()
    except (OSError, ValueError):
        return False


def _resolved_model(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return ""


def _is_device(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value in {"auto", "cpu", "cuda"} or bool(re.fullmatch(r"cuda:\d+", value))


def _device_matches(requested: Any, actual: Any) -> bool:
    requested_value = str(requested or "auto").lower()
    actual_value = str(actual or "auto").lower()
    if requested_value == "auto":
        return actual_value in {"auto", "cpu", "cuda", "cuda:0"}
    if requested_value == "cuda":
        return actual_value in {"cuda", "cuda:0"}
    return actual_value == requested_value


def _check_keys(
    value: Mapping[str, Any], required: set[str], allowed: set[str], label: str
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - allowed)
    if missing:
        raise ValueError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} unknown fields: {', '.join(unknown)}")


def validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest must be a JSON object")
    _check_keys(data, _MANIFEST_REQUIRED, _MANIFEST_ALLOWED, "manifest")
    if data["schema"] != WORKER_MANIFEST_SCHEMA:
        raise ManifestValidationError("manifest schema mismatch")
    if not _is_int(data["version"]) or data["version"] != WORKER_SCHEMA_VERSION:
        raise ManifestValidationError("manifest version mismatch")
    for key in ("model", "prompt"):
        if not isinstance(data[key], str) or not data[key]:
            raise ManifestValidationError(f"manifest {key} must be a non-empty string")
    model_path = Path(data["model"]).expanduser()
    local_model = model_path.exists()
    if local_model:
        data = dict(data)
        data["model"] = _resolved_model(data["model"])
    if not isinstance(data["revision"], str):
        raise ManifestValidationError("manifest revision must be a string")
    if not local_model and (
        not data["revision"]
        or (data["model"] != JINA_MODEL_ID and data["revision"] == JINA_REVISION)
    ):
        raise ManifestValidationError(
            "manifest revision must be explicit for remote models"
        )
    options = data["options"]
    if not isinstance(options, dict):
        raise ManifestValidationError("manifest options must be an object")
    if set(options) != _OPTION_ALLOWED:
        missing = sorted(_OPTION_ALLOWED - set(options))
        unknown = sorted(set(options) - _OPTION_ALLOWED)
        detail = []
        if missing:
            detail.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown fields: {', '.join(unknown)}")
        raise ManifestValidationError("manifest options " + ", ".join(detail))
    if not _is_int(options["max_new_tokens"]):
        raise ManifestValidationError("manifest max_new_tokens must be an integer")
    if (
        not JINA_MIN_OUTPUT_TOKENS
        <= options["max_new_tokens"]
        <= JINA_MAX_OUTPUT_TOKENS
    ):
        raise ManifestValidationError(
            f"manifest max_new_tokens must be between {JINA_MIN_OUTPUT_TOKENS} "
            f"and {JINA_MAX_OUTPUT_TOKENS}"
        )
    if not _is_device(options["device"]):
        raise ManifestValidationError("manifest device is invalid")
    if not isinstance(options["offline"], bool):
        raise ManifestValidationError("manifest offline must be boolean")
    if "metadata" in data and not isinstance(data["metadata"], dict):
        raise ManifestValidationError("manifest metadata must be an object")
    pages = data["pages"]
    if not isinstance(pages, list) or not pages:
        raise ManifestValidationError("manifest pages must be a non-empty array")
    if len(pages) > JINA_MAX_PAGES:
        raise ManifestValidationError(
            f"manifest pages cannot exceed {JINA_MAX_PAGES} entries"
        )
    normalized_pages: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ManifestValidationError("manifest page must be an object")
        _check_keys(page, _PAGE_REQUIRED, _PAGE_ALLOWED, "manifest page")
        if not _is_int(page["page"]) or page["page"] < 0:
            raise ManifestValidationError(
                "manifest page must be a non-negative integer"
            )
        if page["page"] in seen:
            raise ManifestValidationError("manifest pages must be unique")
        seen.add(page["page"])
        if not _is_regular_file(page["image"]):
            raise ManifestValidationError("manifest image must be a regular file")
        if not _is_sha256(page["image_sha256"]):
            raise ManifestValidationError("manifest image_sha256 is invalid")
        if not isinstance(page["render_policy"], dict):
            raise ManifestValidationError("manifest render_policy must be an object")
        normalized_pages.append(
            {
                **page,
                "image": str(Path(page["image"]).expanduser().resolve()),
            }
        )
    data = dict(data)
    data["pages"] = normalized_pages
    return data


def validate_result(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ResultValidationError("result must be a JSON object")
    _check_keys(data, _RESULT_REQUIRED, _RESULT_ALLOWED, "result")
    if data["schema"] != WORKER_RESULT_SCHEMA:
        raise ResultValidationError("result schema mismatch")
    if not _is_int(data["version"]) or data["version"] != WORKER_SCHEMA_VERSION:
        raise ResultValidationError("result version mismatch")
    if data["backend"] != "jina":
        raise ResultValidationError("result backend must be jina")
    for key in ("model", "prompt"):
        if not isinstance(data[key], str) or not data[key]:
            raise ResultValidationError(f"result {key} must be a non-empty string")
    model_path = Path(data["model"]).expanduser()
    local_model = model_path.exists()
    if local_model:
        data = dict(data)
        data["model"] = _resolved_model(data["model"])
    if not isinstance(data["revision"], str):
        raise ResultValidationError("result revision must be a string")
    if not local_model and (
        not data["revision"]
        or (data["model"] != JINA_MODEL_ID and data["revision"] == JINA_REVISION)
    ):
        raise ResultValidationError(
            "result revision must be explicit for remote models"
        )
    if not _is_device(data["device"]):
        raise ResultValidationError("result device is invalid")
    if not isinstance(data["offline"], bool):
        raise ResultValidationError("result offline must be boolean")
    if not _is_int(data["max_new_tokens"]):
        raise ResultValidationError("result max_new_tokens must be an integer")
    if not JINA_MIN_OUTPUT_TOKENS <= data["max_new_tokens"] <= JINA_MAX_OUTPUT_TOKENS:
        raise ResultValidationError(
            f"result max_new_tokens must be between {JINA_MIN_OUTPUT_TOKENS} "
            f"and {JINA_MAX_OUTPUT_TOKENS}"
        )
    if "metadata" in data and not isinstance(data["metadata"], dict):
        raise ResultValidationError("result metadata must be an object")
    pages = data["pages"]
    if not isinstance(pages, list) or not pages:
        raise ResultValidationError("result pages must be a non-empty array")
    if len(pages) > JINA_MAX_PAGES:
        raise ResultValidationError(
            f"result pages cannot exceed {JINA_MAX_PAGES} entries"
        )
    seen: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ResultValidationError("result page must be an object")
        _check_keys(page, _RESULT_PAGE_REQUIRED, _RESULT_PAGE_ALLOWED, "result page")
        if not _is_int(page["page"]) or page["page"] < 0:
            raise ResultValidationError("result page must be a non-negative integer")
        if page["page"] in seen:
            raise ResultValidationError("result pages must be unique")
        seen.add(page["page"])
        if not isinstance(page["markdown"], str):
            raise ResultValidationError("result markdown must be a string")
        if not _is_sha256(page["image_sha256"]):
            raise ResultValidationError("result image_sha256 is invalid")
        if not isinstance(page["render_policy"], dict):
            raise ResultValidationError("result render_policy must be an object")
    return data


def validate_result_against_manifest(result: Any, manifest: Any) -> dict[str, Any]:
    checked_result = validate_result(result)
    checked_manifest = validate_manifest(manifest)
    for key in ("model", "revision", "prompt"):
        if checked_result[key] != checked_manifest[key]:
            raise ResultValidationError(f"result {key} does not match manifest")
    if not _device_matches(
        checked_manifest["options"]["device"], checked_result["device"]
    ):
        raise ResultValidationError("result device does not match manifest")
    for key in ("offline", "max_new_tokens"):
        if checked_result[key] != checked_manifest["options"][key]:
            raise ResultValidationError(f"result {key} does not match manifest")
    result_pages = {item["page"]: item for item in checked_result["pages"]}
    manifest_pages = {item["page"]: item for item in checked_manifest["pages"]}
    if set(result_pages) != set(manifest_pages):
        raise ResultValidationError("result page set does not match manifest")
    for page_no, manifest_page in manifest_pages.items():
        result_page = result_pages[page_no]
        if result_page["image_sha256"] != manifest_page["image_sha256"]:
            raise ResultValidationError(
                f"result page {page_no} image_sha256 does not match manifest"
            )
        if result_page["render_policy"] != manifest_page["render_policy"]:
            raise ResultValidationError(
                f"result page {page_no} render_policy does not match manifest"
            )
    return checked_result


def make_manifest(
    pages: list[dict[str, Any]],
    *,
    model: str = JINA_MODEL_ID,
    revision: str = JINA_REVISION,
    prompt: str = JINA_PROMPT,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_options = {
        "max_new_tokens": JINA_DEFAULT_MAX_NEW_TOKENS,
        "device": "auto",
        "offline": False,
    }
    manifest_options.update(dict(options or {}))
    data = {
        "schema": WORKER_MANIFEST_SCHEMA,
        "version": WORKER_SCHEMA_VERSION,
        "model": model,
        "revision": revision,
        "prompt": prompt,
        "options": manifest_options,
        "pages": pages,
    }
    return validate_manifest(data)


def make_result(
    pages: list[dict[str, Any]],
    *,
    model: str = JINA_MODEL_ID,
    revision: str = JINA_REVISION,
    prompt: str = JINA_PROMPT,
    device: str = "auto",
    offline: bool = False,
    max_new_tokens: int = JINA_DEFAULT_MAX_NEW_TOKENS,
) -> dict[str, Any]:
    data = {
        "schema": WORKER_RESULT_SCHEMA,
        "version": WORKER_SCHEMA_VERSION,
        "backend": "jina",
        "model": model,
        "revision": revision,
        "prompt": prompt,
        "device": device,
        "offline": offline,
        "max_new_tokens": max_new_tokens,
        "pages": pages,
    }
    return validate_result(data)


def read_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read JSON {os.fspath(path)!r}: {exc}") from exc


def write_json_atomic(path: str | os.PathLike[str], data: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _force_utf8_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _device_error(message: str) -> None:
    print(f"jina_ocr_worker: device_error: {message}", file=sys.stderr)


def _load_model(manifest: Mapping[str, Any], device: str):
    try:
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        AutoModelForCausalLM = transformers.AutoModelForCausalLM
        AutoProcessor = transformers.AutoProcessor
    except Exception as exc:
        raise RuntimeError(f"dependency_error: {exc}") from exc
    requested = str(device or "auto").lower()
    try:
        cuda_available = bool(torch.cuda.is_available())
    except Exception as exc:
        _device_error(f"CUDA probe failed: {exc}")
        raise RuntimeError("device_error: CUDA probe failed") from exc
    if requested == "auto":
        selected = "cuda:0" if cuda_available else "cpu"
    elif requested == "cuda":
        requested = "cuda:0"
    if requested.startswith("cuda") and not cuda_available:
        _device_error("CUDA requested but torch.cuda.is_available() is false")
        raise RuntimeError("device_error: CUDA is unavailable")
    if requested.startswith("cuda:"):
        try:
            device_index = int(requested.split(":", 1)[1])
            device_count_fn = getattr(torch.cuda, "device_count", None)
            device_count = int(device_count_fn()) if callable(device_count_fn) else 1
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"device_error: cannot inspect CUDA devices: {exc}"
            ) from exc
        if device_index < 0 or device_index >= device_count:
            _device_error(
                f"CUDA device index {device_index} is unavailable (count={device_count})"
            )
            raise RuntimeError("device_error: requested CUDA device is unavailable")
    if requested == "cpu":
        selected = "cpu"
    elif requested.startswith("cuda"):
        selected = requested
    else:
        raise RuntimeError(f"device_error: invalid Jina OCR device {device!r}")
    torch_device = torch.device(selected)
    kwargs = {"revision": manifest["revision"]} if manifest["revision"] else {}
    if manifest["options"].get("offline"):
        kwargs["local_files_only"] = True
    try:
        processor = AutoProcessor.from_pretrained(
            manifest["model"],
            trust_remote_code=True,
            **kwargs,
        )
        dtype = torch.bfloat16
        model = AutoModelForCausalLM.from_pretrained(
            manifest["model"],
            dtype=dtype,
            trust_remote_code=True,
            **kwargs,
        ).to(torch_device)
    except Exception as exc:
        text = str(exc).lower()
        if manifest["options"].get("offline") and (
            "offline" in text
            or "local entry" in text
            or "connection" in text
            or "not found" in text
        ):
            raise RuntimeError(f"offline_error: {exc}") from exc
        raise RuntimeError(f"model_error: {exc}") from exc
    model.eval()
    return processor, model, torch, torch_device, selected


def _decode_output(
    processor: Any, output: Any, inputs: Mapping[str, Any], torch: Any
) -> str:
    decoder = getattr(processor, "decode_ocr", None)
    if callable(decoder):
        value = decoder(output, inputs["input_ids"])
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is not None:
            return str(value)
    generated = output
    if hasattr(generated, "sequences"):
        generated = generated.sequences
    input_ids = inputs.get("input_ids")
    if hasattr(generated, "shape") and input_ids is not None:
        start = int(input_ids.shape[-1])
        generated = generated[:, start:]
    if hasattr(processor, "batch_decode"):
        values = processor.batch_decode(generated, skip_special_tokens=True)
        if isinstance(values, (list, tuple)):
            return str(values[0] if values else "")
        return str(values)
    if hasattr(processor, "decode"):
        return str(processor.decode(generated, skip_special_tokens=True))
    return str(generated)


def _sequence_values(output: Any) -> list[Any]:
    value = getattr(output, "sequences", output)
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:
            return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _generation_truncated(
    output: Any, inputs: Mapping[str, Any], processor: Any, model: Any, limit: int
) -> bool:
    for name in ("finished", "completed", "complete", "done"):
        if hasattr(output, name):
            value = getattr(output, name)
            if isinstance(value, bool):
                if value:
                    return False
                break
    sequences = _sequence_values(output)
    if not sequences:
        return False
    input_ids = inputs.get("input_ids")
    if input_ids is None:
        input_length = 0
    elif hasattr(input_ids, "shape"):
        input_length = int(input_ids.shape[-1])
    else:
        try:
            input_length = len(input_ids)
        except TypeError:
            input_length = 0
    generated_length = 0
    for sequence in sequences:
        if not isinstance(sequence, (list, tuple)):
            continue
        generated_length = max(generated_length, len(sequence) - input_length)
    if generated_length < limit:
        return False
    eos_ids: set[int] = set()
    for owner in (output, getattr(model, "generation_config", None), processor):
        value = getattr(owner, "eos_token_id", None)
        if value is None:
            value = getattr(getattr(owner, "tokenizer", None), "eos_token_id", None)
        if isinstance(value, int):
            eos_ids.add(value)
        elif isinstance(value, (list, tuple, set)):
            eos_ids.update(int(item) for item in value if isinstance(item, int))
    if eos_ids:
        for sequence in sequences:
            if isinstance(sequence, (list, tuple)) and any(
                int(item) in eos_ids
                for item in sequence[input_length:]
                if isinstance(item, int)
            ):
                return False
    return True


def run_manifest(
    manifest: Mapping[str, Any], result_path: str | os.PathLike[str]
) -> None:
    data = validate_manifest(manifest)
    offline = bool(data["options"].get("offline", False))
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
    device = str(data["options"]["device"])
    processor, model, torch, torch_device, effective_device = _load_model(data, device)
    pages: list[dict[str, Any]] = []
    try:
        Image = importlib.import_module("PIL.Image")
    except Exception as exc:
        raise RuntimeError(f"dependency_error: Pillow unavailable: {exc}") from exc
    for item in data["pages"]:
        try:
            with open(item["image"], "rb") as image_file:
                digest = hashlib.sha256()
                for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    digest.update(chunk)
                image_digest = digest.hexdigest()
            if image_digest != item["image_sha256"]:
                raise RuntimeError(f"page {item['page']} image checksum mismatch")
            with Image.open(item["image"]) as source:
                image = source.convert("RGB")
            prepare = getattr(processor, "prepare_ocr_inputs", None)
            if callable(prepare):
                try:
                    inputs = prepare(image, prompt=data["prompt"], device=torch_device)
                except TypeError:
                    inputs = prepare(image, device=torch_device)
            else:
                inputs = processor(
                    images=image, text=data["prompt"], return_tensors="pt"
                )
            if hasattr(inputs, "to"):
                moved_inputs = inputs.to(torch_device)
                if moved_inputs is not None:
                    inputs = moved_inputs
            max_new_tokens = int(data["options"]["max_new_tokens"])
            inference_context = getattr(torch, "inference_mode", None)
            if callable(inference_context):
                with inference_context():
                    output = model.generate(
                        **inputs,
                        max_new_tokens=max_new_tokens,
                        do_sample=False,
                    )
            else:
                output = model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                )
            if _generation_truncated(output, inputs, processor, model, max_new_tokens):
                raise RuntimeError(
                    f"generation_truncated: page {item['page']} reached "
                    f"max_new_tokens={max_new_tokens} without a completion signal"
                )
            markdown = _decode_output(processor, output, inputs, torch)
            if not isinstance(markdown, str):
                markdown = str(markdown)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"page_error: {exc}") from exc
        page = {
            "page": int(item["page"]),
            "markdown": markdown,
            "image_sha256": item["image_sha256"],
            "render_policy": dict(item["render_policy"]),
        }
        pages.append(page)
    result = make_result(
        pages,
        model=data["model"],
        revision=data["revision"],
        prompt=data["prompt"],
        device=effective_device,
        offline=offline,
        max_new_tokens=int(data["options"]["max_new_tokens"]),
    )
    result = validate_result_against_manifest(result, data)
    write_json_atomic(result_path, result)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jina_ocr_worker.py")
    parser.add_argument("manifest_pos", nargs="?")
    parser.add_argument("result_pos", nargs="?")
    parser.add_argument("--manifest", "--input", dest="manifest_opt")
    parser.add_argument("--result", "--output", dest="result_opt")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    _force_utf8_stdio()
    args = _parse_args(argv)
    manifest_path = args.manifest_opt or args.manifest_pos
    result_path = args.result_opt or args.result_pos
    if not manifest_path or not result_path:
        print(
            "usage: jina_ocr_worker.py <manifest.json> <result.json>",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = read_json(manifest_path)
        validate_manifest(manifest)
        run_manifest(manifest, result_path)
    except (ManifestValidationError, ResultValidationError, ValueError) as exc:
        print(f"jina_ocr_worker: schema_error: {exc}", file=sys.stderr)
        return 2
    except TimeoutError as exc:
        print(f"jina_ocr_worker: timeout_error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        text = str(exc)
        if text.startswith("device_error:"):
            print(f"jina_ocr_worker: {text}", file=sys.stderr)
        elif text.startswith("offline_error:"):
            print(f"jina_ocr_worker: {text}", file=sys.stderr)
        elif text.startswith("model_error:"):
            print(f"jina_ocr_worker: {text}", file=sys.stderr)
        elif text.startswith("dependency_error:"):
            print(f"jina_ocr_worker: {text}", file=sys.stderr)
        else:
            print(f"jina_ocr_worker: {text}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "JINA_MODEL_ID",
    "JINA_REVISION",
    "JINA_PROMPT",
    "JINA_DEFAULT_MAX_NEW_TOKENS",
    "WORKER_MANIFEST_SCHEMA",
    "WORKER_RESULT_SCHEMA",
    "WORKER_SCHEMA_VERSION",
    "ManifestValidationError",
    "ResultValidationError",
    "validate_manifest",
    "validate_result",
    "validate_result_against_manifest",
    "make_manifest",
    "make_result",
    "read_json",
    "write_json_atomic",
    "main",
]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
