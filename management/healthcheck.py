"""Verify that owocr/OBS, Kokoro, Ollama, and RVC services are reachable."""

import asyncio
import logging
import requests
import websockets

import config

log = logging.getLogger(__name__)


def check_owocr() -> None:
    """Verify owocr websocket is accepting connections."""
    async def _probe():
        async with websockets.connect(config.OWOCR_WS_URL, open_timeout=3):
            pass  # connection succeeded

    try:
        asyncio.get_event_loop().run_until_complete(_probe())
    except Exception:
        # Python ≥3.10 may not have a running loop yet
        asyncio.run(_probe())
    log.info("owocr websocket OK  (%s)", config.OWOCR_WS_URL)


def check_kokoro() -> None:
    """Verify Kokoro TTS Gradio server is responding."""
    r = requests.get(f"{config.KOKORO_URL}/v1/audio/voices", timeout=5)
    r.raise_for_status()
    log.info("Kokoro TTS OK  (%s)", config.KOKORO_URL)


def check_rvc() -> None:
    """Verify RVC Gradio server is responding."""
    r = requests.get(config.RVC_URL, timeout=5)
    r.raise_for_status()
    log.info("RVC OK  (%s)", config.RVC_URL)


def check_applio() -> None:
    """Verify Applio FastAPI server is responding."""
    r = requests.get(f"{config.APPLIO_URL}/docs", timeout=5)
    r.raise_for_status()
    log.info("Applio OK  (%s)", config.APPLIO_URL)


def _check_ollama_model(model_name: str, label: str) -> None:
    """Verify Ollama API is responding and *model_name* is available."""
    r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()
    models = [m["name"] for m in r.json().get("models", [])]
    model_base = model_name.split(":")[0]
    found = any(model_base in m for m in models)
    if not found:
        log.warning("Ollama %s model '%s' not found in available models: %s",
                    label, model_name, models)
    log.info("Ollama %s OK  (%s, model=%s)", label, config.OLLAMA_URL, model_name)


def check_ollama_cleanup() -> None:
    """Verify the Ollama cleanup model is reachable."""
    _check_ollama_model(config.OLLAMA_CLEANUP_MODEL, "cleanup")


def check_ollama_ocr() -> None:
    """Verify the Ollama vision-OCR model is reachable."""
    _check_ollama_model(config.OLLAMA_OCR_MODEL, "vision-OCR")


def check_obs() -> None:
    """Verify OBS websocket is accepting connections."""
    import obsws_python as obs_ws
    client = obs_ws.ReqClient(
        host=config.OBS_HOST,
        port=config.OBS_PORT,
        password=config.OBS_PASSWORD or None,
    )
    ver = client.get_version()
    client.disconnect()
    log.info("OBS OK  (%s:%d, obs-ws %s)",
             config.OBS_HOST, config.OBS_PORT,
             getattr(ver, 'obs_web_socket_version', '?'))


def run_all_checks() -> None:
    """Run every health-check.  Raises on first failure."""

    # ─── Section name ───────────────────────────────────────────────────────
    config_dependency_map = {
        # Config key that has dependencies
        "OCR_PROVIDER": { # { convig value: { other_config_key: (option1, option2) } }
            "ollama": {
                "FRAME_CAPTURE_METHOD": ("obs_plugin", "obs_websocket"),
            },
            "ollama_plain": {
                "FRAME_CAPTURE_METHOD": ("obs_plugin", "obs_websocket"),
            },
            "owocr_send_frames": {
                "FRAME_CAPTURE_METHOD": ("obs_plugin", "obs_websocket"),
            }
        },
        "TTS_PROVIDER": {
            "applio": {
                "RVC_PROVIDER": ("applio",),
            },
        },
    }
    services = {
        "owocr": {
            "check": check_owocr,
            "condition": lambda cfg: cfg.OCR_PROVIDER in ("owocr_receive_only", "owocr_send_frames"),
        },
        "Kokoro TTS": {
            "check": check_kokoro,
            "condition": lambda cfg: cfg.TTS_PROVIDER == "kokoro_fastapi",
        },
        "RVC": {
            "check": check_rvc,
            "condition": lambda cfg: cfg.RVC_PROVIDER == "rvc_gradio",
        },
        "Applio": {
            "check": check_applio,
            "condition": lambda cfg: cfg.TTS_PROVIDER == "applio" or cfg.RVC_PROVIDER == "applio",
        },
        "Ollama cleanup": {
            "check": check_ollama_cleanup,
            "condition": lambda cfg: cfg.OLLAMA_TEXT_CLEANUP_ENABLED,
        },
        "Ollama vision-OCR": {
            "check": check_ollama_ocr,
            "condition": lambda cfg: cfg.OCR_PROVIDER in ("ollama", "ollama_plain"),
        },
        "OBS websocket": {
            "check": check_obs,
            "condition": lambda cfg: cfg.FRAME_CAPTURE_METHOD == "obs_websocket",
        },
    }

    # validate_config
    for config_key, value_map in config_dependency_map.items():
        config_value = getattr(config, config_key)
        if config_value in value_map:
            dependencies = value_map[config_value]
            for dep_key, valid_options in dependencies.items():
                dep_value = getattr(config, dep_key)
                if dep_value not in valid_options:
                    raise RuntimeError(
                        f"Invalid configuration: {config_key}='{config_value}' requires "
                        f"{dep_key} to be one of {valid_options}, but got '{dep_value}'"
                    )
    # Build the required services list
    required_services = []
    for name, info in services.items():
        if info["condition"](config):
            log.debug("Adding '%s' to health checks based on config conditions", name)
            required_services.append((name, info["check"]))
        else:
            log.debug("Skipping '%s' health check based on config conditions", name)

    # Run the checks
    log.info("Running health checks for %d services based on current configuration...", len(required_services))
    for name, check_fn in required_services:
        log.info("Checking %s...", name)
        try:
            check_fn()
        except Exception as exc:
            raise RuntimeError(f"{name} is not reachable: {exc}") from exc
    log.info("All services healthy — pipeline ready")
