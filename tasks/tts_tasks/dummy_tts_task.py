"""Celery task: dummy TTS provider — drops text and marks the sequence as SKIP."""

import logging

import redis as _redis
from celery.exceptions import Ignore

import config
from tasks.celery_app import app

log = logging.getLogger(__name__)


@app.task(name="tasks.run_dummy_tts")
def run_dummy_tts(prev_result: list) -> None:
    replica, seq_num = prev_result
    log.debug("dummy_tts: dropping seq=%d speaker=%s", seq_num, replica.get("speaker", "?"))
    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
    raise Ignore()
