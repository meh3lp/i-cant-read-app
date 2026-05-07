"""Pre-LLM text filter: noise rejection, fuzzy dedup, and overlap extraction.

Sits between the owocr listener and the pipeline to reduce redundant or
garbled OCR text before it reaches the (expensive) Ollama cleanup step.

State (the sliding window of recent texts) is stored in Redis so that
multiple Celery workers can share the same dedup context.
"""

import logging
import re
from difflib import SequenceMatcher

import config

log = logging.getLogger(__name__)

# Sentence boundary regex — splits after . ! ? followed by whitespace
_SENT_RE = re.compile(r"(?<=[.!?])\s+")

# A token is "readable" if it's ≥3 ASCII letters, contains a vowel,
# and has sane casing (all-lower, all-upper, or Title Case — not "OmVETSE").
_VOWELS = set("aeiouAEIOU")
_SANE_CASE_RE = re.compile(
    r"^(?:[A-Z][a-z]+|[a-z]+|[A-Z]+)$"  # Title | lower | UPPER
)
# Common English words shorter than 3 characters — always count as readable.
_SHORT_WORDS = {
    "a", "i", "am", "an", "as", "at", "be", "by", "do", "go", "he", "if",
    "in", "is", "it", "me", "my", "no", "of", "on", "or", "ox", "so", "to",
    "up", "us", "we",
}


def _is_readable_token(tok: str) -> bool:
    """Return True if *tok* looks like a plausible English word."""
    # Strip trailing punctuation for the check
    core = tok.rstrip(".,;:!?'\"-")
    if core.lower() in _SHORT_WORDS:
        return True
    if len(core) < 3:
        return False
    if not core.isalpha():
        return False
    if not any(c in _VOWELS for c in core):
        return False
    if not _SANE_CASE_RE.match(core):
        return False
    return True


class TextFilter:
    """Stateless filter that deduplicates and cleans incoming OCR text.

    All state (the sliding window of previous texts) is provided by the
    caller, read from the universal pipeline history.
    """

    @staticmethod
    def filter(text: str, previous_texts: list[str]) -> str | None:
        """Return cleaned text or *None* if the text should be dropped.

        *previous_texts* is the ordered list of recent texts from the
        pipeline history (oldest-first).
        """
        if not getattr(config, "TEXT_FILTER_ENABLED", True):
            return text

        # Stage A — noise / garbled rejection
        if TextFilter._is_noise(text):
            log.debug("filter: rejected as noise: %s", text[:60])
            return None

        # Stage B — fuzzy dedup against recent window
        if TextFilter._is_fuzzy_duplicate(text, previous_texts):
            log.debug("filter: rejected as fuzzy dup: %s", text[:60])
            return None

        # Stage C — overlap extraction (keep only new sentences)
        text = TextFilter._extract_new_content(text, previous_texts)
        if not text:
            log.debug("filter: nothing new after overlap removal")
            return None

        return text

    # ── Stage A: noise rejection ─────────────────────────────────────────

    @staticmethod
    def _is_noise(text: str) -> bool:
        min_len = getattr(config, "TEXT_FILTER_MIN_LENGTH", 15)
        blocklist: list[str] = getattr(config, "TEXT_FILTER_UI_BLOCKLIST", [])

        # Exact blocklist match (case-insensitive)
        stripped = text.strip()
        for entry in blocklist:
            if stripped.lower() == entry.lower():
                return True

        # Short text with low "readable" token ratio → likely garbled
        if len(stripped) < min_len:
            return True

        tokens = stripped.split()
        if not tokens:
            return True

        readable = sum(1 for t in tokens if _is_readable_token(t))
        ratio = readable / len(tokens)

        # For very short texts, require high readability
        if len(stripped) < 40 and ratio <= 0.5:
            return True

        # For any length, reject if almost nothing is readable
        if ratio < 0.3:
            return True

        return False

    # ── Stage B: fuzzy dedup ─────────────────────────────────────────────

    @staticmethod
    def _is_fuzzy_duplicate(text: str, previous_texts: list[str]) -> bool:
        threshold = getattr(config, "TEXT_FILTER_SIMILARITY_THRESHOLD", 0.85)
        for prev in previous_texts:
            sim = SequenceMatcher(None, prev, text).ratio()
            if sim >= threshold:
                # Allow through if the new text is substantially longer
                # (it may contain genuinely new content appended)
                if len(text) > len(prev) * 1.3:
                    return False
                log.debug(
                    "filter: sim=%.2f with prev (len %d vs %d)",
                    sim, len(prev), len(text),
                )
                return True
        return False

    # ── Stage C: overlap extraction ──────────────────────────────────────

    @staticmethod
    def _extract_new_content(text: str, previous_texts: list[str]) -> str | None:
        """If *text* overlaps heavily with a recent entry,
        return only the sentences that are new."""
        if not previous_texts:
            return text

        overlap_thresh = getattr(config, "TEXT_FILTER_OVERLAP_THRESHOLD", 0.5)
        sent_sim_thresh = 0.8  # per-sentence similarity for removal

        # Compare against the most recent entry only
        prev = previous_texts[-1]
        sim = SequenceMatcher(None, prev, text).ratio()

        if sim < overlap_thresh:
            return text  # not enough overlap to warrant extraction

        # Split both into sentences
        prev_sents = [s.strip() for s in _SENT_RE.split(prev) if s.strip()]
        new_sents = [s.strip() for s in _SENT_RE.split(text) if s.strip()]

        if not new_sents:
            return text

        # Keep sentences from *text* that don't closely match any in *prev*
        kept: list[str] = []
        for ns in new_sents:
            is_old = any(
                SequenceMatcher(None, ps, ns).ratio() >= sent_sim_thresh
                for ps in prev_sents
            )
            if not is_old:
                kept.append(ns)

        if not kept:
            return None

        result = " ".join(kept).strip()
        if not result:
            return None

        log.info("filter: extracted new content: %s", result[:80])
        return result
