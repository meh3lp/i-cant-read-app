"""Celery task: Applio TTS + RVC voice conversion (combined)."""
import logging
import os
import requests

from tasks.celery_app import app
import config

log = logging.getLogger(__name__)


def _get_tts_preset(speaker: str) -> dict:
    """Look up Applio TTS preset for *speaker*."""
    presets = getattr(config, "VOICE_PRESETS", {})
    entry = presets.get(speaker, presets.get("default", {}))
    tts = entry.get("tts", {})
    return tts.get("applio", {})


@app.task(bind=True, name="tasks.run_applio_tts", max_retries=5, default_retry_delay=5)
def run_applio_tts(self, prev_result: list) -> list:
    """Synthesize text via Applio EdgeTTS + RVC in one call.

    *prev_result* is ``[replica_dict, seq_num]``.
    Returns ``[converted_wav_path, seq_num]``.
    """
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    preset = _get_tts_preset(speaker)
    voice = preset.get("voice", config.APPLIO_TTS_VOICE)
    rate = preset.get("rate", config.APPLIO_TTS_RATE)

    os.makedirs(config.TTS_FILES_DIR, exist_ok=True)
    output_tts_path = f"{config.TTS_FILES_DIR}/applio_tts_{seq_num}.wav"
    output_rvc_path = f"{config.TTS_FILES_DIR}/applio_rvc_{seq_num}.wav"

    log.info("applio tts: seq=%d speaker=%s voice=%s", seq_num, speaker, voice)

    payload = {
        "tts_text": text,
        "tts_voice": voice,
        "tts_rate": rate,
        "output_tts_path": output_tts_path,
        "output_rvc_path": output_rvc_path,
        "pth_path": config.APPLIO_PTH_PATH,
        "index_path": config.APPLIO_INDEX_PATH,
        "pitch": config.APPLIO_PITCH,
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
            f"{config.APPLIO_URL}/tts",
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Applio TTS request failed: %s", e)
        raise self.retry(exc=e)

    data = r.json()
    converted_path = data.get("output_path", output_rvc_path)

    log.info("applio tts: seq=%d → %s", seq_num, converted_path)
    return [converted_path, seq_num]
