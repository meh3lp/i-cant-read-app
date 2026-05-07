"""Celery task: Applio standalone RVC voice conversion."""
import logging
import os
import requests

from tasks.celery_app import app
import config

log = logging.getLogger(__name__)


@app.task(bind=True, name="tasks.run_applio_rvc", max_retries=5, default_retry_delay=5)
def run_applio_rvc(self, prev_result: list) -> list:
    """Run Applio RVC voice conversion on a TTS wav.

    *prev_result* is ``[wav_path, seq_num]`` from the TTS task.
    Returns ``[converted_wav_path, seq_num]``.
    """
    wav_path, seq_num = prev_result

    log.info("Applio RVC converting: %s", wav_path)

    os.makedirs(config.TTS_FILES_DIR, exist_ok=True)
    output_path = f"{config.TTS_FILES_DIR}/applio_rvc_{seq_num}.wav"

    payload = {
        "input_path": wav_path,
        "output_path": output_path,
        "pth_path": config.APPLIO_PTH_PATH,
        "index_path": config.APPLIO_INDEX_PATH,
        "index_rate": config.APPLIO_INDEX_RATE,
        "volume_envelope": config.APPLIO_VOLUME_ENVELOPE,
        "protect": config.APPLIO_PROTECT,
        "f0_method": config.APPLIO_F0_METHOD,
        "export_format": config.APPLIO_EXPORT_FORMAT,
        "split_audio": config.APPLIO_SPLIT_AUDIO,
    }
    if config.APPLIO_PROPOSED_PITCH:
        payload.update({
            "proposed_pitch": config.APPLIO_PROPOSED_PITCH,
            "proposed_pitch_threshold": config.APPLIO_PROPOSED_PITCH_THRESHOLD,
        })
    else:
        payload.update({
            "pitch": config.APPLIO_PITCH,
        })

    try:
        r = requests.post(
            f"{config.APPLIO_URL}/infer",
            json=payload,
            timeout=300,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Applio RVC request failed: %s", e)
        raise self.retry(exc=e)

    data = r.json()
    converted_path = data.get("output_path", output_path)

    # Cleanup intermediate TTS file
    try:
        os.remove(wav_path)
        log.debug("Removed intermediate TTS file: %s", wav_path)
    except Exception as e:
        log.warning("Failed to remove intermediate TTS file %s: %s", wav_path, e)

    log.info("applio rvc: seq=%d → %s", seq_num, converted_path)
    return [converted_path, seq_num]
