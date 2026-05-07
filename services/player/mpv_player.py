"""Sequential audio playback queue using a single persistent mpv process.

The Player thread polls a Redis hash (keyed by sequence number) instead of
a Python queue, ensuring playback order matches OCR arrival order even when
Celery chains finish out of order.
"""

import json
import logging
import os
import socket
import subprocess
import threading
import time
import typing

import config

if typing.TYPE_CHECKING:
    import redis as _redis
    from management.i_cant_read import ICantRead

log = logging.getLogger(__name__)

_SOCK_PATH = f"/tmp/cantread-mpv-{os.getpid()}.sock"



class MPVPlayer:
    """Spawns one mpv in --idle mode and sends it files over IPC.

    Reads finished wav paths from the Redis playback hash in strict
    sequence-number order.
    """

    def __init__(
        self,
        app: "ICantRead",
        redis_client: "_redis.Redis",
        stop_event: threading.Event
    ):
        self.app = app
        self._redis = redis_client
        self._stop = stop_event
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_seq: int = 0

    # ── public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._start_mpv()
        self._connect_ipc()
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._thread.start()
        log.info("player started (mpv pid=%d, next_seq=%d)", self._proc.pid, self._next_seq)

    def stop(self) -> None:
        self._stop.set()
        self._send_command(["quit"])
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._proc:
            self._proc.wait(timeout=5)
        if self._thread:
            self._thread.join(timeout=5)
        try:
            os.unlink(_SOCK_PATH)
        except FileNotFoundError:
            pass
        # Clean up Redis keys
        try:
            self._redis.delete(config.PLAYBACK_HASH_KEY)
        except Exception:
            pass

    # ── mpv lifecycle ────────────────────────────────────────────────────

    def _start_mpv(self) -> None:
        # clean up stale socket
        try:
            os.unlink(_SOCK_PATH)
        except FileNotFoundError:
            pass

        self._proc = subprocess.Popen(
            [
                "mpv",
                "--idle",
                "--no-video",
                "--no-terminal",
                "--really-quiet",
                "--keep-open=no",
                f"--input-ipc-server={_SOCK_PATH}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _connect_ipc(self) -> None:
        """Wait for the IPC socket to appear then connect."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(_SOCK_PATH):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("mpv IPC socket did not appear")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(_SOCK_PATH)
        self._sock.setblocking(False)
        log.debug("connected to mpv IPC socket")

    # ── IPC helpers ──────────────────────────────────────────────────────

    def _send_command(self, cmd: list) -> None:
        if not self._sock:
            return
        msg = json.dumps({"command": cmd}) + "\n"
        try:
            self._sock.sendall(msg.encode())
        except OSError:
            log.warning("mpv IPC send failed")

    def _read_events(self, timeout: float = 0.1) -> list[dict]:
        """Read any pending JSON lines from mpv. Non-blocking."""
        events = []
        if not self._sock:
            return events
        try:
            self._sock.settimeout(timeout)
            data = self._sock.recv(4096)
            if data:
                self._buf += data
        except (socket.timeout, BlockingIOError):
            pass
        except OSError:
            return events

        # split on newlines — each line is one JSON message
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return events

    def _wait_end_file(self) -> None:
        """Block until mpv fires an 'end-file' event or we're told to stop."""
        while not self._stop.is_set():
            for ev in self._read_events(timeout=0.2):
                if ev.get("event") == "end-file":
                    time.sleep(config.PLAYER_DELAY)  # avoid overlap with next file
                    return
            # also bail if mpv died
            if self._proc and self._proc.poll() is not None:
                return

    # ── main loop ────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            # Poll Redis for the next sequence number
            value = self._redis.hget(config.PLAYBACK_HASH_KEY, str(self._next_seq))

            if value is None:
                # Not ready yet — wait and retry
                time.sleep(0.1)
                continue

            # Remove the consumed entry
            self._redis.hdel(config.PLAYBACK_HASH_KEY, str(self._next_seq))
            seq = self._next_seq
            self._next_seq += 1

            # Handle SKIP markers (task was filtered / failed recognition)
            if value == "SKIP":
                log.debug("player: skipping seq=%d", seq)
                continue

            wav_path = value

            if not os.path.isfile(wav_path):
                log.error("file does not exist: %s (seq=%d)", wav_path, seq)
                continue

            log.info("playing seq=%d: %s", seq, wav_path)

            # drain any stale events
            self._read_events(timeout=0)

            # tell mpv to play this file
            self._send_command(["loadfile", wav_path, "replace"])
            self._wait_end_file()

            log.info("finished seq=%d: %s", seq, wav_path)
