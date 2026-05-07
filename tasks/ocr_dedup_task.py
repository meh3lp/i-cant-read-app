"""Celery task: deduplicate OCR results against recently seen text."""

import logging

import redis as _redis
from celery.exceptions import Ignore

import config
from tasks.celery_app import app
from tasks.utils.ocr_dedup import OCRDedup
from tasks.utils.history import PipelineHistory

log = logging.getLogger(__name__)


@app.task(name="tasks.dedup_ocr")
def dedup_ocr(prev_result: list) -> list:
    """Remove old / repeated text from an OCR result.

    *prev_result* is ``[replica_dict, seq_num]`` where replica_dict is
    ``{"speaker": str, "text": str}``.
    Returns ``[replica_dict, seq_num]`` or marks SKIP + raises Ignore.

    Previous texts are read from the universal pipeline history.
    """
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    history = PipelineHistory(r)
    history.update_status(seq_num, "processing")

    window_size = getattr(config, "OCR_DEDUP_WINDOW_SIZE", 20)
    entries = history.get_entries_before(seq_num, limit=window_size)
    previous_texts = [e["text"] for e in entries]

    result = OCRDedup.dedup(text, previous_texts)

    if result is None:
        log.info("dedup_ocr: rejected seq=%d speaker=%s text=%s", seq_num, speaker, text[:60])
        history.update_status(seq_num, "dropped")
        r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
        raise Ignore()

    log.info("dedup_ocr seq=%d: %s", seq_num, result[:80])
    return [{"speaker": speaker, "text": result}, seq_num]
