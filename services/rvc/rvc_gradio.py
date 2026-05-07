import requests
import logging

import config

from tasks.rvc_tasks import run_rvc_gradio
from .rvc import RVC

log = logging.getLogger(__name__)

class RVCGradio(RVC):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_voice()
        self.task = run_rvc_gradio

    def _init_voice(self):
        """Load the RVC voice model so subsequent conversions use it."""
        payload = {
            "data": [
                config.RVC_MODEL,
                config.RVC_PROTECT_1,
                config.RVC_PROTECT_2,
            ],
            "session_hash": "cantread_pipeline",
        }
        r = requests.post(
            f"{config.RVC_URL}/api/infer_change_voice",
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        log.info("RVC voice initialised: %s → %s", config.RVC_MODEL, result)
