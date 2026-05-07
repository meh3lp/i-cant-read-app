"""Celery task: OCR text cleanup via Ollama LLM."""

import logging

import redis as _redis
from celery.exceptions import Ignore
from ollama import Client

import config
from tasks.celery_app import app
from tasks.utils.history import PipelineHistory

log = logging.getLogger(__name__)

_client = Client(host=config.OLLAMA_URL, timeout=30)


@app.task(name="tasks.clean_text")
def clean_text(prev_result: list) -> list:
    """Clean OCR text through Ollama.

    *prev_result* is ``[replica_dict, seq_num]`` where replica_dict is
    ``{"speaker": str, "text": str}``.
    Returns ``[replica_dict, seq_num]`` or marks SKIP + raises Ignore.

    Conversation history is built from the universal pipeline history.
    """
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    log.info("cleaning: %s", text[:80])

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    history = PipelineHistory(r)
    history_size = config.OLLAMA_CLEANUP_HISTORY_SIZE

    # Build LLM conversation context from pipeline history.
    # Each past entry becomes a user (original) / assistant (cleaned) exchange.
    llm_history: list[dict] = []
    if history_size > 0:
        entries = history.get_entries_before(
            seq_num, limit=history_size, exclude_dropped=True,
        )
        for e in entries:
            llm_history.append({"role": "user", "content": e["text"]})
            llm_history.append({
                "role": "assistant",
                "content": e.get("cleaned_text") or e["text"],
            })

    cleaned = text
    try:
        messages = [
            {"role": "system", "content": config.OLLAMA_CLEANUP_SYSTEM_PROMPT},
            *llm_history,
            {"role": "user", "content": text},
        ]
        resp = _client.chat(
            model=config.OLLAMA_CLEANUP_MODEL,
            messages=messages,
            options={"num_predict": 2048},
            think=False,
            keep_alive=config.OLLAMA_KEEP_ALIVE,
        )
        result = resp.message.content.strip()
        if not result:
            log.warning("ollama returned empty — using original")
        else:
            log.info("cleaned:  %s", result[:80])
            cleaned = result

        history.update_cleaned_text(seq_num, cleaned)
    except Exception:
        log.exception("ollama cleanup failed — using original text")

    if cleaned.strip().lower() == "failed recognition":
        log.info("cleanup: failed recognition for seq=%d", seq_num)
        history.update_status(seq_num, "dropped")
        r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
        raise Ignore()

    return [{"speaker": speaker, "text": cleaned}, seq_num]
