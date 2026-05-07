import sys
import signal
import logging
import threading
import redis as _redis
import importlib

import config
import typing

from .healthcheck import run_all_checks
from .dispatcher import Dispatcher
if typing.TYPE_CHECKING:
    from services.rvc import RVC
    from services.listeners import Listener
    from services.player import Player

log = logging.getLogger(__name__)


class ICantRead:
    frame_listener: "Listener|None" = None
    text_listener: "Listener|None" = None
    rvc: "RVC|None" = None
    dispatcher: Dispatcher
    player: "Player|None" = None
    text_ws_server = None

    def __init__(self):
        self._health_check()
        self._connect_redis()

    # ─── Internal methods ───────────────────────────────────────────────────────
    def _health_check(self):
        try:
            run_all_checks()
        except RuntimeError as exc:
            log.error("%s", exc)
            sys.exit(1)


    def _connect_redis(self):
        redis_client = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            redis_client.ping()
        except _redis.ConnectionError:
            log.error("cannot connect to Redis at %s", config.REDIS_URL)
            sys.exit(1)

        # Flush stale pipeline state from a previous run
        redis_client.delete(
            config.PLAYBACK_HASH_KEY,
            config.SEQ_COUNTER_KEY,
            config.HISTORY_HASH_KEY,
            config.BATCH_COUNTER_KEY,
        )
        self._redis = redis_client
        log.info("redis connected, stale keys flushed")


    # ─── Public helper methods ───────────────────────────────────────────────────────
    @property
    def ocr_provider_requires_frame_capture(self) -> bool:
        return config.OCR_PROVIDER in ["ollama", "ollama_plain", "owocr_send_frames"]


    @property
    def ocr_provider_returns_text_immediately(self) -> bool:
        """
        for non-immediate returning ocr providers we run 2 listeners:
        first listener receives frames and sends to dispatch_captured_frame
        second listener receives text and sends it to dispatch_recognized_text
        """
        return config.OCR_PROVIDER in ["ollama", "ollama_plain", "owocr_receive_only"]

    @property
    def listeners(self) -> list:
        listeners = []
        if self.frame_listener:
            listeners.append(self.frame_listener)
        if self.text_listener:
            listeners.append(self.text_listener)
        return listeners

    # ─── Main logic methods ───────────────────────────────────────────────────────
    def run(self):
        '''
        Start the main pipeline: listeners → dispatcher → tasks → player.
        '''
        self._stop_event = threading.Event()

        # Initialize components in order of dependencies: tasks may depend on player, dispatcher depends on tasks, listener depends on dispatcher.
        # 1) Player
        self._init_player()

        # 1.1) Text WebSocket server
        self._init_text_ws()

        # 2) Tasks
        # 2.1) RVC init
        self._init_rvc()

        # 3) Dispatcher
        self.dispatcher = Dispatcher(self, self._redis)

        # 4) Listeners
        self._init_listeners()

        # Start everything
        if self.player:
            self.player.start()
        if self.text_ws_server:
            self.text_ws_server.start()
        for listener in self.listeners:
            listener.start()

        log.info("pipeline running — press Ctrl+C to stop")

        self._shutdown = threading.Event()

        def _on_signal(sig, _frame):
            log.info("received %s — shutting down…", signal.Signals(sig).name)
            self._shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        self._shutdown.wait()

        log.info("stopping listener…")
        for listener in self.listeners:
            listener.stop()
        if self.player:
            log.info("stopping player…")
            self.player.stop()
        if self.text_ws_server:
            log.info("stopping text websocket server…")
            self.text_ws_server.stop()
        log.info("done")


    def _init_player(self):
        if config.PLAYER_ENABLED:
            player_services = {
                "mpv": ("services.player.mpv_player", "MPVPlayer"),
            }
            player_module = importlib.import_module(player_services[config.PLAYER][0])
            player_class = getattr(player_module, player_services[config.PLAYER][1])
            self.player = player_class(self, self._redis, self._stop_event)
            return self.player


    def _init_text_ws(self):
        if config.TEXT_WEBSOCKET_ENABLED:
            from services.text_websocket import TextWebSocketServer
            self.text_ws_server = TextWebSocketServer(self, self._redis, self._stop_event)


    def _init_rvc(self):
        if config.RVC_PROVIDER and config.TTS_PROVIDER != "applio":
            # Applio TTS already includes RVC; skip standalone RVC init
            rvc_services = {
                "rvc_gradio": ("services.rvc.rvc_gradio", "RVCGradio"),
                "applio": ("services.rvc.rvc_applio", "RVCApplio"),
            }
            rvc_module = importlib.import_module(rvc_services[config.RVC_PROVIDER][0])
            rvc_class = getattr(rvc_module, rvc_services[config.RVC_PROVIDER][1])
            self.rvc = rvc_class(self)
    

    def _init_listeners(self):
        # 1) Frame capture listeners
        if self.ocr_provider_requires_frame_capture:
            frame_listener_services = {
                "obs_plugin": ("services.listeners.obs_plugin_listener", "OBSPluginListener"),
                "obs_websocket": ("services.listeners.obs_websocket_listener", "OBSWebSocketListener"),
            }

            frame_listener_module = importlib.import_module(frame_listener_services[config.FRAME_CAPTURE_METHOD][0])
            frame_listener_class = getattr(frame_listener_module, frame_listener_services[config.FRAME_CAPTURE_METHOD][1])
            self.frame_listener = frame_listener_class(
                self,
                self.dispatcher,
                self._stop_event,
                self._redis
            )
        # 2) Text-only listeners
        if not self.ocr_provider_returns_text_immediately or not self.ocr_provider_requires_frame_capture:
            text_listener_services = {
                "websocket": ("services.listeners.websocket_listener", "WebSocketListener"),
            }
            text_listener_module = importlib.import_module(text_listener_services[config.TEXT_LISTENER_METHOD][0])
            text_listener_class = getattr(text_listener_module, text_listener_services[config.TEXT_LISTENER_METHOD][1])
            self.text_listener = text_listener_class(
                self,
                self.dispatcher,
                self._stop_event,
                self._redis
            )
