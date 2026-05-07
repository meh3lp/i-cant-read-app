import logging
import asyncio
import threading
import websockets

import config
from services.listeners.listener import Listener

log = logging.getLogger(__name__)

class WebsocketClientListener(Listener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread: threading.Thread | None = None

    # ─── public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="WebsocketClientListenerThread",
            daemon=True
        )
        self._thread.start()
        log.info("WebsocketClientListener started")

    def stop(self):
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ─── internals ───────────────────────────────────────────────────────
    
    def _run(self) -> None:
        """Thread entry — spins up a tiny asyncio loop for the websocket."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._listen())
        except Exception as e:
            if not self._stop.is_set():
                log.exception(f"WebsocketClientListener listener crashed ({e})")
        finally:
            loop.close()


    async def _listen(self) -> None:
        """Connect (with reconnect) and consume messages."""
        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(config.TEXT_SOURCE_WEBSOCKET_CLIENT_URL) as ws:
                    log.info("connected to text source websocket server")
                    backoff = 1
                    async for message in ws:
                        if self._stop.is_set():
                            return
                        text = str(message).strip()
                        if not text:
                            continue

                        # Dispatch the Celery chain
                        self.dispatcher.dispatch_recognized_text(
                            text,
                            speaker="Narrator",
                        )

            except websockets.ConnectionClosed:
                log.warning("text source websocket closed, reconnecting in %ds…", backoff)
            except Exception:
                if self._stop.is_set():
                    return
                log.warning("text source websocket connection error, retrying in %ds…", backoff)

            # exponential back-off capped at 30s
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
