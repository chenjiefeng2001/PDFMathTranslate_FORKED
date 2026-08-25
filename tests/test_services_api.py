"""Phase A REST/SSE API 层单元测试（pdf2zh.services.api）。不触发真实翻译：submit_task 被 monkeypatch 记录请求并返回假任务号；SSE 用「空 source_path → 立即 FAILED」的真实服务路径验证初始帧与终态帧。"""import json


import pytest

from fastapi.testclient import TestClient

from pathlib import Path


from pdf2zh.services.api import create_api_appfrom pdf2zh.services.runtime_service import RuntimeServicefrom pdf2zh.services.runtime_singleton import (    get_runtime_service,    reset_runtime_service,)@pytest.fixture()def fresh_service():    reset_runtime_service()    yield RuntimeService()    reset_runtime_service()def _client(monkeypatch, service=None):    if service is not None:        monkeypatch.setattr(            type(service),            "submit_task",            lambda self, request: (                getattr(self, "_fake_id", None)                or setattr(self, "_fake_id", "task_fake")                or self._fake_id            ),        )    return TestClient(        create_api_app(service=service), base_url="http://127.0.0.1:11009"    )class TestHealthAndEngines:    def test_health(self, monkeypatch):        client = _client(monkeypatch)        resp = client.get("/api/health")        assert resp.status_code == 200        body = resp.json()        assert body["status"] == "ok"        assert isinstance(body["tasks"], int)    def test_engines_registry_and_masking(self, monkeypatch):        client = _client(monkeypatch)        resp = client.get("/api/engines")        assert resp.status_code == 200        engines = resp.json()        names = [e["name"] for e in engines]        # 注册表覆盖：传统 + LLM + opencode        for expected in ("google", "openai", "deepl", "opencode"):            assert expected in names        opencode = next(e for e in engines if e["name"] == "opencode")        keys = {e["key"] for e in opencode["envs"]}        assert {"OPENCODE_PATH", "OPENCODE_MODEL", "OPENCODE_SERVER_URL"} <= keys        # 关键安全约束：envs 只回显 configured 布尔，绝不回显值        for engine in engines:            for item in engine["envs"]:                assert set(item.keys()) == {"key", "configured"}class TestSubmit:    def test_submit_maps_form_fields(self, monkeypatch, fresh_service):        captured = {}        def fake_submit(self, request):            captured["request"] = request            return "task_abc"        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)        client = TestClient(            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"        )        resp = client.post(            "/api/tasks",            data={                "source_path": "/tmp/a.pdf",                "target_lang": "ja",                "engine": "opencode:opencode/gpt-5",                "threads": 99,                "page_range": "1-3",                "mode_choice": "quality",            },        )        assert resp.status_code == 200        assert resp.json() == {"task_id": "task_abc"}        req = captured["request"]        assert req.target_lang == "ja"        assert req.engine == "opencode:opencode/gpt-5"        assert req.threads == 32  # 上限钳制        assert req.page_range == "1-3"        assert req.extra_config.get("mode_choice") == "quality"    def test_submit_upload_writes_temp_file(self, monkeypatch, tmp_path, fresh_service):        captured = {}        def fake_submit(self, request):            captured["request"] = request            return "task_up"        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)        client = TestClient(            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"        )        resp = client.post(            "/api/tasks",            data={"target_lang": "zh"},            files={"file": ("doc.pdf", b"%PDF-1.4 fake")},        )        assert resp.status_code == 200        from pathlib import Path        src = Path(captured["request"].source_path)        assert src.exists() and src.read_bytes().startswith(b"%PDF")    def test_submit_rejects_bad_extra_config(self, monkeypatch, fresh_service):

        monkeypatch.setattr(RuntimeService, "submit_task", lambda self, r: "task_x")

        client = TestClient(
            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"
        )

        resp = client.post("/api/tasks", data={"extra_config": "{bad json"})

        assert resp.status_code == 400


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

        names = [Path(p).name.split("_", 1)[1] for p in req.files]

        assert names == ["a.pdf", "b.pdf", "c.docx"]

        # 每个上传文件均已落盘且内容一致；source_path 回落为第一个文件
        for path, payload in zip(req.files, [b"%PDF-1.4 aaa", b"%PDF-1.4 bbb", b"PK docx"]):
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

        assert _zipfile.ZipFile(_io.BytesIO(resp.content)).namelist() == ["out-mono.pdf"]

    def test_legacy_single_file_still_accepted(
        self, monkeypatch, fresh_service
    ):

        client, captured = self._client_with_capture(monkeypatch, fresh_service)

        resp = client.post(
            "/api/tasks",
            data={},
            files={"file": ("legacy.pdf", b"%PDF-1.4 legacy")},
        )

        assert resp.status_code == 200

        req = captured["request"]

        assert len(req.files) == 1

        assert Path(req.files[0]).exists()class TestTaskLifecycle:    @pytest.fixture()    def failed_task_client(self, fresh_service):        """空 source_path → submit_task 直接落 FAILED（无文件快速失败路径）。"""        return (            TestClient(                create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"            ),            fresh_service,        )    def test_unknown_task_404(self, failed_task_client):        client, _ = failed_task_client        assert client.get("/api/tasks/nope").status_code == 404        assert client.delete("/api/tasks/nope").status_code == 404        assert client.get("/api/tasks/nope/artifacts").status_code == 404    def test_empty_source_fails_fast_then_visible(self, failed_task_client):        client, svc = failed_task_client        resp = client.post("/api/tasks", data={})        assert resp.status_code == 200        tid = resp.json()["task_id"]        state = client.get(f"/api/tasks/{tid}").json()        assert state["status"] in ("failed", "pending")        listed = {t["task_id"] for t in client.get("/api/tasks").json()}        assert tid in listedclass TestSseStream:    def test_initial_state_and_done_frames(self, fresh_service):        client = TestClient(            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"        )        tid = client.post("/api/tasks", data={}).json()["task_id"]        frames = []        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:            assert resp.headers["content-type"].startswith("text/event-stream")            for line in resp.iter_lines():                frames.append(line)                if any(l.startswith("event: done") for l in frames):                    break        joined = "\n".join(frames)        assert "event: state" in joined        assert "event: done" in joined        state_payload = json.loads(joined.split("data: ", 1)[1].split("\n")[0])        assert state_payload["task_id"] == tid    def test_last_event_id_replay(self, fresh_service):        """断线续传：Last-Event-ID=1 时只重放第 2 条事件，且帧带 id 行。"""        import time as _time        from pdf2zh.services.runtime_service import TaskProgressEvent        client = TestClient(            create_api_app(service=fresh_service), base_url="http://127.0.0.1:11009"        )        tid = client.post("/api/tasks", data={}).json()["task_id"]        fresh_service._store.add_event(            tid,            TaskProgressEvent(                task_id=tid,                stage="parsing",                progress=10.0,                message="first",                timestamp=_time.time(),            ),        )        fresh_service._store.add_event(            tid,            TaskProgressEvent(                task_id=tid,                stage="translating",                progress=50.0,                message="second",                timestamp=_time.time(),            ),        )        frames = []        with client.stream(            "GET",            f"/api/tasks/{tid}/events",            headers={"Last-Event-ID": "1"},        ) as resp:            for line in resp.iter_lines():                frames.append(line)                if any(l.startswith("event: done") for l in frames):                    break        joined = "\n".join(frames)        assert "id: 2" in joined, joined        assert '"seq": 2' in joined        assert "first" not in joined  # 已消费的第 1 条不重放        assert "second" in joinedclass TestSharedSingleton:    def test_singleton_identity(self):        assert get_runtime_service() is get_runtime_service()