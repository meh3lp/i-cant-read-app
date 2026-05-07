import config
import logging
import redis as _redis
import typing

from celery import chain
from celery.result import AsyncResult

import config

from tasks import (
    clean_text,
    enqueue_playback,
    filter_text,
    run_kokoro_fastapi,
    run_applio_tts,
    run_dummy_tts,
    initialize_chain,
    initialize_frame_chain,
    run_ollama_ocr_frame,
    run_ollama_plain_ocr_frame,
    run_owocr_ocr_frame,
    dedup_ocr,
    send_text_to_websocket,
)
from tasks.utils.history import PipelineHistory
if typing.TYPE_CHECKING:
    from management.i_cant_read import ICantRead

log = logging.getLogger(__name__)

class Dispatcher:
    '''
    Takes whatever listener got (could be text or image)
    and decide where it goes next
    '''
    app: "ICantRead"

    def __init__(
        self,
        app: "ICantRead",
        redis: "_redis.Redis",
    ):
        self.app = app
        self.redis = redis

        tts_task_options = {
            "kokoro_fastapi": run_kokoro_fastapi,
            "applio": run_applio_tts,
            "dummy": run_dummy_tts,
        }
        tts_task_module = tts_task_options.get(config.TTS_PROVIDER)
        if tts_task_module is None:
            log.error("unsupported TTS provider: %s", config.TTS_PROVIDER)
            raise ValueError(f"unsupported TTS provider: {config.TTS_PROVIDER}")
        self._generate_tts = tts_task_module

        if app.ocr_provider_requires_frame_capture:
            ocr_task_options = {
                "ollama": run_ollama_ocr_frame,
                "ollama_plain": run_ollama_plain_ocr_frame,
                "owocr_send_frames": run_owocr_ocr_frame,
            }
            self.ocr_task = ocr_task_options.get(config.OCR_PROVIDER)
            if self.ocr_task is None:
                log.error("unsupported OCR provider: %s", config.OCR_PROVIDER)
                raise ValueError(f"unsupported OCR provider: {config.OCR_PROVIDER}")


    def _next_seq(self) -> int:
        """Atomically claim the next sequence number from Redis."""
        return int(self.redis.incr(config.SEQ_COUNTER_KEY)) - 1


    def _next_frame_seq(self) -> int:
        """Atomically claim the next frame sequence number from Redis."""
        return int(self.redis.incr(config.FRAME_SEQ_COUNTER_KEY)) - 1


    def _build_text_pipeline(self) -> list:
        """Return the shared tail of the chain: dedup → filter → cleanup → tts → rvc → playback."""
        tasks = []

        # ─── Optional OCR dedup ──────────────────────────────────────────
        if config.OCR_DEDUP_ENABLED:
            tasks.append(dedup_ocr.s())

        # ─── Optional TTS pre-processing ─────────────────────────────────
        if config.TEXT_FILTER_ENABLED:
            tasks.append(filter_text.s())
        if config.OLLAMA_TEXT_CLEANUP_ENABLED:
            tasks.append(clean_text.s())

        # ─── Optional WebSocket text push ────────────────────────────────
        if config.TEXT_WEBSOCKET_ENABLED:
            tasks.append(send_text_to_websocket.s())

        # ─── Required TTS step ───────────────────────────────────────────
        tasks.append(self._generate_tts.s())

        # ─── Optional RVC step ───────────────────────────────────────────
        if self.app.rvc is not None:
            tasks.append(self.app.rvc.task.s())

        # ─── Required playback step ──────────────────────────────────────
        tasks.append(enqueue_playback.s())
        return tasks


    def dispatch_recognized_text(self, text: str, speaker: str = "Narrator") -> AsyncResult:
        """Dispatch already-recognized text through the text→TTS pipeline.

        Returns the Celery AsyncResult so callers can optionally wait.
        """
        seq = self._next_seq()
        replica = {"speaker": speaker, "text": text}

        history = PipelineHistory(self.redis)
        batch = PipelineHistory.next_batch(self.redis)
        history.write_entry(seq, text, speaker, batch)

        tasks = [initialize_chain.si((replica, seq))]
        tasks.extend(self._build_text_pipeline())

        pipeline = chain(*tasks)
        result = pipeline.delay()
        log.info("dispatched seq=%d speaker=%s: %s", seq, speaker, text[:80])
        return result


    def dispatch_captured_frame(self, b64_image: str) -> AsyncResult:
        """Dispatch a base64-encoded screenshot for OCR.

        Returns the Celery AsyncResult.  The result value is
        ``[replicas_list, seq_num]`` (or raises Ignore on empty).
        The caller is responsible for fan-out of individual replicas
        via dispatch_recognized_text.
        """
        tasks = [
            initialize_frame_chain.si((b64_image, self._next_frame_seq())),
            self.ocr_task.s(),
        ]

        pipeline = chain(*tasks)
        result = pipeline.delay()
        log.info("dispatched frame for OCR")
        return result
