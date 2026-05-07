"""Celery application for the cantread TTS pipeline."""

from celery import Celery

import config

app = Celery(
    "cantread",
    broker=config.REDIS_URL,
    backend=config.REDIS_URL,
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,     # one task at a time per worker process
)

app.autodiscover_tasks([
    "tasks"
])
