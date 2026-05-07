import time
import pathlib
import socket
import threading
import logging
import base64

import config
from .listener import Listener

log = logging.getLogger(__name__)

class OBSPluginListener(Listener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread: threading.Thread | None = None

    # ─── public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="OBSPluginListenerThread",
            daemon=True
        )
        self._thread.start()
        log.info("OBSPluginListener started")

    def stop(self):
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ─── internals ───────────────────────────────────────────────────────
    def _read_frame(self):
        log.info("Reading frame from OBS plugin path: %s", config.OBS_PLUGIN_FRAME_PATH)
        path = config.OBS_PLUGIN_FRAME_PATH
        with open(path, "rb") as f:
            b64_string = base64.b64encode(f.read()).decode("utf-8")
        log.info("Read and encoded frame from OBS plugin, size=%d bytes", len(b64_string))
        return b64_string

    def _run(self) -> None:
        socket_path = config.OBS_PLUGIN_GATE_SOCKET_PATH
        pathlib.Path(socket_path).unlink(missing_ok=True)

        self._pending_ocr = []

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.bind(socket_path)
        while not self.stop_event.is_set():
            try:
                self._check_pending_ocr()
                sock.recv(1)  # blocks until a frame passes all gates
                log.info("Received frame capture signal from OBS plugin")
                frame = self._read_frame()
                result = self.dispatcher.dispatch_captured_frame(frame)
                self._pending_ocr.append(result)
            except TimeoutError:
                continue  # just re-check the loop condition
            except Exception as e:
                log.error("Error in OBSPluginListener: %s", e)
        sock.close()

    def _check_pending_ocr(self) -> None:
        still_pending = []
        for result in self._pending_ocr:
            if not result.ready():
                still_pending.append(result)
                continue
            try:
                replicas, _seq = result.get(propagate=True)
            except Exception as e:
                log.error("OCR chain failed: %s", e)
                continue
            for replica in replicas:
                self.dispatcher.dispatch_recognized_text(
                    replica.get("text", ""),
                    speaker=replica.get("speaker", "Narrator"),
                )
        self._pending_ocr = still_pending
