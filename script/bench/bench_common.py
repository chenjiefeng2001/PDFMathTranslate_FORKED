"""基准测试公共设施：进程托管、HTTP 计时、RSS 采样、合成 PDF、SSE 时间线。

驱动端跑在源码 Python 上，被测对象是 frozen onedir sidecar（软件本体的
分发形态），二者通过 REST/SSE 解耦——测得的是真实交付物而非开发态。
"""

from __future__ import annotations

import json
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE = (
    REPO_ROOT / "deploy/_build_sidecar/dist/pdf2zh-api-sidecar/pdf2zh-api-sidecar.exe"
)
SAMPLE_PDF = Path(__file__).parent / "_fixture_10p.pdf"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(
    method: str,
    url: str,
    *,
    timeout: float = 30.0,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, float]:
    """返回 (status, parsed-json-or-text, elapsed_seconds)。"""
    request = urllib.request.Request(url, method=method, data=body)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = resp.read()
            elapsed = time.perf_counter() - started
            try:
                return resp.status, json.loads(payload), elapsed
            except json.JSONDecodeError:
                return resp.status, payload.decode("utf-8", "replace"), elapsed
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - started
        try:
            return exc.code, json.loads(exc.read()), elapsed
        except Exception:  # noqa: BLE001 -- 基准里错误体格式不重要
            return exc.code, "", elapsed


def multipart_body(pdf_path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    crlf = "\r\n"
    parts: list[bytes] = []
    parts.append(
        (
            f"--{boundary}{crlf}"
            f'Content-Disposition: form-data; name="file"; filename="bench.pdf"{crlf}'
            f"Content-Type: application/pdf{crlf}{crlf}"
        ).encode()
    )
    parts.append(pdf_path.read_bytes())
    for key, value in fields.items():
        parts.append(
            (
                f"{crlf}--{boundary}{crlf}"
                f'Content-Disposition: form-data; name="{key}"{crlf}{crlf}'
                f"{value}"
            ).encode()
        )
    parts.append(f"{crlf}--{boundary}--{crlf}".encode())
    body = b"".join(parts)
    ctype = f"multipart/form-data; boundary={boundary}"
    return body, ctype


@dataclass
class SseFrame:
    ts: float  # perf_counter 相对起点秒
    event: str
    data: dict[str, Any]


def read_sse(
    url: str,
    *,
    timeout: float = 600.0,
    stop_event: threading.Event | None = None,
    on_frame: Callable[[SseFrame], bool] | None = None,
) -> list[SseFrame]:
    """读取 SSE 流直到连接关闭/on_frame 返回 False/超时。返回带相对时间戳的帧序列。"""
    frames: list[SseFrame] = []
    started = time.perf_counter()
    current_event = ""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url), timeout=timeout
        ) as resp:
            buffer = b""
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", "replace").rstrip("\r")
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                    elif line.startswith("data:") and current_event:
                        raw = line[len("data:") :].strip()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {"raw": raw}
                        frame = SseFrame(
                            ts=time.perf_counter() - started,
                            event=current_event,
                            data=data,
                        )
                        frames.append(frame)
                        keep = True if on_frame is None else on_frame(frame)
                        if not keep:
                            return frames
                        current_event = ""
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return frames


@dataclass
class SidecarHandle:
    process: subprocess.Popen
    port: int
    startup_s: float = 0.0  # Popen -> 首个 /api/health 200 的真实冷启动时长
    rss_stop: threading.Event = field(default_factory=threading.Event)
    rss_samples_mb: list[float] = field(default_factory=list)
    rss_parent_mb: list[float] = field(default_factory=list)
    rss_workers_mb: list[float] = field(default_factory=list)

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def peak_tree_rss_mb(self) -> float:
        return max(self.rss_samples_mb) if self.rss_samples_mb else 0.0


def launch_sidecar(
    exe: Path,
    *,
    port: int | None = None,
    startup_timeout: float = 120.0,
    sample_rss: bool = False,
) -> SidecarHandle:
    """启动 sidecar 并等待健康；可选后台采样进程树 RSS。"""
    port = port or free_port()
    spawned = time.perf_counter()
    process = subprocess.Popen(
        [str(exe), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = SidecarHandle(process=process, port=port)
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = http_json("GET", handle.base() + "/api/health", timeout=2.0)
            if status == 200:
                handle.startup_s = time.perf_counter() - spawned
                break
        except OSError:
            pass
        time.sleep(0.02)
    else:
        process.kill()
        raise RuntimeError(f"sidecar did not become healthy within {startup_timeout}s")

    if sample_rss:

        def _sample() -> None:
            import psutil

            parent = psutil.Process(process.pid)
            while not handle.rss_stop.is_set():
                try:
                    parent_rss = parent.memory_info().rss
                    workers_rss = 0
                    for child in parent.children(recursive=True):
                        workers_rss += child.memory_info().rss
                    handle.rss_parent_mb.append(parent_rss / 1048576.0)
                    handle.rss_workers_mb.append(workers_rss / 1048576.0)
                    handle.rss_samples_mb.append((parent_rss + workers_rss) / 1048576.0)
                except Exception:  # noqa: BLE001 -- 进程退出时采样失败忽略
                    return
                handle.rss_stop.wait(0.5)

        threading.Thread(target=_sample, daemon=True).start()
    return handle


def stop_sidecar(handle: SidecarHandle) -> None:
    handle.rss_stop.set()
    try:
        parent = __import__("psutil").Process(handle.process.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:  # noqa: BLE001
        handle.process.kill()


def pct(values: Iterable[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def median(values: Iterable[float]) -> float:
    return statistics.median(values)


def gen_pdf(path: Path, pages: int = 10, lines_per_page: int = 28) -> Path:
    """生成确定性多页文本 PDF 作为翻译负载。"""
    import pymupdf

    document = pymupdf.open()
    words = (
        "The quick brown fox jumps over the lazy dog while scientists study "
        "parallel translation pipelines and layout analysis models carefully."
    ).split()
    for page_index in range(pages):
        page = document.new_page(width=595, height=842)
        cursor_y = 72.0
        for line in range(lines_per_page):
            text = " ".join(
                words[(page_index * 7 + line * 3 + k) % len(words)] for k in range(14)
            )
            page.insert_text((60, cursor_y), text, fontsize=11, fontname="helv")
            cursor_y += 24
    document.save(str(path))
    document.close()
    return path


def ensure_fixture(pages: int = 10) -> Path:
    if SAMPLE_PDF.exists() and pages == 10:
        return SAMPLE_PDF
    return gen_pdf(SAMPLE_PDF, pages=pages)


def print_table(
    title: str, rows: list[tuple[str, ...]], headers: tuple[str, ...]
) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "-".join("-" * width for width in widths)
    print(f"\n== {title} ==")
    print(" | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print(line)
    for row in rows:
        print(" | ".join(str(c).ljust(w) for c, w in zip(row, widths)))


if __name__ == "__main__":
    fixture = ensure_fixture()
    print(f"fixture ready: {fixture} ({fixture.stat().st_size} bytes)")
    sys.exit(0)
