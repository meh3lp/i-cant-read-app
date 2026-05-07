"""Celery task: pre-LLM text filtering (noise, fuzzy dedup, overlap)."""

import logging

import redis as _redis
from celery.exceptions import Ignore

import config
from tasks.celery_app import app
from tasks.utils import TextFilter
from tasks.utils.history import PipelineHistory

log = logging.getLogger(__name__)


@app.task(name="tasks.filter_text")
def filter_text(prev_result: list) -> list:
    """Filter *text* through the noise / dedup / overlap pipeline.

    *prev_result* is ``[replica_dict, seq_num]`` where replica_dict is
    ``{"speaker": str, "text": str}``.
    Returns ``[replica_dict, seq_num]`` on success.
    Marks the playback hash as ``SKIP`` and raises :class:`Ignore` if rejected.

    Previous texts are read from the universal pipeline history.
    """
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    history = PipelineHistory(r)

    window_size = getattr(config, "TEXT_FILTER_WINDOW_SIZE", 10)
    entries = history.get_entries_before(seq_num, limit=window_size, exclude_dropped=True)
    previous_texts = [e["text"] for e in entries]

    filtered = TextFilter.filter(text, previous_texts)

    if filtered is None:
        log.info("filter: rejected seq=%d text=%s", seq_num, text[:60])
        history.update_status(seq_num, "dropped")
        r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
        raise Ignore()

    return [{"speaker": speaker, "text": filtered}, seq_num]
