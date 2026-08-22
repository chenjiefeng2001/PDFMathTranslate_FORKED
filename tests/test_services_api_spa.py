"""Phase B：SPA 静态托管挂载测试（PDF2ZH_SPA_DIR）。"""

import os
from pathlib import Path

from fastapi.testclient import TestClient


def test_spa_mount_serves_index(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=root></div></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setenv("PDF2ZH_SPA_DIR", str(dist))
    # 延迟 import 确保读取到环境变量
    from pdf2zh.services.api import create_api_app

    client = TestClient(create_api_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "root" in resp.text
    # /api 路由不受 mount 影响
    assert client.get("/api/health").status_code == 200


def test_spa_mount_absent_keeps_pure_api(monkeypatch, tmp_path):
    monkeypatch.setenv("PDF2ZH_SPA_DIR", str(tmp_path / "nonexistent"))
    from pdf2zh.services.api import create_api_app

    client = TestClient(create_api_app())
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code in (404, 405)


def test_spa_env_unset_by_default(monkeypatch):
    monkeypatch.delenv("PDF2ZH_SPA_DIR", raising=False)
    from pdf2zh.services.api import create_api_app

    client = TestClient(create_api_app())
    assert client.get("/api/health").status_code == 200
