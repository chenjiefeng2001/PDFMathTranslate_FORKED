from flask import Flask, request, send_file
from celery import Celery, Task
from celery.result import AsyncResult
from pdf2zh import translate_stream
import tqdm
import json
import io
import os
from string import Template
from pdf2zh.doclayout import ModelInstance
from pdf2zh.config import ConfigManager

# v2 端点必须共享同一 RuntimeService 实例：任务存储为内存 dict，
# 每请求新建实例会导致任务状态跨请求不可见（历史缺陷）。
from pdf2zh.services.runtime_singleton import get_runtime_service

flask_app = Flask("pdf2zh")
flask_app.config.from_mapping(
    CELERY=dict(
        broker_url=ConfigManager.get("CELERY_BROKER", "redis://127.0.0.1:6379/0"),
        result_backend=ConfigManager.get("CELERY_RESULT", "redis://127.0.0.1:6379/0"),
    )
)


def celery_init_app(app: Flask) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name)
    celery_app.config_from_object(app.config["CELERY"])
    celery_app.Task = FlaskTask
    celery_app.set_default()
    celery_app.autodiscover_tasks()
    app.extensions["celery"] = celery_app
    return celery_app


celery_app = celery_init_app(flask_app)


@celery_app.task(bind=True)
def translate_task(
    self: Task,
    stream: bytes,
    args: dict,
):
    def progress_bar(t: tqdm.tqdm):
        self.update_state(state="PROGRESS", meta={"n": t.n, "total": t.total})  # noqa
        print(f"Translating {t.n} / {t.total} pages")

    if not isinstance(args, dict):
        raise ValueError("translation args must be a JSON object")
    if "prompt" in args:
        if isinstance(args["prompt"], str):
            args["prompt"] = Template(args["prompt"])
    if args.get("pages") not in (None, ""):
        from pdf2zh.v3.ingestion.config import normalize_user_page_indices

        args["pages"] = normalize_user_page_indices(args["pages"])

    doc_dual, doc_mono = translate_stream(
        stream,
        callback=progress_bar,
        model=ModelInstance.value,
        **args,
    )
    if not doc_mono or not doc_dual:
        raise RuntimeError("translate_stream returned empty output")
    return doc_mono, doc_dual


@flask_app.route("/v1/translate", methods=["POST"])
def create_translate_tasks():
    file = request.files.get("file")
    if file is None:
        return {"error": "No file provided"}, 400
    stream = file.stream.read()
    try:
        args = json.loads(request.form.get("data") or "{}")
    except (TypeError, ValueError) as exc:
        return {"error": f"invalid data: {exc}"}, 400
    if not isinstance(args, dict):
        return {"error": "data must be a JSON object"}, 400
    task = translate_task.delay(stream, args)
    return {"id": task.id}


@flask_app.route("/v1/translate/<id>", methods=["GET"])
def get_translate_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    if str(result.state) == "PROGRESS":
        return {"state": str(result.state), "info": result.info}
    else:
        return {"state": str(result.state)}


@flask_app.route("/v1/translate/<id>", methods=["DELETE"])
def delete_translate_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    result.revoke(terminate=True)
    return {"state": str(result.state)}


# ── V2 RuntimeService API ─────────────────────────────────────────────────────


@flask_app.route("/v2/translate", methods=["POST"])
def create_translate_task_v2():
    """Submit a translation task via RuntimeService.

    POST body (JSON):
        {
            "file": <file bytes or file path>,
            "lang_in": "auto",
            "lang_out": "zh-CN",
            "service": "google",
            "pages": null
        }

    Returns:
        {"task_id": "task_abc123"}
    """
    from pdf2zh.services.runtime_service import TranslationRequest

    file = request.files.get("file")
    form_data = request.form.get("data", "{}")
    try:
        args = json.loads(form_data)
    except (TypeError, ValueError) as exc:
        return {"error": f"invalid data: {exc}"}, 400
    if not isinstance(args, dict):
        return {"error": "data must be a JSON object"}, 400

    if file is None:
        return {"error": "No file provided"}, 400

    stream = file.stream.read()
    # Save to temp file for RuntimeService
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        tmp.write(stream)
    finally:
        tmp.close()

    from pdf2zh.v3.ingestion.config import (
        JINA_DEFAULT_DPI,
        JINA_DEFAULT_MAX_NEW_TOKENS,
        JINA_DEFAULT_MAX_PIXELS,
        JINA_DEFAULT_TIMEOUT,
        JINA_MIN_COVERAGE,
        JINA_MODEL_ID,
        JINA_PROMPT,
        JINA_REVISION,
        JinaOcrOptions,
        normalize_ingest_backend,
    )

    from pdf2zh.fs_utils import remove_path_robust

    try:
        magicpdf_ocr_raw = args.get("magicpdf_ocr", False)
        magicpdf_ocr = (
            magicpdf_ocr_raw.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(magicpdf_ocr_raw, str)
            else bool(magicpdf_ocr_raw)
        )
        jina_options = None
        glossary_value = args.get("glossary_files") or []
        if isinstance(glossary_value, str):
            glossary_value = [
                item.strip() for item in glossary_value.split(",") if item.strip()
            ]
        elif not isinstance(glossary_value, list):
            raise ValueError("glossary_files must be a list or comma-separated string")
        if normalize_ingest_backend(args.get("ingest_backend", "auto")) == "jina":
            jina_options = JinaOcrOptions.from_mapping(
                {
                    "model": args.get("jina_model", JINA_MODEL_ID),
                    "revision": args.get("jina_revision", JINA_REVISION),
                    "prompt": args.get("jina_prompt", JINA_PROMPT),
                    "device": args.get("jina_device", "auto"),
                    "dpi": args.get("jina_dpi", JINA_DEFAULT_DPI),
                    "max_pixels": args.get("jina_max_pixels", JINA_DEFAULT_MAX_PIXELS),
                    "max_new_tokens": args.get(
                        "jina_max_new_tokens", JINA_DEFAULT_MAX_NEW_TOKENS
                    ),
                    "timeout": args.get("jina_timeout", JINA_DEFAULT_TIMEOUT),
                    "cache_dir": args.get("jina_cache_dir", ""),
                    "min_coverage": args.get("jina_min_coverage", JINA_MIN_COVERAGE),
                    "offline": args.get("jina_offline", False),
                }
            )
        req = TranslationRequest(
            source_path=tmp.name,
            target_lang=args.get("lang_out", "zh-CN"),
            source_lang=args.get("lang_in", "auto"),
            engine=args.get("service", "google"),
            threads=max(1, min(32, int(args.get("threads", 4) or 4))),
            page_range=args.get("pages"),
            parse_engine=args.get("parse_engine", "auto"),
            ingest_backend=args.get("ingest_backend", "auto"),
            backend=(args.get("backend") or "auto").strip().lower() or "auto",
            output_dir=args.get("output_dir", ""),
            magicpdf_ocr=magicpdf_ocr,
            magicpdf_ocr_mode=(
                str(args.get("magicpdf_ocr_mode") or "auto").strip().lower()
            ),
            mineru_vram_size=str(args.get("mineru_vram_size") or "").strip(),
            mineru_window_size=str(args.get("mineru_window_size") or "").strip(),
            mineru_parse_method=str(args.get("mineru_parse_method") or "").strip(),
            mineru_backend=str(args.get("mineru_backend") or "").strip(),
            glossary_files=[str(item) for item in glossary_value],
            jina_model=(
                jina_options.model
                if jina_options is not None
                else args.get("jina_model", JINA_MODEL_ID)
            ),
            jina_revision=(
                jina_options.revision
                if jina_options is not None
                else args.get("jina_revision", JINA_REVISION)
            ),
            jina_prompt=(
                jina_options.prompt
                if jina_options is not None
                else args.get("jina_prompt", JINA_PROMPT)
            ),
            jina_device=(
                jina_options.device
                if jina_options is not None
                else args.get("jina_device", "auto")
            ),
            jina_dpi=(
                jina_options.dpi
                if jina_options is not None
                else args.get("jina_dpi", JINA_DEFAULT_DPI)
            ),
            jina_max_pixels=(
                jina_options.max_pixels
                if jina_options is not None
                else args.get("jina_max_pixels", JINA_DEFAULT_MAX_PIXELS)
            ),
            jina_max_new_tokens=(
                jina_options.max_new_tokens
                if jina_options is not None
                else args.get("jina_max_new_tokens", JINA_DEFAULT_MAX_NEW_TOKENS)
            ),
            jina_timeout=(
                jina_options.timeout
                if jina_options is not None
                else args.get("jina_timeout", JINA_DEFAULT_TIMEOUT)
            ),
            jina_cache_dir=(
                jina_options.cache_dir
                if jina_options is not None
                else args.get("jina_cache_dir", "")
            ),
            jina_min_coverage=(
                jina_options.min_coverage
                if jina_options is not None
                else args.get("jina_min_coverage", JINA_MIN_COVERAGE)
            ),
            jina_offline=(
                jina_options.offline
                if jina_options is not None
                else args.get("jina_offline", False)
            ),
        )

        svc = get_runtime_service()
        task_id = svc.submit_task(req)
    except (TypeError, ValueError) as exc:
        remove_path_robust(tmp.name, defer=True)
        return {"error": str(exc)}, 400
    except Exception:
        remove_path_robust(tmp.name, defer=True)
        raise

    def _cleanup_input() -> None:
        import time

        for _ in range(86400):
            state = svc.get_task_state(task_id)
            if state is None or state.status in {
                "completed",
                "failed",
                "cancelled",
            }:
                time.sleep(1.0)
                remove_path_robust(tmp.name, defer=True)
                return
            time.sleep(1.0)

    import threading

    threading.Thread(
        target=_cleanup_input,
        name="pdf2zh-v2-input-cleanup",
        daemon=True,
    ).start()
    return {"task_id": task_id}


@flask_app.route("/v2/translate/<task_id>", methods=["GET"])
def get_translate_task_v2(task_id: str):
    """Get translation task state and result.

    Returns:
        {
            "status": "completed",
            "progress": 100.0,
            "stage": "completed",
            "message": "Completed",
            "result_files": [{"name": "...", "path": "..."}],
            "diagnostic_summary": null,
            "quality_scores": null
        }
    """

    svc = get_runtime_service()
    state = svc.get_task_state(task_id)
    if state is None:
        return {"error": "task not found"}, 404
    return state.to_dict()


@flask_app.route("/v2/translate/<task_id>", methods=["DELETE"])
def cancel_translate_task_v2(task_id: str):
    """Cancel a running translation task."""

    svc = get_runtime_service()
    ok = svc.cancel_task(task_id)
    return {"cancelled": ok}


@flask_app.route("/v2/translate/<task_id>/artifacts/<format>")
def get_translate_artifact_v2(task_id: str, format: str):
    """Download translation result by format (pdf, mono, dual)."""

    svc = get_runtime_service()
    state = svc.get_task_state(task_id)
    if state is None:
        return {"error": "task not found"}, 404
    if state.status != "completed":
        return {"error": "task not completed"}, 400
    if not state.result_files:
        return {"error": "no result files"}, 400

    # Find matching file
    for f in state.result_files:
        name = f.get("name", "")
        if format == "pdf" and "-mono" in name:
            continue
        if format in name or format == "pdf":
            path = f.get("path")
            if path and os.path.exists(path):
                return send_file(path, "application/pdf")
    return {"error": f"No artifact for format: {format}"}, 404


@flask_app.route("/v1/translate/<id>/<format>")
def get_translate_result(id: str, format: str):
    if format not in {"mono", "dual"}:
        return {"error": "format must be mono or dual"}, 400
    result = celery_app.AsyncResult(id)
    if not result.ready():
        return {"error": "task not finished"}, 400
    if not result.successful():
        return {"error": "task failed"}, 400
    doc_mono, doc_dual = result.get()
    to_send = doc_mono if format == "mono" else doc_dual
    return send_file(io.BytesIO(to_send), "application/pdf")


if __name__ == "__main__":
    from pdf2zh.pdf2zh import spawn_child_yields_to

    if spawn_child_yields_to():
        raise SystemExit(0)
    flask_app.run()
