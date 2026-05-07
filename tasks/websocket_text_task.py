"""Celery task: publish recognized text to Redis pubsub for the WebSocket server."""

import json
import logging

import redis as _redis

import config
from tasks.celery_app import app

log = logging.getLogger(__name__)

TEXT_WS_CHANNEL = "cantread:text_ws"


@app.task(name="tasks.send_text_to_websocket")
def send_text_to_websocket(prev_result: list) -> list:
    """Publish the replica text to a Redis pubsub channel and pass through."""
    replica, seq_num = prev_result
    speaker = replica.get("speaker", "Narrator")
    text = replica.get("text", "")

    # payload = json.dumps({"seq": seq_num, "speaker": speaker, "text": text})
    payload = text
    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    r.publish(TEXT_WS_CHANNEL, payload)
    log.debug("text_ws: published seq=%d speaker=%s", seq_num, speaker)

    return prev_result
