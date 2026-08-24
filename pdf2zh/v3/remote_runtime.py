"""Module: V7.6 Out-of-Process Runtime Service — REST 适配层.

Iteration feedback (doc/v7_operator_runtime_report.md §六): the runtime
should be exposed outside the process so other services / frontends can
drive document intelligence over a network contract. This module provides a
zero-dependency transport adapter:

    RuntimeService ── RuntimeRestServer (http.server) ── RuntimeRestClient

- ``RuntimeTransport`` — the protocol every adapter implements (a future gRPC
  adapter can satisfy the same interface without touching the service).
- ``RuntimeRestServer`` — wraps a ``RuntimeService`` behind a threaded HTTP
  server (stdlib ``http.server``, no new dependencies).
- ``RuntimeRestClient`` — blocking JSON client (stdlib ``urllib``) with the
  same lifecycle verbs as ``RuntimeService`` (open / execute / status /
  translations / snapshot / rollback / close / stats / health).

Routes::

    GET  /health
    GET  /v1/stats
    POST /v1/sessions                          {"document": [...], ...}
    POST /v1/sessions/{id}/execute             {"changed_ids": [...]?}
    POST /v1/sessions/{id}/execute_incremental {"changed_ids": [...]}
    GET  /v1/sessions/{id}/status
    GET  /v1/sessions/{id}/translations
    POST /v1/sessions/{id}/snapshot            {"label": "..."?}
    POST /v1/sessions/{id}/rollback            {"label": "..."?}
    POST /v1/sessions/{id}/close

Usage::

    service = RuntimeService()
    with RuntimeRestServer(service) as server:     # daemon thread on :0
        client = RuntimeRestClient(server.url)
        session_id = client.open_session(blocks)["session_id"]
        print(client.execute(session_id))
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ── 入站防护 ─────────────────────────────────────────────────────────
#: 请求体上限（默认 64MB，``PDF2ZH_RUNTIME_MAX_BODY_MB`` 可调）。
#: ``_read_body`` 此前按 Content-Length 无界读入内存；loopback-only
#: 默认缓解了大半，但 host 参数一旦外开即成内存 DoS 点。
def _max_body_bytes() -> int:
    raw = os.environ.get("PDF2ZH_RUNTIME_MAX_BODY_MB") or ""
    try:
        mb = int(raw.strip()) if raw.strip() else 64
    except ValueError:
        mb = 64
    return max(1, mb) << 20


class _BodyTooLarge(ValueError):
    """Content-Length 超出上限（映射为 HTTP 413）。"""


#: 拒绝超限请求前最多排空的字节数：先消费掉已声明的请求体再回 413，
#: 客户端才能收到状态码而不是连接重置；声明量离谱时放弃排空直接断开。
_DRAIN_LIMIT = 16 << 20


#: 同时处理的请求上限：ThreadingHTTPServer 每连接一个线程且无内建
#: 上限，用信号量封顶，超出的请求排队而非无限增殖线程。
_HANDLER_SLOTS = threading.BoundedSemaphore(32)


class RuntimeRemoteError(RuntimeError):
    """Raised when the remote runtime returns a non-2xx / unreachable state."""


def _jsonable(obj: Any) -> Any:
    from pdf2zh.v3.operators import _as_jsonable
    return _as_jsonable(obj)


class RuntimeTransport:
    """The wire contract shared by every out-of-process adapter.

    Subclasses implement each verb; ``RuntimeRestClient`` is the HTTP one, a
    future gRPC adapter would implement the same interface.
    """

    def open_session(self, document: Any, document_id: Optional[str] = None,
                     target_lang: str = "zh-CN") -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self, session_id: str,
                changed_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def status(self, session_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def translations(self, session_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def snapshot(self, session_id: str,
                 label: str = "snapshot") -> Dict[str, Any]:
        raise NotImplementedError

    def rollback(self, session_id: str,
                 label: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def close(self, session_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def stats(self) -> Dict[str, Any]:
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError


# ── HTTP server ──────────────────────────────────────────────────────

_ROUTES = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("GET", re.compile(r"^/v1/stats$"), "stats"),
    ("POST", re.compile(r"^/v1/sessions$"), "open"),
    ("POST",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/execute$"),
     "execute"),
    ("POST",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/execute_incremental$"),
     "execute_incremental"),
    ("GET",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/status$"), "status"),
    ("GET",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/translations$"),
     "translations"),
    ("POST",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/snapshot$"),
     "snapshot"),
    ("POST",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/rollback$"),
     "rollback"),
    ("POST",
     re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/close$"), "close"),
]


def _find_snapshot_by_label(service: Any, session_id: str,
                            label: str) -> Any:
    for snap in service.list_snapshots(session_id):
        if getattr(snap, "label", "") == label:
            return snap
    raise ValueError(f"No snapshot labeled '{label}' for session "
                     f"'{session_id}'")


class _RuntimeRequestHandler(BaseHTTPRequestHandler):
    """Threaded JSON handler dispatching to the wrapped RuntimeService."""

    server: "_RuntimeHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        return

    # ── plumbing ─────────────────────────────────────────────────────

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._respond(status, {"error": str(message), "status": status})

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _max_body_bytes():
            remaining = min(length, _DRAIN_LIMIT)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if remaining:
                # 客户端声明的体量远大于实际发送：连接已不可靠
                self.close_connection = True
            raise _BodyTooLarge(
                f"body of {length} bytes exceeds limit "
                f"({_max_body_bytes()} bytes)"
            )
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        return data if isinstance(data, dict) else {"document": data}

    # ── routing ──────────────────────────────────────────────────────

    def _dispatch(self) -> None:
        with _HANDLER_SLOTS:
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        parsed = urlparse(self.path)
        for method, pattern, action in _ROUTES:
            if self.command != method:
                continue
            match = pattern.match(parsed.path)
            if match is None:
                continue
            try:
                body = self._read_body()
            except _BodyTooLarge as exc:
                self._error(413, exc)
                return
            except ValueError as exc:
                self._error(400, exc)
                return
            try:
                payload = self.server._handle(action, match.groupdict(), body)
            except KeyError as exc:
                self._error(404, f"Unknown session: {exc}")
                return
            except ValueError as exc:
                self._error(400, exc)
                return
            except Exception as exc:  # noqa: BLE001 - surface via HTTP
                logger.exception("REST handler '%s' failed", action)
                self._error(500, exc)
                return
            self._respond(200, payload)
            return
        self._error(404, f"No route for {self.command} {parsed.path}")

    do_GET = _dispatch
    do_POST = _dispatch



class _RuntimeHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the wrapped RuntimeService."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Any, handler: Any, service: Any) -> None:
        self.service = service
        super().__init__(addr, handler)

    def _handle(self, action: str, groups: Dict[str, str],
                body: Dict[str, Any]) -> Dict[str, Any]:
        service = self.service
        if action == "health":
            return {"status": "ok", "service": "pdf2zh-dir-runtime"}
        if action == "stats":
            return _jsonable(service.stats())
        if action == "open":
            document = body.get("document")
            if document is None:
                raise ValueError("'document' is required to open a session")
            session = service.open(
                document,
                document_id=body.get("document_id"),
                target_lang=body.get("target_lang", "zh-CN"))
            return {"session_id": session.session_id,
                    "document_id": body.get("document_id"),
                    "state": session.state.value}
        session_id = groups["session_id"]
        if action == "execute":
            changed = body.get("changed_ids")
            output = service.execute(session_id, changed_ids=changed)
            return _jsonable(output)
        if action == "execute_incremental":
            changed = body.get("changed_ids") or []
            if not changed:
                raise ValueError("'changed_ids' is required for incremental")
            output = service.execute(session_id, changed_ids=list(changed))
            return _jsonable(output)
        if action == "status":
            return _jsonable(service.status(session_id))
        if action == "translations":
            session = service.sessions.get(session_id)
            return {"session_id": session_id,
                    "translations": dict(getattr(session, "translations", {}))}
        if action == "snapshot":
            snap = service.snapshot(session_id,
                                    label=body.get("label", "snapshot"))
            return {"session_id": session_id,
                    "snapshot_id": snap.snapshot_id,
                    "label": snap.label}
        if action == "rollback":
            label = body.get("label")
            snap = _find_snapshot_by_label(service, session_id, label) \
                if label else None
            target = service.rollback(session_id, snapshot=snap)
            return {"session_id": session_id,
                    "rolled_back_to": getattr(target, "label", "")}
        if action == "close":
            closed = service.close(session_id)
            return {"session_id": session_id, "closed": bool(closed)}
        raise ValueError(f"Unhandled action: {action}")


class RuntimeRestServer:
    """Expose a ``RuntimeService`` over HTTP inside this process.

    ``start()`` launches a daemon thread (blocking-free); ``stop()`` shuts
    it down. ``port=0`` binds an ephemeral free port (read ``.port`` after
    start). Also usable as a context manager.
    """

    def __init__(self, runtime_service: Any, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        self.service = runtime_service
        self.host = host
        self.port = port
        self._httpd: Optional[_RuntimeHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "RuntimeRestServer":
        if self._httpd is not None:
            return self
        self._httpd = _RuntimeHTTPServer(
            (self.host, self.port), _RuntimeRequestHandler, self.service)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="pdf2zh-rest-runtime", daemon=True)
        self._thread.start()
        logger.info("Runtime REST server listening on %s", self.url)
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None

    def __enter__(self) -> "RuntimeRestServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()



# ── HTTP client ──────────────────────────────────────────────────────

class RuntimeRestClient(RuntimeTransport):
    """Blocking JSON-REST client for ``RuntimeRestServer`` (stdlib only)."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode("utf-8") \
            if payload is not None else None
        request = Request(
            self.base_url + path, data=body, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                message = parsed.get("error", detail)
            except json.JSONDecodeError:
                message = detail or exc.reason
            raise RuntimeRemoteError(
                f"{method} {path} → {exc.code}: {message}") from exc
        except URLError as exc:
            raise RuntimeRemoteError(
                f"{method} {path} unreachable: {exc.reason}") from exc
        return json.loads(raw) if raw.strip() else {}

    # ── RuntimeTransport verbs ───────────────────────────────────────

    def open_session(self, document: Any, document_id: Optional[str] = None,
                     target_lang: str = "zh-CN") -> Dict[str, Any]:
        return self._request("POST", "/v1/sessions", {
            "document": _jsonable(document),
            "document_id": document_id,
            "target_lang": target_lang,
        })

    def execute(self, session_id: str,
                changed_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        payload = {"changed_ids": list(changed_ids)} \
            if changed_ids is not None else None
        return self._request("POST", f"/v1/sessions/{session_id}/execute",
                             payload)

    def execute_incremental(self, session_id: str,
                            changed_ids: List[str]) -> Dict[str, Any]:
        return self._request(
            "POST", f"/v1/sessions/{session_id}/execute_incremental",
            {"changed_ids": list(changed_ids)})

    def status(self, session_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{session_id}/status")

    def translations(self, session_id: str) -> Dict[str, Any]:
        return self._request("GET",
                             f"/v1/sessions/{session_id}/translations")

    def snapshot(self, session_id: str,
                 label: str = "snapshot") -> Dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{session_id}/snapshot",
                             {"label": label})

    def rollback(self, session_id: str,
                 label: Optional[str] = None) -> Dict[str, Any]:
        payload = {"label": label} if label else {}
        return self._request("POST", f"/v1/sessions/{session_id}/rollback",
                             payload)

    def close(self, session_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{session_id}/close")

    def stats(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/stats")

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")


__all__ = [
    "RuntimeTransport", "RuntimeRemoteError", "RuntimeRestServer",
    "RuntimeRestClient",
]

