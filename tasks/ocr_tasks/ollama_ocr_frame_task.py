"""Celery task: OCR a screenshot via Ollama vision model (characters mode)."""

import json
import logging
import re
from difflib import SequenceMatcher

import redis as _redis
from celery.exceptions import Ignore
from celery import group
from ollama import Client

import config
from tasks.celery_app import app

log = logging.getLogger(__name__)

_client = Client(host=config.OLLAMA_URL, timeout=60)

_REPLICAS_SCHEMA = {
    "type": "object",
    "properties": {
        "text_type": {
            "type": "string"
        },
        "replicas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["speaker", "text"],
            },
        },
    },
    "required": ["replicas", "text_type"],
}


@app.task(name="tasks.run_ollama_ocr_frame")
def run_ollama_ocr_frame(prev_result: list) -> list:
    """OCR a base64-encoded PNG frame via Ollama vision.

    *prev_result* is ``[b64_image, seq_num]`` from initialize_frame_chain.
    Returns ``[replicas_list, seq_num]`` or marks SKIP + raises Ignore.
    Each replica is ``{"speaker": str, "text": str}``.
    """
    b64_image, seq_num = prev_result

    log.info("ocr_frame: processing seq=%d", seq_num)

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True)


    all_replicas = []

    # for _ in range(config.OCR_PASSES):
    #     try:
    #         data = _do_ocr(b64_image, seq_num)
    #     except Exception as e:
    #         log.error("ocr_frame: error in OCR pass for seq=%d: %s", seq_num, str(e))
    #         raise self.retry(exc=e)
    #     replicas = data.get("replicas", [])
    #     if replicas:
    #         all_replicas.extend(replicas)
    #     else:
    #         log.warning("ocr_frame: no replicas from Ollama for seq=%d", seq_num)
    #     if data.get("text_type", "").lower() == "book":
    #         break

    tasks = [run_ollama_ocr_single_pass.s([b64_image, seq_num]) for _ in range(config.OCR_PASSES)]
    results = group(tasks).apply().get(disable_sync_subtasks=False)
    pass_replica_lists = []
    for data in results:
        replicas = data.get("replicas", [])
        if replicas:
            pass_replica_lists.append(replicas)
        else:
            log.warning("ocr_frame: no replicas from Ollama for seq=%d in one pass", seq_num)

    all_replicas = _merge_passes(pass_replica_lists)

    if not all_replicas:
        log.info("ocr_frame: no replicas for seq=%d — SKIP", seq_num)
        r.hset(config.PLAYBACK_HASH_KEY, str(seq_num), "SKIP")
        raise Ignore()

    log.info("ocr_frame seq=%d: %d replicas", seq_num, len(all_replicas))
    return [all_replicas, seq_num]


# ── multi-pass merge ─────────────────────────────────────────────────────────

_MERGE_THRESHOLD = 0.85  # fuzzy-match threshold for cross-pass replica matching


def _normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _replica_matches(a: dict, b: dict, threshold: float = _MERGE_THRESHOLD) -> bool:
    """True if two replica dicts represent the same text."""
    ta = _normalize_text(a.get("text", ""))
    tb = _normalize_text(b.get("text", ""))
    if not ta or not tb:
        return False
    return SequenceMatcher(None, ta, tb).ratio() >= threshold


def _merge_passes(pass_lists: list[list[dict]]) -> list[dict]:
    """Merge replica lists from multiple OCR passes into one ordered list.

    Uses the longest pass as the anchor ordering.  Replicas from other
    passes that don't match any anchor item are inserted at the position
    implied by their surrounding matched neighbours.

    Returns a single ordered list of replica dicts with duplicates removed.
    """
    if not pass_lists:
        return []
    if len(pass_lists) == 1:
        return pass_lists[0]

    # Pick longest as anchor
    anchor = max(pass_lists, key=len)
    others = [p for p in pass_lists if p is not anchor]

    # For each non-anchor pass, find which anchor index each item matches
    for other in others:
        # Build match mapping: other_idx -> anchor_idx (or -1)
        matches: list[int] = []
        used_anchor: set[int] = set()
        for o_item in other:
            best_idx = -1
            best_ratio = 0.0
            for a_idx, a_item in enumerate(anchor):
                if a_idx in used_anchor:
                    continue
                ta = _normalize_text(a_item.get("text", ""))
                tb = _normalize_text(o_item.get("text", ""))
                if not ta or not tb:
                    continue
                ratio = SequenceMatcher(None, ta, tb).ratio()
                if ratio >= _MERGE_THRESHOLD and ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = a_idx
            matches.append(best_idx)
            if best_idx >= 0:
                used_anchor.add(best_idx)

        # Insert unmatched items into anchor at the right position
        # Walk other in reverse so inserts don't shift indices
        inserts: list[tuple[int, dict]] = []  # (insert_before_anchor_idx, item)
        for o_idx, a_idx in enumerate(matches):
            if a_idx >= 0:
                continue  # already in anchor
            # Find nearest matched neighbour *after* this item
            insert_pos = len(anchor)  # default: append
            for later in range(o_idx + 1, len(other)):
                if matches[later] >= 0:
                    insert_pos = matches[later]
                    break
            inserts.append((insert_pos, other[o_idx]))

        # Sort inserts by position (stable) and apply in reverse
        inserts.sort(key=lambda x: x[0])
        for pos, item in reversed(inserts):
            # Avoid inserting duplicates of items already in anchor
            if any(_replica_matches(item, a) for a in anchor):
                continue
            anchor.insert(pos, item)

    return anchor


@app.task(bind=True, name="tasks.run_ollama_ocr_single_pass", max_retries=3, default_retry_delay=5)
def run_ollama_ocr_single_pass(self, prev_result: list):
    """
    Celery task wrapper for _do_ocr to allow retries
    """
    b64_image, seq_num = prev_result
    log.info("ocr_frame (single pass): processing seq=%d", seq_num)
    try:
        data = _do_ocr(b64_image, seq_num)
    except Exception as e:
        log.error("ocr_frame (single pass): error in OCR for seq=%d: %s", seq_num, str(e))
        raise self.retry(exc=e)
    return data


# ── duplicate-key aware JSON parsing ─────────────────────────────────────────


def _parse_replicas_json(raw: str):
    """Parse JSON that may contain duplicate keys inside replica objects.

    LLMs sometimes produce ``{"text": "A", "text": "B"}`` — Python's
    ``json.loads`` silently keeps only the last value.  This function
    detects duplicate ``"text"`` keys and splits them into separate
    replica dicts so no content is lost.
    """

    def _split_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        """``object_pairs_hook`` for :func:`json.loads`.

        When a key appears more than once, collect the extras.  The
        marker ``__dup_texts__`` is used to smuggle them out.
        """
        result: dict = {}
        extras: list[dict] = []
        seen_text = False
        current_speaker = None

        for key, value in pairs:
            if key == "speaker":
                if "speaker" not in result:
                    result["speaker"] = value
                    current_speaker = value
                # ignore duplicate speaker keys
            elif key == "text":
                if not seen_text:
                    result["text"] = value
                    seen_text = True
                else:
                    # Duplicate "text" — create a new replica
                    extra: dict = {"text": value}
                    if current_speaker:
                        extra["speaker"] = current_speaker
                    extras.append(extra)
            else:
                result[key] = value

        if extras:
            result["__dup_texts__"] = extras
        return result

    data = json.loads(raw, object_pairs_hook=_split_duplicate_keys)

    # Post-process: expand any __dup_texts__ markers inside replicas
    if isinstance(data, dict) and "replicas" in data:
        expanded: list[dict] = []
        for rep in data["replicas"]:
            if not isinstance(rep, dict):
                expanded.append(rep)
                continue
            extras = rep.pop("__dup_texts__", None)
            expanded.append(rep)
            if extras:
                expanded.extend(extras)
        data["replicas"] = expanded

    return data


def _do_ocr(b64_image, seq_num):
    messages = [
        {
            "role": "system",
            "content": config.OLLAMA_OCR_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": "OCR this image.",
            "images": [b64_image],
        },
    ]
    resp = _client.chat(
        model=config.OLLAMA_OCR_MODEL,
        messages=messages,
        options={"num_predict": 1024},
        think=False,
        format=_REPLICAS_SCHEMA,
        keep_alive=config.OLLAMA_KEEP_ALIVE,
    )
    raw = resp.message.content.strip()
    log.info("Ollama raw response for seq=%d: %s", seq_num, raw.replace("\n", ""))
    if "''''" in raw:
        # Strip '''json ... ''' if present
        # raw = raw.split("'''", 1)[-1].rsplit("'''", 1)[0]
        raw = raw.strip("'''").strip("json").strip()
    if "```" in raw:
        # Strip ```json ... ``` if present
        raw = raw.strip("```").strip("json").strip()
    data = _parse_replicas_json(raw)
    # AI returned wrong JSON root object
    if isinstance(data, list):
        # ```json[{"replicas": [{"speaker": "Narrator", "text": "You see, it's the time of the year again for the Titan popularity vote. The competition this time round is quite intense — Ol' Thannie's new album has got all the disciples in a zealous frenzy, and Mnestia's fabulous outfit at the fashion exhibition has garnered plenty of supporters."}]}]```
        if len(data) > 0:
            item = data[0]
            if isinstance(item, dict) and "replicas" in item:
                data = item
            elif isinstance(item, dict) and "speaker" in item and "text" in item:
                data = {"replicas": data}
            else:
                raise ValueError("Unexpected JSON structure: list without 'replicas' or replica dicts")

    replicas = data.get("replicas", [])

    # Filter out replicas with empty text
    replicas = [rep for rep in replicas if rep.get("text", "").strip()]
    data["replicas"] = replicas
    return data
