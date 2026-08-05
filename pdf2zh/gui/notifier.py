"""SSE event notifier: EventBus -> browser push, replacing the polling Timer.

Architecture (Stage 3 transport):

    Worker -> EventBus -> EventNotifier --SSE stream--> EventSource (browser)
                                                |
                                                v (wake signal)
                     hidden "sync-trigger" button -> drain_events (delta sync)

The browser keeps an EventSource open on ``/gui/events``. Every published
domain event is fanned out to all connected subscribers as a tiny JSON stub
(``task_id`` + ``sequence``); the browser reacts by waking the Gradio
sync dependency once, which pulls only the NEW events from the bus. No
backend polling happens: the Gradio Timer transport is gone.

Thread safety: EventBus subscriber callbacks run on worker threads while the
SSE generators run on the asyncio event loop, so delivery uses
``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional, Tuple

from pdf2zh.gui.events import EVENT_BUS

logger = logging.getLogger(__name__)

#: Idle seconds between SSE keep-alive comments (also the reconnect hint).
_HEARTBEAT_SECONDS = 25

#: SSE media type; "text/event-stream" is the standard.
_EVENT_STREAM_MEDIA_TYPE = "text/event-stream"


class EventNotifier:
    """Fan out EventBus publishes to connected SSE subscribers.

    Subscribers connect via ``connect()`` (called from an asyncio context),
    receive JSON stubs on their queue, and disconnect via ``disconnect()``.
    """

    def __init__(self, bus=EVENT_BUS) -> None:
        self._bus = bus
        #: List of (event_loop, delivery_queue) per open connection.
        self._subscribers: List[Tuple[Optional[asyncio.AbstractEventLoop], asyncio.Queue]] = []
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
        self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ── EventBus callback (worker thread) ───────────────────────────────────

    def _broadcast(self, event: Any) -> None:
        """Deliver a JSON stub of ``event`` to every open connection."""
        payload = json.dumps(
            {"task_id": getattr(event, "task_id", ""), "seq": getattr(event, "sequence", 0)}
        )
        for loop, queue in list(self._subscribers):
            if loop is not None:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            else:
                queue.put_nowait(payload)

    # ── SSE endpoint ────────────────────────────────────────────────────────

    async def sse_stream(self):
        """Starlette/FastAPI endpoint: keep-alive Server-Sent Events stream.

        The stream stays open while the client is connected; idle connections
        receive ``: keep-alive`` comments. Payloads are JSON stubs that the
        browser uses purely as a *wake signal* for one delta sync.
        """
        from starlette.responses import StreamingResponse

        queue = self.connect()

        async def gen():
            try:
                yield "retry: 1000\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), timeout=_HEARTBEAT_SECONDS
                        )
                        yield f"data: {payload}\n\n"
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
