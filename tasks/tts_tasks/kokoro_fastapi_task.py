import logging
import os
import requests

from tasks.celery_app import app
import config

log = logging.getLogger(__name__)


def _get_tts_preset(speaker: str) -> dict:
    """Look up Kokoro TTS preset for *speaker*.

    Falls back to the ``"default"`` preset, then to empty dict
    (which causes the task to use global config values).
    """
    presets = getattr(config, "VOICE_PRESETS", {})
    entry = presets.get(speaker, presets.get("default", {}))
    tts = entry.get("tts", {})
    return tts.get("kokoro_fastapi", {})


@app.task(name="tasks.run_kokoro_fastapi")
def run_kokoro_fastapi(prev_result: list) -> list:
    replica, seq_num = prev_result
    text = replica["text"]
    speaker = replica.get("speaker", "Narrator")

    preset = _get_tts_preset(speaker)
    voice = preset.get("voice", config.KOKORO_VOICE)
    speed = preset.get("speed", config.KOKORO_SPEED)

    log.info("tts: seq=%d speaker=%s voice=%s speed=%s", seq_num, speaker, voice, speed)

    response = requests.post(
        f"{config.KOKORO_URL}/v1/audio/speech",
        json={
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "speed": speed,
        }
    )

    # make sure /dev/shm/cantread/tts exists
    os.makedirs(config.TTS_FILES_DIR, exist_ok=True)
    wav_path = f"{config.TTS_FILES_DIR}/kokoro_{seq_num}.wav"

    with open(wav_path, "wb") as f:
        f.write(response.content)

    return [wav_path, seq_num]
