"""Phase A REST/SSE API 层单元测试（pdf2zh.services.api）。



不触发真实翻译：submit_task 被 monkeypatch 记录请求并返回假任务号；

SSE 用「空 source_path → 立即 FAILED」的真实服务路径验证初始帧与终态帧。

"""

import json
import threading
import time


import pytest


from fastapi.testclient import TestClient

from pathlib import Path


from pdf2zh.services.api import create_api_app

from pdf2zh.services.runtime_service import RuntimeService

from pdf2zh.services.runtime_singleton import (
    get_runtime_service,
    reset_runtime_service,
)
from pdf2zh.v3.ingestion.config import JINA_REVISION


@pytest.fixture()
def fresh_service():

    reset_runtime_service()

    yield RuntimeService()

    reset_runtime_service()


def _client(monkeypatch, service=None):

    if service is not None:

        monkeypatch.setattr(
            type(service),
            "submit_task",
            lambda self, request: (
                getattr(self, "_fake_id", None)
                or setattr(self, "_fake_id", "task_fake")
                or self._fake_id
            ),
        )

    return TestClient(
        create_api_app(service=service), base_url="http://127.0.0.1:11009"
    )


class TestHealthAndEngines:

    def test_health(self, monkeypatch):

        client = _client(monkeypatch)

        resp = client.get("/api/health")

        assert resp.status_code == 200

        body = resp.json()

        assert body["status"] == "ok"

        assert isinstance(body["tasks"], int)

    def test_engines_registry_and_masking(self, monkeypatch):

        client = _client(monkeypatch)

        resp = client.get("/api/engines")

        assert resp.status_code == 200

        engines = resp.json()

        names = [e["name"] for e in engines]

        # 注册表覆盖：传统 + LLM + opencode

        for expected in ("google", "openai", "deepl", "opencode"):

            assert expected in names

        opencode = next(e for e in engines if e["name"] == "opencode")

        keys = {e["key"] for e in opencode["envs"]}

        assert {"OPENCODE_PATH", "OPENCODE_MODEL", "OPENCODE_SERVER_URL"} <= keys

        # 关键安全约束：envs 只回显 configured 布尔，绝不回显值

        for engine in engines:

            for item in engine["envs"]:

                assert set(item.keys()) == {"key", "configured"}


class TestSubmit:

    def test_submit_maps_form_fields(self, monkeypatch, fresh_service):

        captured = {}

        def fake_submit(self, request):

            captured["request"] = request

            return "task_abc"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "target_lang": "ja",
                "engine": "opencode:opencode/gpt-5",
                "threads": 99,
                "page_range": "1-3",
                "mode_choice": "quality",
            },
        )

        assert resp.status_code == 200

        assert resp.json() == {"task_id": "task_abc"}

        req = captured["request"]

        assert req.target_lang == "ja"

        assert req.engine == "opencode:opencode/gpt-5"

        assert req.threads == 32  # 上限钳制

        assert req.page_range == "1-3"

        assert req.extra_config.get("mode_choice") == "quality"

    def test_submit_upload_writes_temp_file(self, monkeypatch, tmp_path, fresh_service):

        captured = {}

        def fake_submit(self, request):

            captured["request"] = request

            return "task_up"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={"target_lang": "zh"},
            files={"file": ("doc.pdf", b"%PDF-1.4 fake")},
        )

        assert resp.status_code == 200

        from pathlib import Path

        src = Path(captured["request"].source_path)

        assert src.exists() and src.read_bytes().startswith(b"%PDF")

    def test_submit_rejects_bad_extra_config(self, monkeypatch, fresh_service):

        monkeypatch.setattr(RuntimeService, "submit_task", lambda self, r: "task_x")

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post("/api/tasks", data={"extra_config": "{bad json"})

        assert resp.status_code == 400

    def test_submit_maps_trace_fields(self, monkeypatch, fresh_service):

        captured = {}

        def fake_submit(self, request):

            captured["request"] = request

            return "task_tr"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "trace_enabled": "true",
                "trace_dir": "C:/traces",
            },
        )

        assert resp.status_code == 200

        req = captured["request"]

        assert req.extra_config.get("trace_enabled") is True

        assert req.extra_config.get("trace_dir") == "C:/traces"

    def test_submit_trace_off_by_default(self, monkeypatch, fresh_service):

        captured = {}

        def fake_submit(self, request):

            captured["request"] = request

            return "task_notr"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={"source_path": "/tmp/a.pdf", "trace_enabled": "false"},
        )

        assert resp.status_code == 200

        req = captured["request"]

        assert "trace_enabled" not in req.extra_config

        assert "trace_dir" not in req.extra_config


class TestBatchSubmit:

    def _client_with_capture(self, monkeypatch, fresh_service):

        captured = {}

        def fake_submit(self, request):

            captured["request"] = request

            return "task_batch"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        return client, captured

    def test_multi_file_upload_builds_batch_request(
        self, monkeypatch, tmp_path, fresh_service
    ):

        client, captured = self._client_with_capture(monkeypatch, fresh_service)

        resp = client.post(
            "/api/tasks",
            data={"target_lang": "zh"},
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 aaa")),
                ("files", ("b.pdf", b"%PDF-1.4 bbb")),
                ("files", ("c.docx", b"PK docx")),
            ],
        )

        assert resp.status_code == 200

        req = captured["request"]

        assert len(req.files) == 3

        # 上传文件保留原始文件名(不再加 uuid 前缀)
        names = [Path(p).name for p in req.files]

        assert names == ["a.pdf", "b.pdf", "c.docx"]

        # 每个上传文件均已落盘且内容一致；source_path 回落为第一个文件
        for path, payload in zip(
            req.files, [b"%PDF-1.4 aaa", b"%PDF-1.4 bbb", b"PK docx"]
        ):
            assert Path(path).read_bytes() == payload

        assert req.source_path == req.files[0]

    def test_mixed_upload_and_source_path(self, monkeypatch, fresh_service):

        client, captured = self._client_with_capture(monkeypatch, fresh_service)

        resp = client.post(
            "/api/tasks",
            data={"source_path": ""},
            files=[("files", ("only.pdf", b"%PDF-1.4 x"))],
        )

        assert resp.status_code == 200

        assert len(captured["request"].files) == 1

    def test_result_zip_routes(self, monkeypatch, fresh_service, tmp_path):

        import io as _io

        import zipfile as _zipfile

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        # 未知任务 / 无 ZIP 状态 → 404
        assert client.get("/api/tasks/nope/result-zip").status_code == 404

        tid = "task_zipped"

        fresh_service._store.create_task(tid)

        assert client.get(f"/api/tasks/{tid}/result-zip").status_code == 404

        # 有真实 ZIP → 200 且内容可解包
        out_pdf = tmp_path / "out-mono.pdf"

        out_pdf.write_bytes(b"%PDF-1.4 ok")

        zip_path = tmp_path / "results.zip"

        with _zipfile.ZipFile(zip_path, "w") as zf:

            zf.write(out_pdf, arcname="out-mono.pdf")

        fresh_service._store.update_task(tid, result_zip=str(zip_path))

        resp = client.get(f"/api/tasks/{tid}/result-zip")

        assert resp.status_code == 200

        assert resp.headers["content-type"].startswith("application/zip")

        assert _zipfile.ZipFile(_io.BytesIO(resp.content)).namelist() == [
            "out-mono.pdf"
        ]

    def test_legacy_single_file_still_accepted(self, monkeypatch, fresh_service):

        client, captured = self._client_with_capture(monkeypatch, fresh_service)

        resp = client.post(
            "/api/tasks",
            data={},
            files={"file": ("legacy.pdf", b"%PDF-1.4 legacy")},
        )

        assert resp.status_code == 200

        req = captured["request"]

        assert len(req.files) == 1

        assert Path(req.files[0]).exists()


class TestTaskLifecycle:

    @pytest.fixture()
    def failed_task_client(self, fresh_service):
        """空 source_path → submit_task 直接落 FAILED（无文件快速失败路径）。"""

        return (
            TestClient(
                create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
            ),
            fresh_service,
        )

    def test_unknown_task_404(self, failed_task_client):

        client, _ = failed_task_client

        assert client.get("/api/tasks/nope").status_code == 404

        assert client.delete("/api/tasks/nope").status_code == 404

        assert client.get("/api/tasks/nope/artifacts").status_code == 404

    def test_empty_source_fails_fast_then_visible(self, failed_task_client):

        client, svc = failed_task_client

        resp = client.post("/api/tasks", data={})

        assert resp.status_code == 200

        tid = resp.json()["task_id"]

        state = client.get(f"/api/tasks/{tid}").json()

        assert state["status"] in ("failed", "pending")

        listed = {t["task_id"] for t in client.get("/api/tasks").json()}

        assert tid in listed


class TestSseStream:

    def test_initial_state_and_done_frames(self, fresh_service):

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        tid = client.post("/api/tasks", data={}).json()["task_id"]

        frames = []

        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:

            assert resp.headers["content-type"].startswith("text/event-stream")

            for line in resp.iter_lines():

                frames.append(line)

                if any(line.startswith("event: done") for line in frames):

                    break

        joined = "\n".join(frames)

        assert "event: state" in joined

        assert "event: done" in joined

        state_payload = json.loads(joined.split("data: ", 1)[1].split("\n")[0])

        assert state_payload["task_id"] == tid

    def test_last_event_id_replay(self, fresh_service):
        """断线续传：Last-Event-ID=1 时只重放第 2 条事件，且帧带 id 行。"""

        import time as _time

        from pdf2zh.services.runtime_service import TaskProgressEvent

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        tid = client.post("/api/tasks", data={}).json()["task_id"]

        fresh_service._store.add_event(
            tid,
            TaskProgressEvent(
                task_id=tid,
                stage="parsing",
                progress=10.0,
                message="first",
                timestamp=_time.time(),
            ),
        )

        fresh_service._store.add_event(
            tid,
            TaskProgressEvent(
                task_id=tid,
                stage="translating",
                progress=50.0,
                message="second",
                timestamp=_time.time(),
            ),
        )

        frames = []

        with client.stream(
            "GET",
            f"/api/tasks/{tid}/events",
            headers={"Last-Event-ID": "1"},
        ) as resp:

            for line in resp.iter_lines():

                frames.append(line)

                if any(line.startswith("event: done") for line in frames):

                    break

        joined = "\n".join(frames)

        assert "id: 2" in joined, joined

        assert '"seq": 2' in joined

        assert "first" not in joined  # 已消费的第 1 条不重放

        assert "second" in joined


class TestSharedSingleton:

    def test_singleton_identity(self):

        assert get_runtime_service() is get_runtime_service()


class TestSubmitIngestBackend:

    def test_ingest_backend_form_field_maps_to_request(
        self, monkeypatch, fresh_service
    ):

        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_abc"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "parse_engine": "magicpdf",
                "ingest_backend": "marker",
            },
        )
        assert resp.status_code == 200
        assert captured["request"].ingest_backend == "marker"

    def test_ingest_backend_unknown_value_passes_through_for_runtime_check(
        self, monkeypatch, fresh_service
    ):

        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_abc"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "parse_engine": "magicpdf",
                "ingest_backend": "bogus-engine",
            },
        )
        assert resp.status_code == 400

    def test_jina_fields_map_to_request(self, monkeypatch, fresh_service):
        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_jina"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        response = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "parse_engine": "magicpdf",
                "ingest_backend": "jina",
                "jina_model": "jinaai/jina-ocr-v1",
                "jina_revision": JINA_REVISION,
                "jina_prompt": "parse",
                "jina_device": "cuda",
                "jina_dpi": 220,
                "jina_max_pixels": 3_000_000,
                "jina_max_new_tokens": 2048,
                "jina_timeout": 120,
                "jina_cache_dir": "C:/cache/jina",
                "jina_min_coverage": 0.4,
                "jina_offline": "true",
            },
        )
        assert response.status_code == 200
        request = captured["request"]
        assert request.ingest_backend == "jina"
        assert request.jina_device == "cuda:0"
        assert request.jina_dpi == 220
        assert request.jina_offline is True

    def test_invalid_jina_options_return_400(self, monkeypatch, fresh_service):
        monkeypatch.setattr(RuntimeService, "submit_task", lambda self, r: "task_x")
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        response = client.post(
            "/api/tasks",
            data={
                "source_path": "/tmp/a.pdf",
                "ingest_backend": "jina",
                "jina_dpi": "1",
            },
        )
        assert response.status_code == 400

    def test_extra_config_must_be_object(self, monkeypatch, fresh_service):
        monkeypatch.setattr(RuntimeService, "submit_task", lambda self, r: "task_x")
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        response = client.post("/api/tasks", data={"extra_config": "[]"})
        assert response.status_code == 400

    def test_jina_setup_and_status_endpoints(self, monkeypatch, fresh_service):
        import pdf2zh.kernel.jina_ocr_env as jina_env

        monkeypatch.setattr(jina_env, "default_venv_python", lambda: "python-jina")
        monkeypatch.setattr(jina_env, "probe_jina_python", lambda: "python-jina")
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        ready = client.get("/api/selftest/jina")
        assert ready.status_code == 200
        assert ready.json()["ok"] is True

        completed = threading.Event()

        def fake_ensure():
            completed.set()
            return "python-jina"

        monkeypatch.setattr(jina_env, "ensure_venv", fake_ensure)
        started = client.post("/api/setup/jina")
        assert started.status_code == 200
        assert started.json()["started"] is True
        assert completed.wait(2)
        status = None
        for _ in range(50):
            status = client.get("/api/setup/jina")
            if status.status_code == 200 and not status.json()["running"]:
                break
            time.sleep(0.01)
        assert status is not None
        assert status.status_code == 200
        assert status.json()["running"] is False
        assert status.json()["done"] is True
        assert status.json()["interpreter"] == "python-jina"


class TestContractNormalization:
    """API 契约的一致性护栏（响应形状/类型/状态码/信息泄漏）。"""

    def test_json_body_is_rejected_not_silently_ignored(
        self, monkeypatch, fresh_service
    ):
        """POST /api/tasks 只接受 multipart：JSON body 必须是 415，不能 200 空跑。

        回归背景：全部参数都是带默认值的 Form/UploadFile 时，FastAPI 对 JSON
        body 不报错而是全取默认值 → 返回 200 + task_id，但 source_path 等
        字段被丢弃，任务必然 "No source files provided" 失败。
        """
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        resp = client.post("/api/tasks", json={"source_path": "a.pdf"})
        assert resp.status_code == 415
        assert "multipart/form-data" in resp.json()["detail"]
        assert fresh_service.list_task_ids() == []

    def test_artifacts_index_is_int(self, monkeypatch, fresh_service, tmp_path):
        """清单里的 index 必须能直接拼进下载 URL（下载端点是 int 路径参数）。"""
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        tid = "task_art_idx"
        fresh_service._store.create_task(tid)
        pdf = tmp_path / "a-mono.pdf"
        pdf.write_bytes(b"%PDF-1.4 mono")
        fresh_service._store.update_task(
            tid,
            status="completed",
            result_files=[{"name": pdf.name, "path": str(pdf)}],
        )
        items = client.get(f"/api/tasks/{tid}/artifacts").json()
        assert items and isinstance(items[0]["index"], int)
        dl = client.get(f"/api/tasks/{tid}/artifacts/{items[0]['index']}")
        assert dl.status_code == 200
        assert dl.content.startswith(b"%PDF-")

    def test_artifact_error_does_not_leak_absolute_path(
        self, monkeypatch, fresh_service, tmp_path
    ):
        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )
        tid = "task_art_leak"
        fresh_service._store.create_task(tid)
        missing = tmp_path / "secret-dir" / "confidential-mono.pdf"
        fresh_service._store.update_task(
            tid,
            status="completed",
            result_files=[{"name": "confidential-mono.pdf", "path": str(missing)}],
        )
        resp = client.get(f"/api/tasks/{tid}/artifacts/0")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "confidential-mono.pdf" in detail
        assert str(tmp_path) not in detail

    def test_guard_errors_use_detail_key(self, monkeypatch, fresh_service):
        """鉴权/Host 守卫必须与业务错误同词（``detail``），否则前端拿不到文案。"""
        monkeypatch.setenv("PDF2ZH_API_TOKEN", "s3cret")
        client = TestClient(create_api_app(), base_url="http://127.0.0.1:11009")
        resp = client.get("/api/engines")
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body and body["detail"]
        assert "error" not in body
        # health 免鉴权
        assert client.get("/api/health").status_code == 200

    def test_non_loopback_host_is_forbidden_with_detail(self, fresh_service):
        client = TestClient(create_api_app(), base_url="http://example.com")
        resp = client.get("/api/health")
        assert resp.status_code == 403
        assert "detail" in resp.json()

    def test_module_docstring_lists_every_route(self):
        """模块 docstring 是开发者的端点索引，必须与实际路由同步。"""
        import re as _re

        from pdf2zh.services import api as api_mod

        source = (
            api_mod.__file__ and open(api_mod.__file__, encoding="utf-8").read()
        ) or ""
        doc = source.split('"""', 2)[1]
        registered = set(
            _re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"\)', source)
        )
        # 路径参数统一占位化后比对
        normalized = {p for p in registered}
        for path in sorted(normalized):
            needle = path
            assert needle in doc, f"route {needle} missing from module docstring"

    def test_public_api_doc_documents_every_route(self):
        """docs/APIS.md 必须覆盖全部对外端点（防止再次文档漂移）。

        历史上 docs/APIS.md 只描述已弃用的 Flask ``/v1`` 服务，32 个实际交付的
        ``/api/*`` 端点零记载；这里把它钉成 CI 可检的契约。
        """
        import re as _re
        from pathlib import Path

        from pdf2zh.services import api as api_mod

        source = (
            api_mod.__file__ and open(api_mod.__file__, encoding="utf-8").read()
        ) or ""
        routes = _re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"\)', source)

        doc_path = Path(__file__).resolve().parent.parent / "docs" / "APIS.md"
        assert doc_path.is_file(), "docs/APIS.md missing"
        doc = doc_path.read_text(encoding="utf-8")

        missing = sorted({r for r in routes if f"`{r}`" not in doc})
        assert not missing, f"routes missing from docs/APIS.md: {missing}"

        # 关键契约要点也必须在文档里
        for needle in (
            "multipart/form-data",
            "415",
            "413",
            "PDF2ZH_API_TOKEN",
            "PDF2ZH_ALLOWED_SOURCE_DIRS",
            "Last-Event-ID",
            "11009",
        ):
            assert needle in doc, f"docs/APIS.md does not mention {needle!r}"

    def test_public_api_doc_documents_submit_defaults(self):
        """提交参数表必须与代码签名一致（含默认值）。

        漂移成本最高的是"文档写了 4 线程、代码默认 8"这类静默不一致：调用方
        按文档调参会得到非预期行为。
        """
        import re as _re
        from pathlib import Path

        from pdf2zh.services import api as api_mod
        from pdf2zh.v3.ingestion import config as jcfg

        source = (
            api_mod.__file__ and open(api_mod.__file__, encoding="utf-8").read()
        ) or ""
        block = source.split("async def submit_task(", 1)[1].split(
            "    ) -> Dict[str, str]:", 1
        )[0]
        params = _re.findall(r"^\s{8}(\w+):\s*(.+?)\s*=\s*(.+?),\s*$", block, _re.M)
        assert len(params) >= 30, "submit signature parsing looks broken"

        doc = (Path(__file__).resolve().parent.parent / "docs" / "APIS.md").read_text(
            encoding="utf-8"
        )
        rows = {}
        for line in doc.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and cells[0].strip("`"):
                rows[cells[0].strip("`")] = " ".join(cells[1:])

        def default_of(expr: str) -> str:
            m = _re.search(r"default=(.*)\)\s*$", expr)
            raw = (m.group(1) if m else expr).strip().strip('"')
            if _re.fullmatch(r"[A-Z][A-Z0-9_]+", raw):
                raw = str(getattr(jcfg, raw, raw))
            return raw

        problems = []
        for name, type_expr, default_expr in params:
            if name == "request":
                continue
            cell = rows.get(name)
            if cell is None:
                problems.append(f"{name}: undocumented")
                continue
            if "File(" in type_expr + default_expr:
                if "upload" not in cell.lower():
                    problems.append(f"{name}: file field described as {cell!r}")
                continue
            cd = default_of(default_expr)
            if cd in ("True", "False"):
                if cd.lower() not in cell.lower():
                    problems.append(f"{name}: default {cd} not documented")
            elif cd not in cell:
                problems.append(f"{name}: default {cd!r} not in {cell!r}")
        assert not problems, "; ".join(problems)
