"""
Celery task for the qwen3-tts-server /tts/voice-clone endpoint.

Features a custom logic for registering custom voice clones on the server and batching sentences

Expected config.py additions (mirrors the KOKORO_* globals used by
tasks/tts_tasks/kokoro_fastapi_task.py):

    QWEN3_TTS_URL = "http://localhost:8000"     # qwen3-tts-server base URL
    QWEN3_TTS_REF_AUDIO_PATH = "/path/to/default_reference.wav"
    QWEN3_TTS_REF_TEXT = "Transcript of the default reference clip."
    QWEN3_TTS_LANGUAGE = "Auto"                  # optional, default shown
    QWEN3_TTS_MODEL = None                       # optional model id override
    QWEN3_TTS_TIMEOUT = 120                       # optional, seconds
    QWEN3_TTS_PROMPT_TIMEOUT = 60                 # optional, seconds
    QWEN3_TTS_MAX_BATCH_SENTENCES = 16            # optional, sentences/request
    REDIS_URL = "redis://localhost:6379/0"        # if not already defined

And a per-speaker override in VOICE_PRESETS, alongside the existing
"kokoro_fastapi" entries:

    VOICE_PRESETS = {
        "default": {
            "tts": {
                "qwen3_tts_voice_clone": {
                    "ref_audio_path": "/path/to/default_reference.wav",
                    "ref_text": "Transcript of the default reference clip.",
                    "language": "English",
                },
            },
        },
        "Narrator": {
            "tts": {
                "qwen3_tts_voice_clone": {
                    "ref_audio_path": "/path/to/narrator_reference.wav",
                    "ref_text": "Transcript of the narrator reference clip.",
                    "language": "English",
                    # "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",  # optional
                },
            },
        },
    }
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import time
import wave
import zipfile

import redis
import requests

from tasks.celery_app import app
import config

log = logging.getLogger(__name__)

_redis = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

# How much earlier (seconds) than the server's own TTL we let our cached
# prompt_id expire, so we never hand the server an id it has already
# garbage-collected on its side. Comfortably larger than one task's runtime.
_PROMPT_TTL_SAFETY_MARGIN = 60

# How long a worker is allowed to hold the "I'm creating this prompt" lock,
# and how often other workers poll while waiting on it.
_PROMPT_LOCK_TIMEOUT = 30
_PROMPT_LOCK_POLL_INTERVAL = 0.5

# Splits on ., !, ?, … followed by whitespace (Latin-script convention), or
# immediately after CJK terminators 。！？ (which conventionally have no
# following space). This is a lightweight heuristic, NOT a full sentence
# tokenizer -- it doesn't special-case abbreviations like "Mr." or decimal
# numbers. Swap in `pysbd` or `nltk.sent_tokenize` here if that matters.
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?\u2026])(?:[\'"\u2019\u201d)\]]+)?\s+(?=\S)'
    r'|'
    r'(?<=[\u3002\uff01\uff1f])(?:[\'"\u2019\u201d)\]]+)?\s*'
)

# Matches the "output_<N>.wav" names the server puts in a batch-response ZIP.
_ZIP_ENTRY_RE = re.compile(r'output_(\d+)\.wav$')


def _get_tts_preset(speaker: str) -> dict:
    """Look up the Qwen3-TTS voice-clone preset for *speaker*.

    Falls back to the ``"default"`` preset, then to an empty dict (which
    causes the task to use the QWEN3_TTS_REF_* global config values).
    """
    presets = getattr(config, "VOICE_PRESETS", {})
    entry = presets.get(speaker, presets.get("default", {}))
    tts = entry.get("tts", {})
    return tts.get("qwen3_tts_voice_clone", {})


def _split_sentences(text: str) -> list[str]:
    """Lightweight, dependency-free sentence splitter (see notes above)."""
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _prompt_cache_key(ref_audio_path: str, ref_text: str, model: str | None) -> str:
    """Redis key that changes whenever the reference clip, its transcript,
    or the target model changes -- so editing a reference file (which
    updates its mtime) automatically invalidates the cached prompt instead
    of silently reusing stale voice features.
    """
    stat = os.stat(ref_audio_path)
    fingerprint = f"{ref_audio_path}:{stat.st_mtime_ns}:{stat.st_size}:{ref_text}:{model}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"qwen3_tts:voice_clone_prompt:{digest}"


def _create_prompt(qwen_url: str, ref_audio_path: str, ref_text: str, model: str | None) -> tuple[str, int]:
    """Calls POST /tts/voice-clone/prompt. Returns (prompt_id, expires_in_seconds)."""
    data = {"ref_text": ref_text}
    if model:
        data["model"] = model
    with open(ref_audio_path, "rb") as f:
        response = requests.post(
            f"{qwen_url}/tts/voice-clone/prompt",
            data=data,
            files={"file": (os.path.basename(ref_audio_path), f, "audio/wav")},
            timeout=getattr(config, "QWEN3_TTS_PROMPT_TIMEOUT", 60),
        )
    response.raise_for_status()
    body = response.json()
    return body["prompt_id"], body["expires_in_seconds"]


def _get_or_create_prompt_id(qwen_url: str, ref_audio_path: str, ref_text: str, model: str | None) -> str:
    """Redis-cached voice-clone prompt_id, shared across tasks/workers so the
    comparatively expensive reference-audio feature extraction only happens
    once per reference clip, not once per line of dialogue.
    """
    cache_key = _prompt_cache_key(ref_audio_path, ref_text, model)

    cached = _redis.get(cache_key)
    if cached:
        return cached

    # Serialize prompt creation across concurrent workers so several
    # replicas for the same speaker arriving at once don't all pay for
    # feature extraction independently.
    lock_key = f"{cache_key}:lock"
    got_lock = _redis.set(lock_key, "1", nx=True, ex=_PROMPT_LOCK_TIMEOUT)

    if not got_lock:
        deadline = time.monotonic() + _PROMPT_LOCK_TIMEOUT
        while time.monotonic() < deadline:
            cached = _redis.get(cache_key)
            if cached:
                return cached
            time.sleep(_PROMPT_LOCK_POLL_INTERVAL)
        log.warning("tts: timed out waiting for prompt lock %s, creating anyway", lock_key)

    try:
        prompt_id, expires_in_seconds = _create_prompt(qwen_url, ref_audio_path, ref_text, model)
        ttl = max(expires_in_seconds - _PROMPT_TTL_SAFETY_MARGIN, _PROMPT_TTL_SAFETY_MARGIN)
        _redis.set(cache_key, prompt_id, ex=ttl)
        return prompt_id
    finally:
        _redis.delete(lock_key)


def _unpack_wavs(response: requests.Response) -> list[bytes]:
    """A batch of 1 comes back as raw audio/wav; a batch of >1 comes back as
    a ZIP of output_0.wav, output_1.wav, ... -- normalize both to a list of
    WAV byte-blobs in the original order.
    """
    if "zip" in response.headers.get("content-type", ""):
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = sorted(
                zf.namelist(),
                key=lambda n: int(m.group(1)) if (m := _ZIP_ENTRY_RE.search(n)) else 0,
            )
            return [zf.read(n) for n in names]
    return [response.content]


def _generate_batch(
    qwen_url: str,
    texts: list[str],
    language: str,
    prompt_id: str,
    ref_audio_path: str,
    ref_text: str,
    model: str | None,
    timeout: float,
) -> tuple[list[bytes], str]:
    """POSTs one batch of sentences to /tts/voice-clone. Returns
    (wav_blobs_in_order, prompt_id) -- prompt_id is handed back in case a
    stale cached id had to be refreshed, so the caller reuses the fresh one
    for any remaining batches instead of hitting the same 404 repeatedly.
    """
    payload = {"text": texts, "language": [language] * len(texts), "prompt_id": prompt_id}
    response = requests.post(f"{qwen_url}/tts/voice-clone", json=payload, timeout=timeout)

    if response.status_code == 404:
        # Our cached prompt_id outlived the server's copy of it (e.g. the
        # server restarted and lost its in-memory prompt cache before our
        # Redis TTL caught up). Drop the stale entry, recreate it, retry once.
        log.warning("tts: prompt_id=%s expired server-side, recreating", prompt_id)
        _redis.delete(_prompt_cache_key(ref_audio_path, ref_text, model))
        prompt_id = _get_or_create_prompt_id(qwen_url, ref_audio_path, ref_text, model)
        payload["prompt_id"] = prompt_id
        response = requests.post(f"{qwen_url}/tts/voice-clone", json=payload, timeout=timeout)

    response.raise_for_status()
    return _unpack_wavs(response), prompt_id


def _concat_wavs(wav_blobs: list[bytes]) -> bytes:
    """Concatenate same-format PCM WAV byte-blobs into one, using only the
    stdlib `wave` module -- qwen3-tts-server writes PCM_16 WAVs by default,
    so this needs no extra audio dependencies in the Celery worker. If your
    server config ever produces a different subtype (e.g. float WAV), this
    will raise `wave.Error: unknown format`; switch to a soundfile-based
    concat in that case.
    """
    if len(wav_blobs) == 1:
        return wav_blobs[0]

    frames = []
    params = None
    for blob in wav_blobs:
        with wave.open(io.BytesIO(blob), "rb") as wf:
            cur = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
            if params is None:
                params = cur
            elif cur != params:
                raise ValueError(f"Cannot concatenate WAVs with mismatched format: {params} vs {cur}")
            frames.append(wf.readframes(wf.getnframes()))

    out_buf = io.BytesIO()
    with wave.open(out_buf, "wb") as out_wf:
        nchannels, sampwidth, framerate = params
        out_wf.setnchannels(nchannels)
        out_wf.setsampwidth(sampwidth)
        out_wf.setframerate(framerate)
        for f in frames:
            out_wf.writeframes(f)
    return out_buf.getvalue()


@app.task(name="tasks.run_qwen3_tts")
def run_qwen3_tts(prev_result: list) -> list:
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    preset = _get_tts_preset(speaker)
    ref_audio_path = preset.get("ref_audio_path", getattr(config, "QWEN3_TTS_REF_AUDIO_PATH", None))
    ref_text = preset.get("ref_text", getattr(config, "QWEN3_TTS_REF_TEXT", None))
    language = preset.get("language", getattr(config, "QWEN3_TTS_LANGUAGE", "Auto"))
    model = preset.get("model", getattr(config, "QWEN3_TTS_MODEL", None))

    if not ref_audio_path or not ref_text:
        raise ValueError(
            f"No reference audio/transcript configured for speaker '{speaker}'. "
            f"Set VOICE_PRESETS[...]['tts']['qwen3_tts_voice_clone']['ref_audio_path'/'ref_text'] "
            f"or the QWEN3_TTS_REF_AUDIO_PATH/QWEN3_TTS_REF_TEXT defaults in config."
        )

    qwen_url = config.QWEN3_TTS_URL
    timeout = getattr(config, "QWEN3_TTS_TIMEOUT", 120)
    batch_size = max(1, getattr(config, "QWEN3_TTS_MAX_BATCH_SENTENCES", 16))

    sentences = _split_sentences(text) or [text]
    log.info(
        "tts: seq=%d speaker=%s ref=%s lang=%s %d sentence(s), batch_size=%d",
        seq_num, speaker, ref_audio_path, language, len(sentences), batch_size,
    )

    prompt_id = _get_or_create_prompt_id(qwen_url, ref_audio_path, ref_text, model)

    all_wav_blobs: list[bytes] = []
    for batch in _chunked(sentences, batch_size):
        blobs, prompt_id = _generate_batch(
            qwen_url, batch, language, prompt_id, ref_audio_path, ref_text, model, timeout,
        )
        all_wav_blobs.extend(blobs)

    wav_bytes = _concat_wavs(all_wav_blobs)

    os.makedirs(config.TTS_FILES_DIR, exist_ok=True)
    wav_path = f"{config.TTS_FILES_DIR}/qwen3_{seq_num}.wav"
    with open(wav_path, "wb") as f:
        f.write(wav_bytes)

    return [wav_path, seq_num]