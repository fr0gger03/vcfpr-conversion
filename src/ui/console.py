# SPDX-License-Identifier: Apache-2.0
"""Migration Console: minimal in-memory pub/sub + SSE log stream.

Deliberately not a full job-runner: `broker.publish()` is the single hook a
future `provision` implementation (or this module's own `/console/simulate`
endpoint, used for demos/tests) uses to push progress lines. `/console/stream`
fans them out to every connected client via Server-Sent Events.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=["migration-console"])

_HEARTBEAT_SECONDS = 15.0


class LogBroker:
    """Tiny in-memory fan-out: each subscriber gets its own asyncio.Queue."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def publish(self, message: str) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(message)


broker = LogBroker()


class LogMessage(BaseModel):
    message: str


async def _event_stream(request: Request, max_events: int | None = None) -> AsyncIterator[str]:
    queue = broker.subscribe()
    # Yield immediately so clients (and tests) don't have to wait a full
    # heartbeat interval to confirm the stream is alive.
    yield "event: connected\ndata: migration console connected\n\n"
    emitted = 0
    try:
        while max_events is None or emitted < max_events:
            if await request.is_disconnected():
                break
            try:
                message = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                yield f"data: {message}\n\n"
                emitted += 1
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        broker.unsubscribe(queue)


@router.get("/console/stream")
async def console_stream(request: Request, max_events: int | None = None) -> StreamingResponse:
    """`max_events` bounds how many queued messages are relayed before the
    response closes on its own; real clients should omit it to get a
    never-ending stream. It exists so in-process ASGI test clients (which
    only return a response once the app coroutine finishes) can exercise
    this endpoint without hanging forever -- e.g. `?max_events=0` closes
    right after the initial `connected` event."""
    return StreamingResponse(_event_stream(request, max_events), media_type="text/event-stream")


@router.post("/console/publish")
def console_publish(log: LogMessage) -> dict:
    broker.publish(log.message)
    return {"status": "published", "message": log.message}


@router.post("/console/simulate")
def console_simulate(steps: int = 5) -> dict:
    """Push a handful of canned progress messages -- stand-in for a real
    `provision` run until the CLI wires one up to `broker.publish()`."""
    for i in range(1, steps + 1):
        broker.publish(json.dumps({"step": i, "of": steps, "message": f"Migration step {i}/{steps} complete"}))
    return {"status": "simulated", "steps": steps}


@router.get("/console", response_class=HTMLResponse)
def console_view() -> str:
    return """<html><head><title>Migration Console</title></head>
<body>
<h1>Migration Console</h1>
<pre id="log"></pre>
<script>
  const log = document.getElementById("log");
  const src = new EventSource("/console/stream");
  src.onmessage = (e) => { log.textContent += e.data + "\\n"; };
</script>
</body></html>"""
