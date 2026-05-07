"""WebSocket server that forwards OCR-recognized text to connected clients.

Subscribes to a Redis pubsub channel and broadcasts each message to all
connected WebSocket clients.  Runs its own asyncio event loop in a
dedicated daemon thread (same pattern as MPVPlayer).
"""

import asyncio
import json
import logging
import threading
import typing

import redis as _redis
import websockets
from websockets.asyncio.server import serve

import config
from tasks.websocket_text_task import TEXT_WS_CHANNEL

if typing.TYPE_CHECKING:
    from management.i_cant_read import ICantRead

log = logging.getLogger(__name__)


class TextWebSocketServer:
    def __init__(
        self,
        app: "ICantRead",
        redis_client: "_redis.Redis",
        stop_event: threading.Event,
    ):
        self.app = app
        self._redis = redis_client
        self._stop = stop_event
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set = set()

    # ── public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="text-ws", daemon=True)
        self._thread.start()
        log.info("text websocket server starting on %s:%d", config.TEXT_WEBSOCKET_HOST, config.TEXT_WEBSOCKET_PORT)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ── internals ────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async with serve(self._ws_handler, config.TEXT_WEBSOCKET_HOST, config.TEXT_WEBSOCKET_PORT):
            redis_task = asyncio.create_task(self._redis_subscriber())
            stop_task = asyncio.create_task(self._wait_stop())
            await stop_task
            redis_task.cancel()

    async def _ws_handler(self, websocket) -> None:
        self._clients.add(websocket)
        log.info("text-ws: client connected (%d total)", len(self._clients))
        try:
            async for _ in websocket:
                pass  # we only push, ignore incoming messages
        finally:
            self._clients.discard(websocket)
            log.info("text-ws: client disconnected (%d total)", len(self._clients))

    async def _redis_subscriber(self) -> None:
        """Subscribe to Redis pubsub in a thread and relay messages to the event loop."""
        sub_redis = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        pubsub = sub_redis.pubsub()
        pubsub.subscribe(TEXT_WS_CHANNEL)
        try:
            while not self._stop.is_set():
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                )
                if msg and msg["type"] == "message":
                    await self._broadcast(msg["data"])
        finally:
            pubsub.unsubscribe()
            pubsub.close()
            sub_redis.close()

    async def _broadcast(self, data: str) -> None:
        if not self._clients:
            return
        websockets.broadcast(self._clients, data)

    async def _wait_stop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
