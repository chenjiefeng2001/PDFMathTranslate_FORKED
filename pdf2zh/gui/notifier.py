"""SSE event notifier: EventBus -> browser push, replacing the polling Timer.

Architecture (Stage 3 transport):

    Worker -> EventBus -> EventNotifier --SSE stream--> EventSource (browser)
                                                            |
                                                            v (wake signal)
                         hidden "sync-trigger" button -> drain_events (delta sync)

The browser keeps an EventSource open on ``/gui/events``. Every published
domain event is fanned out to all connected subscribers as a **full JSON
payload** (``task_id`` + ``seq`` + ``event_type`` + event fields), each frame
carrying an SSE ``id:`` cursor. The browser reacts by waking the hidden
Gradio sync dependency once, which pulls only the NEW events from the bus
(rendering happens server-side; the SSE payload removes the reliance on the
bus cursor for wake correctness and enables a future client-side renderer).

Reconnect/robustness (Phase 0): per the SSE spec, the browser sends the
``Last-Event-ID`` header on every (re)connection. When present, ``sse_stream``
first replays every event published since that cursor from ``EventBus``
(``events_after``) before switching to the live stream -- so a dropped
connection loses nothing and never needs a client-side poll to catch up.

Thread safety: EventBus subscriber callbacks run on worker threads while the
SSE generators run on the asyncio event loop, so delivery uses
``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional, Tuple

from starlette.requests import Request

from pdf2zh.gui.events import EVENT_BUS, TaskEvent

logger = logging.getLogger(__name__)

#: Idle seconds between SSE keep-alive comments (also the reconnect hint).
_HEARTBEAT_SECONDS = 25

#: SSE media type; "text/event-stream" is the standard.
_EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

#: Client reconnect delay advertised by the stream.
_RETRY_MILLISECONDS = 1000


def _format_frame(event: TaskEvent) -> str:
    """Render a publish-ready SSE frame carrying the full event payload."""
    payload = event.to_dict()
    payload["seq"] = event.sequence
    return (
        f"id: {event.sequence}\n" f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _replay_frames(events: List[TaskEvent]) -> List[str]:
    """Build ordered SSE frames for a list of events (already seq-sorted)."""
    return [_format_frame(ev) for ev in events]


class EventNotifier:
    """Fan out EventBus publishes to connected SSE subscribers.

    Subscribers connect via ``connect()`` (called from an asyncio context),
    receive JSON stubs on their queue, and disconnect via ``disconnect()``.
    """

    def __init__(self, bus=EVENT_BUS) -> None:
        self._bus = bus
        #: List of (event_loop, delivery_queue) per open connection.
        self._subscribers: List[
            Tuple[Optional[asyncio.AbstractEventLoop], asyncio.Queue]
        ] = []
        self._sub_id: Optional[int] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the EventBus once (idempotent)."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe(self._broadcast)
            logger.debug("EventNotifier subscribed to EventBus")

    def stop(self) -> None:
        """Unsubscribe from the EventBus and drop all connections."""
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None
        self._subscribers.clear()

    # ── subscriber registry ─────────────────────────────────────────────────

    def connect(self) -> asyncio.Queue:
        """Register a new SSE subscriber; returns its delivery queue."""
        queue: asyncio.Queue = asyncio.Queue()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._subscribers.append((loop, queue))
        return queue

    def disconnect(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue (safe to call from any context)."""
        self._subscribers = [
            (loop, q) for loop, q in self._subscribers if q is not queue
        ]

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ── EventBus callback (worker thread) ───────────────────────────────────

    def _broadcast(self, event: Any) -> None:
        """Deliver a full JSON frame of ``event`` to every open connection."""
        frame = _format_frame(event)
        for loop, queue in list(self._subscribers):
            if loop is not None:
                loop.call_soon_threadsafe(queue.put_nowait, frame)
            else:
                queue.put_nowait(frame)

    # ── SSE endpoint ────────────────────────────────────────────────────────

    async def sse_stream(self, request: Request):
        """Starlette/FastAPI endpoint: keep-alive Server-Sent Events stream.

        The stream stays open while the client is connected; idle connections
        receive ``: keep-alive`` comments. Payloads are full JSON events that
        the browser uses as the wake signal for one delta sync (rendering is
        server-side, see ``app.drain_events``).

        Reconnect/robustness: ``EventSource`` automatically sends the browser's
        ``Last-Event-ID`` header on every (re)connection. When present, the
        stream first replays every event published after that cursor from
        ``EventBus.events_after``, then switches to the live stream -- a
        dropped connection loses nothing and never needs client-side polling.
        """
        from starlette.responses import StreamingResponse

        queue = self.connect()

        last_event_id = 0
        if request is not None:
            header = request.headers.get("last-event-id") or ""
            try:
                last_event_id = int(header.strip())
            except (TypeError, ValueError):
                last_event_id = 0
        if last_event_id > 0:
            missed = self._bus.events_after(last_event_id)
            for frame in _replay_frames(missed):
                queue.put_nowait(frame)
            if missed:
                logger.debug(
                    "SSE replay after Last-Event-ID=%s: %d missed event(s)",
                    last_event_id,
                    len(missed),
                )

        async def gen():
            try:
                yield f"retry: {_RETRY_MILLISECONDS}\n\n"
                while True:
                    try:
                        frame = await asyncio.wait_for(
                            queue.get(), timeout=_HEARTBEAT_SECONDS
                        )
                        yield frame
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                self.disconnect(queue)

        return StreamingResponse(
            gen(),
            media_type=_EVENT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


#: Application-wide singleton notifier (mirrors EVENT_BRIDGE).
EVENT_NOTIFIER = EventNotifier()


__all__ = ["EventNotifier", "EVENT_NOTIFIER"]
