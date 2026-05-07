import logging

from tasks.rvc_tasks import run_applio_rvc
from .rvc import RVC

log = logging.getLogger(__name__)


class RVCApplio(RVC):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.task = run_applio_rvc
