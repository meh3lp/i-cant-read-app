"""Celery tasks package — import the app so `celery -A tasks worker` works."""

from .celery_app import app as celery_app  # noqa: F401

from .cleanup_task import clean_text
from .playback_task import enqueue_playback
from .rvc_tasks import (
    run_rvc_gradio,
    run_applio_rvc,
)
from .textfilter_task import filter_text
from .tts_tasks import (
    run_kokoro_fastapi,
    run_applio_tts,
    run_qwen3_tts,
    run_dummy_tts,
)
from .initial_task import initialize_chain, initialize_frame_chain
from .ocr_tasks import (
    run_ollama_ocr_frame,
    run_ollama_plain_ocr_frame,
    run_owocr_ocr_frame
)
from .ocr_dedup_task import dedup_ocr
from .websocket_text_task import send_text_to_websocket

__all__ = (
    "celery_app",
    "clean_text",
    "enqueue_playback",
    "run_rvc_gradio",
    "run_applio_rvc",
    "filter_text",
    "run_kokoro_fastapi",
    "run_applio_tts",
    "run_qwen3_tts",
    "run_dummy_tts",
    "initialize_chain",
    "initialize_frame_chain",
    "run_ollama_ocr_frame",
    "run_ollama_plain_ocr_frame",
    "run_owocr_ocr_frame",
    "dedup_ocr",
    "send_text_to_websocket",
)
