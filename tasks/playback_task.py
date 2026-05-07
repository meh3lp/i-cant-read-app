"""Celery task: enqueue a finished wav for the Player to pick up."""

import logging

import redis as _redis

import config
from tasks.celery_app import app
from tasks.utils.history import PipelineHistory

log = logging.getLogger(__name__)


@app.task(name="tasks.enqueue_playback")
def enqueue_playback(prev_result: list) -> int:
    """Write the final wav path into the Redis playback hash.

    *prev_result* is ``[wav_path, seq_num]`` from the RVC task.
    Returns *seq_num* for logging / monitoring.
    """
    wav_path, seq_num = prev_result

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), wav_path)

    PipelineHistory(r).update_status(seq_num, "queued")
    log.info("playback: enqueued seq=%d → %s", seq_num, wav_path)

    return seq_num
