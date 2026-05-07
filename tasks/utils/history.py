"""Universal pipeline history backed by a Redis Hash.

Every dispatched replica gets a history entry written *before* its
processing chain is launched.  Downstream tasks (dedup, filter, cleanup)
read from the history — they never write text data, only status updates.

Redis key: ``cantread:history`` (Hash)
  field = ``str(seq_num)``
  value = JSON-encoded entry dict

Entry schema::

    {
        "text":          str,          # original OCR text (immutable)
        "cleaned_text":  str | None,   # set by cleanup task
        "seq":           int,
        "speaker":       str,
        "batch":         int,
        "status":        str,          # initialized | processing | queued | dropped
        "created_at":    float,        # time.time() when dispatcher wrote entry
        "processed_at":  float | None, # set when status becomes queued/dropped
    }
"""

import json
import logging
import time

import redis as _redis

import config

log = logging.getLogger(__name__)


class PipelineHistory:
    """Read/write interface to the universal pipeline history hash."""

    def __init__(self, redis_client: _redis.Redis) -> None:
        self._r = redis_client

    @staticmethod
    def _key() -> str:
        return getattr(config, "HISTORY_HASH_KEY", "cantread:history")

    # ── writes (dispatchers only) ────────────────────────────────────────

    def write_entry(
        self,
        seq: int,
        text: str,
        speaker: str,
        batch: int,
    ) -> None:
        """Create a new history entry with status ``initialized``."""
        entry = {
            "text": text,
            "cleaned_text": None,
            "seq": seq,
            "speaker": speaker,
            "batch": batch,
            "status": "initialized",
            "created_at": time.time(),
            "processed_at": None,
        }
        self._r.hset(self._key(), str(seq), json.dumps(entry))

    # ── status updates (tasks) ───────────────────────────────────────────

    def update_status(self, seq: int, status: str) -> None:
        """Update the status of an existing entry.

        Terminal statuses (``queued``, ``dropped``) also set
        ``processed_at``.
        """
        raw = self._r.hget(self._key(), str(seq))
        if raw is None:
            log.warning("history: no entry for seq=%d to update", seq)
            return
        entry = json.loads(raw)
        entry["status"] = status
        if status in ("queued", "dropped"):
            entry["processed_at"] = time.time()
        self._r.hset(self._key(), str(seq), json.dumps(entry))

    def update_cleaned_text(self, seq: int, cleaned_text: str) -> None:
        """Store the cleaned text produced by the cleanup task."""
        raw = self._r.hget(self._key(), str(seq))
        if raw is None:
            log.warning("history: no entry for seq=%d to update", seq)
            return
        entry = json.loads(raw)
        entry["cleaned_text"] = cleaned_text
        self._r.hset(self._key(), str(seq), json.dumps(entry))

    # ── reads (tasks) ────────────────────────────────────────────────────

    def get_entry(self, seq: int) -> dict | None:
        """Return a single entry or ``None``."""
        raw = self._r.hget(self._key(), str(seq))
        if raw is None:
            return None
        return json.loads(raw)

    def get_entries_before(
        self,
        seq: int,
        *,
        speaker: str | None = None,
        limit: int | None = None,
        exclude_dropped: bool = False,
    ) -> list[dict]:
        """Return history entries with ``entry.seq < seq``.

        Results are ordered oldest-first (ascending seq).

        Parameters
        ----------
        speaker : str, optional
            If set, only return entries from this speaker.
        limit : int, optional
            If set, return at most the *limit* most recent matching
            entries (still oldest-first).
        exclude_dropped : bool
            If ``True``, skip entries whose status is ``"dropped"``.
        """
        if seq <= 0:
            return []

        # Determine the range of keys to fetch.  We read from the
        # Redis Hash using HMGET on sequential integer keys.
        # To honour `limit` efficiently, we scan backwards from
        # seq-1, collecting entries until we have enough.
        collected: list[dict] = []
        # Scan in chunks to avoid huge HMGET calls for very large seqs
        chunk = min(seq, max(limit or 50, 50))
        cursor = seq - 1

        while cursor >= 0 and (limit is None or len(collected) < limit):
            start = max(cursor - chunk + 1, 0)
            fields = [str(i) for i in range(start, cursor + 1)]
            if not fields:
                break
            raw_values = self._r.hmget(self._key(), *fields)

            # Process in reverse (newest first) so we can stop early
            for raw in reversed(raw_values):
                if raw is None:
                    continue
                entry = json.loads(raw)
                if exclude_dropped and entry.get("status") == "dropped":
                    continue
                if speaker is not None and entry.get("speaker") != speaker:
                    continue
                collected.append(entry)
                if limit is not None and len(collected) >= limit:
                    break

            cursor = start - 1

        # collected is newest-first; reverse to oldest-first
        collected.reverse()
        return collected

    # ── batch helper ─────────────────────────────────────────────────────

    @staticmethod
    def next_batch(r: _redis.Redis) -> int:
        """Atomically claim the next batch number."""
        key = getattr(config, "BATCH_COUNTER_KEY", "cantread:batch_counter")
        return int(r.incr(key)) - 1
