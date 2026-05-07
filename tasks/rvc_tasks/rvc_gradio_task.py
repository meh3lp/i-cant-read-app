"""Celery task: RVC voice conversion."""
import logging
import os
import requests

from tasks.celery_app import app
import config

log = logging.getLogger(__name__)


@app.task(bind=True, name="tasks.run_rvc_gradio", max_retries=5, default_retry_delay=5)
def run_rvc_gradio(self, prev_result: list) -> list:
    """Run RVC voice conversion on the TTS wav.

    *prev_result* is ``[wav_path, seq_num]`` from the TTS task.
    Returns ``[converted_wav_path, seq_num]``.
    """
    wav_path, seq_num = prev_result

    log.info("RVC converting: %s", wav_path)

    payload = {
        "fn_index": 2,
        "session_hash": "cantread_pipeline",
        "data": [
            config.RVC_TRANSPOSE,        # 0  – speaker id
            wav_path,                   # 1  – input audio path
            5,                           # 2  – Transpose
            None,                        # 3  – F0 curve file (must be None)
            config.RVC_F0_METHOD,        # 4  – f0 method
            config.RVC_INDEX,            # 5  – index file path
            "null",                      # 6  – autodetect index path, unused
            0.75,                        # 7  - search feature ratio
            3,                           # 8  - Apply median filtering
            0,                           # 9  - Resample result
            0.25,                        # 10 - Volume envelope scaling
            0.33,                        # 11 – Protect voiceless consonants
        ],
    }

    r = requests.post(
        f"{config.RVC_URL}/run/predict",
        json=payload,
        timeout=120,
    )
    try:
        r.raise_for_status()
    except Exception as e:
        log.error("RVC request failed: %s %s", r.status_code, r.text)
        raise self.retry(exc=e)

    data = r.json()
    log.debug("RVC raw response: %s", data)

    # RVC returns {"data": [info_str, (wav_path_or_dict, ...)]}
    # The second element in data is the audio output.
    audio_out = data["data"][1]
    log.debug("RVC audio_out type=%s value=%s", type(audio_out).__name__, audio_out)

    # Depending on RVC version it may be a plain path string
    # or a dict like {"name": "/tmp/...", "data": null, "is_file": true}
    if isinstance(audio_out, dict):
        converted_path = audio_out.get("name") or audio_out.get("path", "")
    elif isinstance(audio_out, (list, tuple)):
        # Some versions return (path, sample_rate)
        converted_path = audio_out[0] if audio_out else ""
        if isinstance(converted_path, dict):
            converted_path = converted_path.get("name") or converted_path.get("path", "")
    else:
        converted_path = str(audio_out)

    # Cleanup: Remove source audio file
    try:
        os.remove(wav_path)
        log.debug("Removed intermediate TTS file: %s", wav_path)
    except Exception as e:
        log.warning("Failed to remove intermediate TTS file %s: %s", wav_path, e)

    log.info("rvc: seq=%d → %s", seq_num, converted_path)
    return [converted_path, seq_num]
