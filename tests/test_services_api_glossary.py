"""API 词表能力测试：任务上传透传 + 词表库管理端点。

submit 的词表字段用假 submit_task 捕获；库端点把 store_dir 指到 tmp_path，
绝不触碰真实 ~/.config。
"""

from __future__ import annotations

import csv
import json

import pytest
from fastapi.testclient import TestClient

from pdf2zh import glossary_store as gs
from pdf2zh.services.api import create_api_app
from pdf2zh.services.runtime_service import RuntimeService


@pytest.fixture()
def service():
    return RuntimeService()


@pytest.fixture()
def client(service):
    return TestClient(
        create_api_app(service=service), base_url="http://127.0.0.1:11009"
    )


@pytest.fixture()
def store(tmp_path, monkeypatch):
    d = tmp_path / "store"
    d.mkdir()
    monkeypatch.setattr(gs, "store_dir", lambda: d)
    return d


def _csv_bytes(rows=(("kernel", "内核", ""),)):
    import io

    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["source", "target", "tgt_lng"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


class TestSubmitGlossaryFields:
    def test_multipart_glossaries_upload(self, client, monkeypatch, tmp_path):
        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_g"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        resp = client.post(
            "/api/tasks",
            data={"source_path": "/tmp/a.pdf"},
            files=[
                ("glossaries", ("terms.csv", _csv_bytes(), "text/csv")),
                ("glossaries", ("more.csv", _csv_bytes(), "text/csv")),
            ],
        )
        assert resp.status_code == 200
        gloss = captured["request"].glossary_files
        assert len(gloss) == 2
        assert all(g.endswith(".csv") for g in gloss)

    def test_server_side_paths_form_field(self, client, monkeypatch, tmp_path):
        g = str(tmp_path / "g.csv")
        with open(g, "wb") as fh:
            fh.write(_csv_bytes())
        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_g2"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        resp = client.post(
            "/api/tasks",
            data={"source_path": "/tmp/a.pdf", "glossary_files": json.dumps([g])},
        )
        assert resp.status_code == 200
        assert captured["request"].glossary_files == [g]

    def test_store_name_resolution_and_missing_rejection(
        self,
        client,
        monkeypatch,
        store,
    ):
        (store / "mine.csv").write_bytes(_csv_bytes())
        captured = {}

        def fake_submit(self, request):
            captured["request"] = request
            return "task_g3"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        ok = client.post(
            "/api/tasks",
            data={"source_path": "/tmp/a.pdf", "glossary_files": "mine"},
        )
        assert ok.status_code == 200
        assert captured["request"].glossary_files == [str(store / "mine.csv")]

        bad = client.post(
            "/api/tasks",
            data={"source_path": "/tmp/a.pdf", "glossary_files": "ghost"},
        )
        assert bad.status_code == 400


class TestGlossaryStoreEndpoints:
    def test_list_empty(self, client, store):
        resp = client.get("/api/glossaries")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_import_validate_download_round_trip(self, client, store):
        bad = client.post(
            "/api/glossaries",
            files={"file": ("bad.csv", b"wrong,header\n", "text/csv")},
        )
        assert bad.status_code == 400

        good = client.post(
            "/api/glossaries",
            data={"name": "my terms"},
            files={"file": ("whatever.csv", _csv_bytes(), "text/csv")},
        )
        assert good.status_code == 200
        body = good.json()
        assert body["name"] == "my_terms"
        assert body["entries"] == 1

        listed = client.get("/api/glossaries").json()
        assert [i["name"] for i in listed] == ["my_terms"]

        downloaded = client.get("/api/glossaries/my_terms/download")
        assert downloaded.status_code == 200
        rows = list(csv.DictReader(downloaded.text.lstrip("\ufeff").splitlines()))
        assert rows[0]["source"] == "kernel"

    def test_download_unknown_404(self, client, store):
        resp = client.get("/api/glossaries/ghost/download")
        assert resp.status_code == 404
