[**Documentation**](https://github.com/Byaidu/PDFMathTranslate) > **API Details** _(current)_

<h2 id="toc">Table of Content</h2>

- [Functional calls in Python](#api-python)
- [HTTP API (current)](#api-http) — `pdf2zh --api`, port `11009`, namespace `/api/*`
  - [Conventions](#api-http-conventions)
  - [Environment variables](#api-http-env)
  - [Health and engines](#api-http-health)
  - [Tasks](#api-http-tasks)
  - [Glossaries](#api-http-glossaries)
  - [Self-test, models and setup](#api-http-setup)
- [Legacy HTTP API](#api-http-legacy) — `pdf2zh --flask` + Celery, port `11008`, namespace `/v1/*`

---

<h2 id="api-python">Python</h2>

As `pdf2zh` is an installed module in Python, we expose two methods for other programs to call in any Python scripts.

For example, if you want translate a document from English to Chinese using Google Translate, you may use the following code:

```python
from pdf2zh import translate, translate_stream

params = {
    'lang_in': 'en',
    'lang_out': 'zh',
    'service': 'google',
    'thread': 4,
}
```
Translate with files:
```python
(file_mono, file_dual) = translate(files=['example.pdf'], **params)[0]
```
Translate with stream:
```python
with open('example.pdf', 'rb') as f:
    (stream_mono, stream_dual) = translate_stream(stream=f.read(), **params)
```

[⬆️ Back to top](#toc)

---

<h2 id="api-http">HTTP API (current)</h2>

The shipped HTTP surface is a FastAPI server. It is what the desktop app and the
SPA talk to, and it needs **no Redis and no Celery**:

```bash
pdf2zh --api                 # http://127.0.0.1:11009
pdf2zh --api --port 12000    # custom port
python -m pdf2zh.services.api --port 11009   # equivalent
```

Interactive schema (when the server is running): `http://127.0.0.1:11009/docs`
and `http://127.0.0.1:11009/openapi.json`.

All paths below are relative to `http://127.0.0.1:11009`. In the desktop build the
port is chosen dynamically and injected into the page as
`window.__PDF2ZH_RUNTIME__.apiBase`, so clients should read it from there rather
than hard-coding `11009`.

### <h3 id="api-http-conventions">Conventions</h3>

Read this before writing a client; the rules below are enforced by
`tests/test_services_api.py::TestContractNormalization`.

| Topic | Rule |
|---|---|
| Success status | **Always `200`.** There is no `201`/`202`/`204` in this API. |
| Error body | `{"detail": "<human readable>"}` — for handler errors *and* for the auth/Host guard. |
| Validation errors | `422` with `{"detail": [ {...}, ... ]}` (an **array** of FastAPI/Pydantic issues). Clients must handle both shapes. |
| "Already in progress" | `200` + `{"started": false, "reason": "already running"}` — **not** `409`. |
| Self-test endpoints | Always `200`; success/failure is the `ok` field. |
| List endpoints | `GET /api/engines`, `/api/glossaries`, `/api/tasks`, `/api/tasks/{task_id}/artifacts` return a **bare JSON array**. Every other endpoint returns an object. |
| Booleans | `true`/`false` (FastAPI form coercion also accepts `1`/`0`/`true`/`false`). |
| Numeric bounds | `threads` is silently clamped to `1..32`; the `jina_*` numeric fields are validated with `400` when the Jina backend is selected. |
| Submit content type | `POST /api/tasks` accepts **only** `multipart/form-data` (or `x-www-form-urlencoded`). A JSON body is rejected with `415` — otherwise every field would be silently dropped. |
| Task ids | `task_` + 12 hex chars. |

### <h3 id="api-http-env">Environment variables</h3>

| Variable | Default | Effect |
|---|---|---|
| `PDF2ZH_API_TOKEN` | unset | When set, every `/api/*` route **except `/api/health`** requires the token (see [Auth](#api-http-auth)). |
| `PDF2ZH_API_TRUST_ANY_HOST` | unset | `1` disables the loopback Host/Origin guard (needed for LAN/reverse-proxy access). |
| `PDF2ZH_ALLOWED_SOURCE_DIRS` | unset | `os.pathsep`-separated allow-list for `source_path`. Without it, `source_path` is rejected with `403`. |
| `PDF2ZH_MAX_UPLOAD_MB` | `200` | Upload size limit; exceeding it returns `413`. |
| `PDF2ZH_SPA_DIR` | unset | Serve a built SPA from this directory at `/`. |
| `PDF2ZH_API_PORT` | `11009` | Port used by `pdf2zh --api` / `python -m pdf2zh.services.api`. |

<a id="api-http-auth"></a>**Auth.** With `PDF2ZH_API_TOKEN=secret`, send the token
by any of:

```bash
-H "X-API-Key: secret"
-H "Authorization: Bearer secret"
# or, less ideal because it lands in logs:
"?api_key=secret"
```

Missing/invalid → `401` + `WWW-Authenticate: Bearer`, body
`{"detail": "unauthorized: missing/invalid API key"}`.

**Host/Origin guard.** By default only loopback `Host` values
(`127.0.0.1`, `localhost`, `::1`) and the origin `tauri.localhost` are accepted;
anything else → `403`
`{"detail": "forbidden: untrusted Host/Origin"}`. Set
`PDF2ZH_API_TRUST_ANY_HOST=1` to lift it (only do this behind an authenticating
proxy). Passing `allow_origins=["*"]` is **ignored** and logged as a warning.

### <h3 id="api-http-health">Health and engines</h3>

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | `{"status": "ok", "tasks": <int>}`. The only auth-exempt route. |
| `GET` | `/api/engines` | `[{ "name", "label", "envs": [{ "key", "configured" }] }]` — credential **values** are never returned. |
| `GET` | `/api/engines/{name}/envs` | `{ "name", "envs": [{ "key", "configured", "masked" }] }`. `masked` is `""` (unset) or `abc****wxyz`. `404` if the engine is unknown. |
| `PUT` | `/api/engines/{name}/envs` | Body `{"envs": {"KEY": "value"}}`; `null` or `""` clears a key. Returns the same shape as the GET. `400` on unknown keys, `422` on a malformed body. |

```bash
curl http://127.0.0.1:11009/api/health
{"status":"ok","tasks":0}

curl http://127.0.0.1:11009/api/engines
[{"name":"google","label":"GoogleTranslator","envs":[{"key":"GOOGLE_TRANSLATE","configured":false}]}, ...]

curl -X PUT http://127.0.0.1:11009/api/engines/google/envs \
  -H "Content-Type: application/json" \
  -d '{"envs": {"GOOGLE_TRANSLATE": ""}}'
```

### <h3 id="api-http-tasks">Tasks</h3>

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/tasks` | Submit a translation task (multipart). Returns `{"task_id": "task_..."}`. |
| `GET` | `/api/tasks` | Array of task states in **summary** mode (heavy per-page snapshots are `null`). |
| `GET` | `/api/tasks/{task_id}` | Full task state. `404` if unknown. |
| `DELETE` | `/api/tasks/{task_id}` | Cancel → `{"cancelled": true|false}`. `false` means the task had already reached a terminal state. |
| `POST` | `/api/tasks/{task_id}/pause` | → `{"pause_task": bool}` |
| `POST` | `/api/tasks/{task_id}/resume` | → `{"resume_task": bool}` |
| `POST` | `/api/tasks/{task_id}/skip` | → `{"skip_task": bool}` (batch: skip the current file) |
| `GET` | `/api/tasks/{task_id}/events` | SSE progress stream (see below). |
| `GET` | `/api/tasks/{task_id}/artifacts` | `[{ "index": <int>, "path", "name", "type" }]` — `index` is an integer, ready to be pasted into the download URL. |
| `GET` | `/api/tasks/{task_id}/artifacts/{index}` | The artifact PDF. `404` for an out-of-range index or a missing file. |
| `GET` | `/api/tasks/{task_id}/result-zip` | Pre-built ZIP of all results. `404` when the task has no ZIP (e.g. healthy single-file tasks). |

**Task state** (both `GET /api/tasks` and `GET /api/tasks/{task_id}`) is the
`TaskState` serialization. The fields clients actually depend on:

`task_id`, `status`, `stage`, `progress`, `total_progress`, `file_progress`,
`message`, `error_message`, `current_file_name`, `file_list`, `total_files`,
`completed_files`, `failed_files`, `file_failures`, `result_files`,
`result_zip`, `result_zip_name`, `selected_file`, `parse_engine`,
`mode_choice`, `eta`, `created_at`, `updated_at`, plus optional diagnostic
report fields (`diagnostic_summary`, `quality_scores`, `gate_verdicts`, …).

`status` is one of `pending`, `running`, `paused`, `parsing`, `normalizing`,
`analyzing`, `planning`, `translating`, `layouting`, `rendering`, `evaluating`,
`repairing`, `completed`, `cancelled`, `failed`. **Terminal states are
absorbing**: once a task is `completed`/`cancelled`/`failed`, a late worker
callback cannot flip it, and a cancel request for an already finished task
returns `{"cancelled": false}`.

#### Submitting

`POST /api/tasks` takes `multipart/form-data`. The only **required** input is at
least one of `file` / `files` / `source_path`; every field below has a default.

| Field | Type | Default | Notes |
|---|---|---|---|
| `file` | file | – | Single upload. |
| `files` | file (repeatable) | – | Batch upload; takes precedence over `file`. |
| `source_path` | text | `""` | Server-side path. Rejected with `403` unless it lives under `PDF2ZH_ALLOWED_SOURCE_DIRS`. |
| `target_lang` | text | `zh-CN` | |
| `source_lang` | text | `auto` | |
| `engine` | text | `google` | Any name from `GET /api/engines`. |
| `threads` | int | `4` | Silently clamped to `1..32`. |
| `page_range` | text | `""` | One-based, inclusive: `""`/`all` = whole document, `15`, `1-10`, `1-10,15,20-30` (both bounds required, ascending, ≤ 10000 pages). `400` if malformed. |
| `parse_engine` | text | `auto` | `auto`/`legacy`/`babeldoc`/`magicpdf`; `400` otherwise. |
| `ingest_backend` | text | `auto` | `auto`/`mineru`/`marker`/`jina`; `400` otherwise. |
| `mode_choice` | text | `auto` | |
| `ocr_mode` | text | `auto` | |
| `backend` | text | `auto` | ONNX provider: `auto`/`cpu`/`cuda`/`dml`. |
| `output_dir` | text | `""` | |
| `ignore_cache` | bool | `false` | |
| `extra_config` | text (JSON object) | `""` | `400` if not valid JSON or not an object. |
| `glossaries` | file (repeatable) | – | Glossary CSV uploads for this task. |
| `glossary_files` | text (JSON array or CSV) | `""` | `400` on malformed input or a missing file. |
| `mineru_vram_size` | text | `""` | MinerU VRAM budget. |
| `mineru_window_size` | text | `""` | MinerU window size. |
| `mineru_parse_method` | text | `""` | `auto`/`txt`/`ocr`. |
| `mineru_backend` | text | `""` | MinerU pipeline backend. |
| `jina_model` | text | `jinaai/jina-ocr-v1` | Jina OCR. |
| `jina_revision` | text | `904f815deed0fbeab02d82d4648b70106272ffe3` | Pinned revision (required for remote models). |
| `jina_prompt` | text | `Transcribe the provided document image into a clean Markdown format, preserving the natural reading order.` | Must be non-empty. |
| `jina_device` | text | `auto` | `auto`/`cpu`/`cuda`/`cuda:<n>`. |
| `jina_dpi` | int | `200` | 72–600. |
| `jina_max_pixels` | int | `4000000` | 1e6–64e6. |
| `jina_max_new_tokens` | int | `4096` | 1–32768. |
| `jina_timeout` | float | `3600.0` | 1–86400 seconds. |
| `jina_cache_dir` | text | `""` | Empty disables the page cache. |
| `jina_min_coverage` | float | `0.20` | 0.0–1.0. |
| `jina_offline` | bool | `false` | |
| `trace_enabled` | bool | `false` | magic-pdf flight-recorder trace. |
| `trace_dir` | text | `""` | |

The `jina_*` fields are only validated when `ingest_backend=jina`; other values
are forwarded unchanged. A non-default `jina_model` additionally requires
`PDF2ZH_JINA_ALLOW_CUSTOM_MODEL=1` (`400` otherwise), because third-party models
are not covered by the pinned revision.

**Error codes for `POST /api/tasks`:**

| Code | When |
|---|---|
| `400` | `extra_config` / `glossary_files` malformed, unknown `ingest_backend` or `parse_engine`, malformed `page_range`, invalid Jina options, custom Jina model without the opt-in env var |
| `403` | `source_path` outside `PDF2ZH_ALLOWED_SOURCE_DIRS` |
| `404` | `source_path` does not exist (only enforced when an API token is configured) |
| `413` | upload exceeds `PDF2ZH_MAX_UPLOAD_MB` |
| `415` | `Content-Type` is not `multipart/form-data` / `x-www-form-urlencoded` |

A submission with **no** file and no `source_path` is accepted (`200` +
`task_id`) and the task then fails fast with
`error_message: "No source files provided"` — the failure is visible in the
task list rather than being hidden behind a request error.

```bash
# Submit (single file)
curl http://127.0.0.1:11009/api/tasks \
  -F "file=@example.pdf" -F "target_lang=zh" -F "engine=google"
{"task_id":"task_1a2b3c4d5e6f"}

# Submit (batch: repeat the file part)
curl http://127.0.0.1:11009/api/tasks \
  -F "files=@a.pdf" -F "files=@b.pdf" -F "page_range=1-10"

# Poll
curl http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f
{"task_id":"task_1a2b3c4d5e6f","status":"completed","progress":100.0, ...}

# List (summary)
curl http://127.0.0.1:11009/api/tasks

# Cancel
curl -X DELETE http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f
{"cancelled":true}

# Results
curl http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f/artifacts
[{"index":0,"path":"...","name":"example-mono.pdf","type":""},{"index":1,"path":"...","name":"example-dual.pdf","type":""}]
curl http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f/artifacts/0 --output example-mono.pdf
curl http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f/result-zip --output results.zip
```

#### Progress stream (SSE)

```bash
curl -N http://127.0.0.1:11009/api/tasks/task_1a2b3c4d5e6f/events
```

Frame sequence: `retry: 3000`, one `state` frame with the full task snapshot,
then `progress` / `log` / `notice` frames carrying `id: <seq>`, then a final
`done` frame `{"status": "completed"}`. A `data: {"message": ...}` `error` frame
means the task id is unknown.

- Resumable: pass `?since=<seq>` or the standard `Last-Event-ID` header
  (`Last-Event-ID` wins). The server replays from the per-task ring buffer.
- Idle connections get a `: keep-alive` comment every 15 s.
- At most **16** concurrent streams; beyond that `503`
  `{"detail": "too many concurrent event streams; retry later"}`.
- If a client stops consuming, the server ends the stream instead of dropping a
  frame — the browser `EventSource` reconnects with its last id and the gap is
  replayed.

### <h3 id="api-http-glossaries">Glossaries</h3>

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/glossaries` | `[{ "name", "path", "entries": <int|null>, "error": <only if the CSV is unreadable> }]` |
| `POST` | `/api/glossaries` | multipart with a required `file` (CSV) and optional `name` → `{ "name", "path", "entries" }`. `400` on a bad CSV, `413` on oversize, `422` when `file` is missing. |
| `GET` | `/api/glossaries/{name}/download` | `text/csv` attachment (UTF-8 with BOM). `404` if it cannot be exported. |

```bash
curl -F "file=@terms.csv" http://127.0.0.1:11009/api/glossaries
curl http://127.0.0.1:11009/api/glossaries
curl http://127.0.0.1:11009/api/glossaries/terms/download --output terms.csv
```

### <h3 id="api-http-setup">Self-test, models and setup</h3>

These endpoints are how the desktop settings panel provisions optional
back-ends. They are poll-based: a `POST` starts background work and a `GET`
reports progress.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/selftest/babeldoc` | `{ "ok", "error" }` — BabelDOC import chain. |
| `GET` | `/api/selftest/magicpdf` | `{ "ok", "backend", "hint", "mineru_cuda", "mineru_venv" }`. |
| `GET` | `/api/selftest/jina` | `{ "ok", "interpreter", "hint" }`. |
| `GET` | `/api/models/doclayout` | `{ "path", "exists", "size_bytes", "sha_ok", "downloading", "last_error" }`. |
| `POST` | `/api/models/doclayout/download` | `{"started": true}` or `{"started": false, "reason": "already running"}`. |
| `GET` | `/api/gpu/provider` | `{ "onnxruntime_version", "target_path", "cuda_dll_present", "cuda_dll_size_bytes", "available_providers", "cuda_active", "downloading", "progress_bytes", "total_bytes", "done", "last_error" }` |
| `POST` | `/api/gpu/provider/download` | `{"started", "reason"}` — on-demand CUDA execution provider (not shipped in the installer). |
| `POST` | `/api/gpu/provider/remove` | `{"removed": bool}`. |
| `POST` | `/api/setup/mineru` | `{"started", "reason"}` — build the isolated MinerU environment. |
| `GET` | `/api/setup/mineru` | `{ "running", "done", "error", "interpreter" }` |
| `POST` | `/api/setup/mineru/cuda` | `{"started", "reason"}` — swap MinerU's torch for the CUDA build. |
| `GET` | `/api/setup/mineru/cuda` | `{ "running", "done", "error", "interpreter" }` |
| `POST` | `/api/setup/jina` | `{"started", "reason"}` — build the isolated Jina OCR runtime. |
| `GET` | `/api/setup/jina` | `{ "running", "done", "error", "interpreter" }` |

`hint` fields carry the exact command to run (for example
`Install the isolated runtime with: pdf2zh-setup-jina`). Each backend has a
matching CLI entry point: `pdf2zh-setup-mineru`, `pdf2zh-setup-jina`,
`pdf2zh-setup-precise`.

[⬆️ Back to top](#toc)

---

<h2 id="api-http-legacy">Legacy HTTP API (deprecated)</h2>

> **Deprecated.** The endpoints below belong to the Flask + Celery server
> (`pdf2zh --flask`, port `11008`, **requires Redis and a Celery worker**). They
> are kept for backwards compatibility only: they are not covered by tests, are
> not used by the desktop app or the SPA, and receive no bug fixes. New
> integrations should use the [current API](#api-http).

```bash
pip install pdf2zh[backend]
pdf2zh --flask
pdf2zh --celery worker
```

| Method | Path | Response |
|---|---|---|
| `POST` | `/v1/translate` | `{"id": "<celery task id>"}` |
| `GET` | `/v1/translate/{id}` | `{"state": "PROGRESS", "info": {"n", "total"}}`, then `{"state": "SUCCESS"}` |
| `GET` | `/v1/translate/{id}/mono` | the monolingual PDF (`400` if unfinished) |
| `GET` | `/v1/translate/{id}/dual` | the bilingual PDF (`400` if unfinished) |
| `DELETE` | `/v1/translate/{id}` | `{"state": "<celery state>"}` after `revoke(terminate=True)` |
| `POST` | `/v2/translate` | `{"task_id": "..."}` (RuntimeService-backed) |
| `GET` | `/v2/translate/{task_id}` | RuntimeService task state |
| `DELETE` | `/v2/translate/{task_id}` | `{"cancelled": bool}` |
| `GET` | `/v2/translate/{task_id}/artifacts/{format}` | `format` ∈ `pdf` / `mono` / `dual` |

In the table, path parameters are written `{id}`; the Flask routes declare them
as `<id>`, and `/v1/translate/{id}/mono` and `.../dual` are two literal values of
the single Flask route `/v1/translate/<id>/<format>` (any other value returns
`400 {"error": "format must be mono or dual"}`).

Note the naming drift between the two generations: submit returns `id` on `/v1`
but `task_id` on `/v2`; page selection is `pages` (list) on `/v1` but
`page_range` (string) on `/v2`; the thread count is `thread` on `/v1` but
`threads` on `/v2`. Errors use `{"error": "..."}` on these legacy routes rather
than the `{"detail": "..."}` of the current API.

<details>
<summary>Legacy v1 examples (verbatim)</summary>

```bash
curl http://localhost:11008/v1/translate -F "file=@example.pdf" -F "data={\"lang_in\":\"en\",\"lang_out\":\"zh\",\"service\":\"google\",\"thread\":4}"
{"id":"d9894125-2f4e-45ea-9d93-1a9068d2045a"}

curl http://localhost:11008/v1/translate/d9894125-2f4e-45ea-9d93-1a9068d2045a
{"info":{"n":13,"total":506},"state":"PROGRESS"}

curl http://localhost:11008/v1/translate/d9894125-2f4e-45ea-9d93-1a9068d2045a/mono --output example-mono.pdf
curl http://localhost:11008/v1/translate/d9894125-2f4e-45ea-9d93-1a9068d2045a/dual --output example-dual.pdf
curl http://localhost:11008/v1/translate/d9894125-2f4e-45ea-9d93-1a9068d2045a -X DELETE
```

</details>

[⬆️ Back to top](#toc)

---
