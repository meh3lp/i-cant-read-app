"""Celery task: OCR a screenshot via Ollama vision model (plain text mode)."""

import logging

import redis as _redis
from celery.exceptions import Ignore
from ollama import Client

import config
from tasks.celery_app import app

log = logging.getLogger(__name__)

_client = Client(host=config.OLLAMA_URL, timeout=60)


@app.task(bind=True, name="tasks.run_ollama_plain_ocr_frame", max_retries=3, default_retry_delay=5)
def run_ollama_plain_ocr_frame(self, prev_result: list) -> list:
    b64_image, seq_num = prev_result
    log.info("ocr_plain: processing seq=%d", seq_num)
    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

    try:
        raw = _do_ocr(b64_image, seq_num)
    except Exception as e:
        log.error("ocr_plain: error in OCR for seq=%d: %s", seq_num, str(e))
        raise self.retry(exc=e)

    if not raw or raw == "[EMPTY]":
        log.info("ocr_plain: no text for seq=%d — SKIP", seq_num)
        r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
        raise Ignore()

    replicas = [{"speaker": "Narrator", "text": raw}]
    log.info("ocr_plain seq=%d: %s", seq_num, raw[:120])
    return [replicas, seq_num]


def _do_ocr(b64_image, seq_num) -> str:
    messages = [
        {"role": "system", "content": config.OLLAMA_OCR_PLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": config.OLLAMA_OCR_PLAIN_USER_PROMPT, "images": [b64_image]},
    ]
    resp = _client.chat(
        model=config.OLLAMA_OCR_PLAIN_MODEL,
        messages=messages,
        options={"num_predict": 1024},
        think=False,
        keep_alive=config.OLLAMA_KEEP_ALIVE,
    )
    raw = resp.message.content.strip()
    log.info("Ollama plain raw response for seq=%d: %s", seq_num, raw.replace("\n", " "))
    return raw
