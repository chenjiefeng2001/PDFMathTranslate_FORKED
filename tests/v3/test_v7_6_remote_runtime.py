"""V7.6 Out-of-Process Runtime Service — REST 适配层.

Covers the V7.6 iteration (see doc/v7_operator_runtime_report.md §六):

  - RuntimeTransport: the shared wire contract for every adapter.
  - RuntimeRestServer: RuntimeService behind a threaded stdlib HTTP server
    (daemon thread, ephemeral port support, context-manager lifecycle).
  - RuntimeRestClient: blocking JSON client implementing the same lifecycle
    verbs as RuntimeService (open / execute / status / translations /
    snapshot / rollback / close / stats / health).
  - error semantics: 404 unknown session, 400 invalid body, 500 handler
    failures, unreachable server.

Run with:
    python -m pytest tests/v3/test_v7_6_remote_runtime.py -v
"""
from __future__ import annotations

import urllib.request

import pytest

from pdf2zh.v3.remote_runtime import (
    RuntimeRemoteError,
    RuntimeRestClient,
    RuntimeRestServer,
    RuntimeTransport,
)
from pdf2zh.v3.runtime_service import RuntimeService

BLOCKS = [
    {"id": "n0", "text": "Transformer models work well.",
     "type": "paragraph", "page": 0},
]


@pytest.fixture()
def server_and_client(tmp_path):
    service = RuntimeService(persistence_dir=str(tmp_path))
    server = RuntimeRestServer(service)
    server.start()
    try:
        client = RuntimeRestClient(server.url)
        yield service, client
    finally:
        server.stop()


# ── Transport protocol ───────────────────────────────────────────────

class TestRuntimeTransport:
    def test_protocol_verbs_raise_not_implemented(self):
        transport = RuntimeTransport()
        for verb, args in [
            ("open_session", ({},)),
            ("execute", ("s",)),
            ("status", ("s",)),
            ("translations", ("s",)),
            ("snapshot", ("s",)),
            ("rollback", ("s",)),
            ("close", ("s",)),
            ("stats", ()),
            ("health", ()),
        ]:
            with pytest.raises(NotImplementedError):
                getattr(transport, verb)(*args)


# ── Server + client lifecycle ────────────────────────────────────────

class TestRemoteLifecycle:
    def test_health_and_stats(self, server_and_client):
        _, client = server_and_client
        health = client.health()
        assert health["status"] == "ok"
        assert "pdf2zh" in health["service"]
        stats = client.stats()
        assert "sessions" in stats and "cache" in stats

    def test_open_execute_status_translations(self, server_and_client):
        service, client = server_and_client
        opened = client.open_session(BLOCKS, document_id="paper-42")
        session_id = opened["session_id"]
        assert opened["state"] == "ready"
        assert opened["document_id"] == "paper-42"
        # server side got the session
        assert service.sessions.get(session_id) is not None

        executed = client.execute(session_id)
        assert executed["stats"]["translated"] == 1
        assert sorted(executed["formats"]) == ["html", "pdf", "text"]

        status = client.status(session_id)
        assert status["state"] == "completed"
        assert status["translated"] == 1
        assert "formats" in status

        translations = client.translations(session_id)
        assert "n0" in translations["translations"]

    def test_incremental_execute_verb(self, server_and_client):
        service, client = server_and_client
        session_id = client.open_session(BLOCKS)["session_id"]
        client.execute(session_id)
        result = client.execute_incremental(session_id, ["n0"])
        assert result["stats"]["translated"] == 1

    def test_snapshot_rollback_flow(self, server_and_client):
        service, client = server_and_client
        session_id = client.open_session(BLOCKS)["session_id"]
        client.execute(session_id)
        snap = client.snapshot(session_id, label="milestone-1")
        assert snap["label"] == "milestone-1"
        assert snap["snapshot_id"]
        rollback = client.rollback(session_id, label="milestone-1")
        assert rollback["rolled_back_to"] == "milestone-1"

    def test_rollback_without_label_falls_back_to_latest(self,
                                                         server_and_client):
        service, client = server_and_client
        session_id = client.open_session(BLOCKS)["session_id"]
        client.execute(session_id)
        client.snapshot(session_id, label="v1")
        client.execute_incremental(session_id, ["n0"])
        result = client.rollback(session_id)
        assert result["rolled_back_to"] == "v1"

    def test_close_removes_session(self, server_and_client):
        service, client = server_and_client
        session_id = client.open_session(BLOCKS)["session_id"]
        closed = client.close(session_id)
        assert closed["closed"] is True
        assert service.sessions.has(session_id) is False


# ── Error semantics ──────────────────────────────────────────────────

class TestRemoteErrors:
    def test_unknown_session_is_404(self, server_and_client):
        _, client = server_and_client
        with pytest.raises(RuntimeRemoteError) as exc:
            client.status("does-not-exist")
        assert "404" in str(exc.value)

    def test_invalid_json_body_is_400(self, server_and_client):
        service, client = server_and_client
        session_id = client.open_session(BLOCKS)["session_id"]
        from urllib.error import HTTPError

        request = urllib.request.Request(
            f"{client.base_url}/v1/sessions/{session_id}/execute",
            data=b"{broken json", method="POST",
            headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as exc:
            with urllib.request.urlopen(request) as _:
                pass
        assert exc.value.code == 400

    def test_missing_document_is_400(self, server_and_client):
        _, client = server_and_client
        with pytest.raises(RuntimeRemoteError) as exc:
            client._request("POST", "/v1/sessions", {})
        assert "400" in str(exc.value)

    def test_unreachable_server(self):
        client = RuntimeRestClient("http://127.0.0.1:1", timeout=2)
        with pytest.raises(RuntimeRemoteError):
            client.health()

    def test_unknown_route_is_404(self, server_and_client):
        _, client = server_and_client
        with pytest.raises(RuntimeRemoteError) as exc:
            client._request("GET", "/nope")
        assert "404" in str(exc.value)


# ── Server lifecycle ─────────────────────────────────────────────────

class TestServerLifecycle:
    def test_context_manager_and_ephemeral_port(self, tmp_path):
        service = RuntimeService(persistence_dir=str(tmp_path))
        with RuntimeRestServer(service) as server:
            assert server.port != 0
            client = RuntimeRestClient(server.url)
            assert client.health()["status"] == "ok"
        # server stopped after context exit
        with pytest.raises(RuntimeRemoteError):
            RuntimeRestClient(server.url, timeout=2).health()

    def test_double_start_is_idempotent(self, tmp_path):
        service = RuntimeService(persistence_dir=str(tmp_path))
        server = RuntimeRestServer(service)
        server.start()
        port = server.port
        server.start()  # no-op
        assert server.port == port
        server.stop()

    def test_server_exposes_custom_runtime(self, tmp_path):
        custom_service = RuntimeService(persistence_dir=str(tmp_path))
        server = RuntimeRestServer(custom_service, port=0)
        server.start()
        try:
            client = RuntimeRestClient(server.url)
            sid = client.open_session(BLOCKS)["session_id"]
            assert custom_service.sessions.has(sid) is True
        finally:
            server.stop()
