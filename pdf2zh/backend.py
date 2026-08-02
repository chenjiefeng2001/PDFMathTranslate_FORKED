from flask import Flask, request, send_file
from celery import Celery, Task
from celery.result import AsyncResult
from pdf2zh import translate_stream
import tqdm
import json
import io
from string import Template
from pdf2zh.doclayout import ModelInstance
from pdf2zh.config import ConfigManager

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

    if "prompt" in args:
        args["prompt"] = Template(args["prompt"])

    doc_mono, doc_dual = translate_stream(
        stream,
        callback=progress_bar,
        model=ModelInstance.value,
        **args,
    )
    return doc_mono, doc_dual


@flask_app.route("/v1/translate", methods=["POST"])
def create_translate_tasks():
    file = request.files["file"]
    stream = file.stream.read()
    print(request.form.get("data"))
    args = json.loads(request.form.get("data"))
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
    from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest

    file = request.files.get("file")
    form_data = request.form.get("data", "{}")
    args = json.loads(form_data)

    if file is None:
        return {"error": "No file provided"}, 400

    stream = file.stream.read()
    # Save to temp file for RuntimeService
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(stream)
    tmp.close()

    req = TranslationRequest(
        source_path=tmp.name,
        target_lang=args.get("lang_out", "zh-CN"),
        source_lang=args.get("lang_in", "auto"),
        engine=args.get("service", "google"),
        page_range=args.get("pages"),
    )

    svc = RuntimeService()
    task_id = svc.submit_task(req)
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
    from pdf2zh.services.runtime_service import RuntimeService

    svc = RuntimeService()
    state = svc.get_task_state(task_id)
    if state is None:
        return {"error": "task not found"}, 404
    return state.to_dict()


@flask_app.route("/v2/translate/<task_id>", methods=["DELETE"])
def cancel_translate_task_v2(task_id: str):
    """Cancel a running translation task."""
    from pdf2zh.services.runtime_service import RuntimeService

    svc = RuntimeService()
    ok = svc.cancel_task(task_id)
    return {"cancelled": ok}


@flask_app.route("/v2/translate/<task_id>/artifacts/<format>")
def get_translate_artifact_v2(task_id: str, format: str):
    """Download translation result by format (pdf, mono, dual)."""
    from pdf2zh.services.runtime_service import RuntimeService

    svc = RuntimeService()
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

def delete_translate_task(id: str):
    result: AsyncResult = celery_app.AsyncResult(id)
    result.revoke(terminate=True)
    return {"state": str(result.state)}


@flask_app.route("/v1/translate/<id>/<format>")
def get_translate_result(id: str, format: str):
    result = celery_app.AsyncResult(id)
    if not result.ready():
        return {"error": "task not finished"}, 400
    if not result.successful():
        return {"error": "task failed"}, 400
    doc_mono, doc_dual = result.get()
    to_send = doc_mono if format == "mono" else doc_dual
    return send_file(io.BytesIO(to_send), "application/pdf")


if __name__ == "__main__":
    flask_app.run()
