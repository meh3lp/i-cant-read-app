"""OCR deduplication: segment-based removal of old text from OCR results.

When a vision-based OCR captures text from a VN screen, consecutive
frames often contain overlapping text — the same paragraph stays on
screen while new lines appear at the bottom.  This module compares each
new OCR result against a Redis-backed sliding window of recent results
and returns only the genuinely new segments.

Algorithm
---------
1. **Whole-text near-dup** — if the entire text closely matches a recent
   entry, skip it (re-scan of the same screen).
2. **Segment comparison** — strip UI noise (e.g. "Divergent Universe"),
   split into sentence-sized segments, fuzzy-compare each against *all*
   segments from the window, and keep only segments that are new.

The segment comparison uses both ``SequenceMatcher.ratio()`` and a
*containment* check (fraction of the shorter segment covered by matching
blocks) so that partial OCR fragments like "One we can never return to
— the Golden Age." are still recognised as old when the window contains
the full sentence "It was an epoch beyond comprehension, one we can
never return to — The Golden Age."
"""

import logging
import re
from difflib import SequenceMatcher

import config

log = logging.getLogger(__name__)

# Sentence boundary: splits after . ! ? followed by whitespace.
# Ellipses (... and …) are excluded — they're trailing-off pauses in
# dialogue, not sentence boundaries, and splitting on them creates tiny
# orphan segments that cause false-positive dedup matches.
_SENT_RE = re.compile(r"(?<=[.!?…])(?<!\.\.)(?<!…)\s+")


# ── helpers ──────────────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace to single spaces."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _strip_noise(text: str) -> str:
    """Remove known UI noise phrases from *text*.

    **Long phrases** (≥ 8 chars, e.g. "Divergent Universe") are replaced
    inline with a newline wherever they appear — they're distinctive
    enough not to collide with real dialogue.

    **Short phrases** (3-7 chars, e.g. "You", "Skip") are only removed
    when they appear on their own line (possibly with surrounding
    whitespace) — matching them inside running prose would corrupt
    legitimate text like "Since you wish to…".

    Entries shorter than 3 chars are skipped entirely.
    """
    blocklist: list[str] = getattr(config, "TEXT_FILTER_UI_BLOCKLIST", [])
    for phrase in blocklist:
        if len(phrase) >= 8:
            # Long/distinctive — safe to strip inline.
            # Lookaround protects contractions (apostrophe) and adjacent letters.
            pattern = r"(?<![a-zA-Z])" + re.escape(phrase) + r"(?![a-zA-Z'])"
            text = re.sub(pattern, "\n", text, flags=re.IGNORECASE)
        elif len(phrase) >= 3:
            # Short — only strip when it's the entire line (with optional
            # surrounding whitespace).  This avoids corrupting words like
            # "you" inside "Since you wish to…".
            pattern = r"^\s*" + re.escape(phrase) + r"\s*$"
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
    return text


def _split_segments(text: str, min_len: int = 10) -> list[str]:
    """Split *text* into segments on paragraph breaks and sentence boundaries.

    Short fragments (< *min_len*) are merged with their neighbour rather
    than dropped outright — this prevents abbreviations like ``"Mr."``
    from causing the next word to be lost (e.g. ``"Svarog..."`` after
    splitting on ``"Mr."``).

    Only truly orphan fragments that remain short after merging are
    discarded.

    Single newlines are treated as OCR line-wrapping and collapsed into
    spaces.  Only double-newlines (``\\n\\n``) are treated as paragraph
    boundaries.
    """
    # Collapse single newlines (OCR line-wraps) into spaces.
    # Preserve double-newlines as paragraph separators.
    text = re.sub(r"\n{2,}", "\x00", text)   # temporarily protect paragraph breaks
    text = re.sub(r"\n", " ", text)           # single newline → space
    text = text.replace("\x00", "\n")         # restore paragraph breaks

    raw: list[str] = []
    for block in text.split("\n"):
        block = block.strip()
        if not block:
            continue
        sents = _SENT_RE.split(block)
        for s in sents:
            s = s.strip()
            if s:
                raw.append(s)

    if not raw:
        return []

    # Merge short fragments into their neighbour instead of dropping them.
    # This keeps "Mr. Svarog..." intact as one segment.
    merged = [raw[0]]
    for seg in raw[1:]:
        if len(merged[-1]) < min_len:
            # Previous fragment is too short — absorb current into it.
            merged[-1] = merged[-1] + " " + seg
        else:
            merged.append(seg)
    # If the last segment is still short, merge it back into the previous.
    if len(merged) > 1 and len(merged[-1]) < min_len:
        merged[-2] = merged[-2] + " " + merged[-1]
        merged.pop()

    return [s for s in merged if len(s) >= min_len]


def _check_segment(
    new_norm: str,
    old_norms: list[str],
    threshold: float,
) -> str | None:
    """Decide what to keep from *new_norm* given previously seen segments.

    Returns:
    * ``None`` — the segment is entirely old, discard it.
    * The full *new_norm* — the segment is entirely new.
    * A trimmed suffix — the segment starts with old content but has a
      genuinely new tail (the VN scrolling-accumulation pattern).

    Uses a **two-pass** strategy so that exact/near-exact ratio matches
    (strongest signal) are always checked before weaker containment
    heuristics.  Any extracted suffix is recursively re-validated to
    ensure it isn't itself old content.

    Pass 1 — ratio ≥ threshold → old (extract suffix if new is much
    longer).
    Pass 2 — contiguous containment ≥ 80 % of new or old → old /
    suffix.
    """
    # ── Pass 1: ratio-based matches (strongest signal) ────────────────
    for old in old_norms:
        sm = SequenceMatcher(None, old, new_norm)
        ratio = sm.ratio()

        if ratio >= threshold:
            # If new is substantially longer, the extra tail is new content.
            if len(new_norm) > len(old) * 1.15:
                suffix = _extract_new_suffix(old, new_norm)
                if suffix is not None:
                    # Re-validate: the suffix itself may be old.
                    return _check_segment(suffix, old_norms, threshold)
            return None

    # ── Pass 2: containment checks (weaker signal) ───────────────────
    if len(new_norm) >= 15:
        for old in old_norms:
            sm = SequenceMatcher(None, old, new_norm)
            match = sm.find_longest_match(0, len(old), 0, len(new_norm))

            # Forward: most of new is found verbatim in old.
            if match.size / len(new_norm) >= 0.80:
                return None

            # Reverse: most of old is embedded in new → new tail.
            if len(old) >= 15 and match.size / len(old) >= 0.80:
                # Guard: if the match starts ≥ 15 chars into new, the
                # two strings merely share a phrase (coincidental lexical
                # overlap like "understand the situation") rather than
                # exhibiting the VN scrolling-accumulation pattern where
                # old text anchors the beginning of new.  In that case
                # the new segment is genuinely new — skip this old entry.
                if match.b >= 15:
                    continue
                suffix = _extract_new_suffix(old, new_norm)
                if suffix is not None:
                    return _check_segment(suffix, old_norms, threshold)
                return None

    return new_norm


def _extract_new_suffix(old: str, new: str) -> str | None:
    """Return the trailing part of *new* that does not appear in *old*.

    Uses SequenceMatcher to find the longest contiguous overlap, then
    returns everything in *new* after that overlap.  Returns None if
    there's no meaningful new suffix.
    """
    sm = SequenceMatcher(None, old, new)
    # Get all matching blocks, find the one that ends latest in `new`.
    blocks = sm.get_matching_blocks()
    if not blocks:
        return new

    # The end of the last significant matching block in `new`.
    last_end_in_new = 0
    for block in blocks:
        if block.size >= 4:  # only care about non-trivial matches
            end = block.b + block.size
            if end > last_end_in_new:
                last_end_in_new = end

    suffix = new[last_end_in_new:].strip()
    if len(suffix) < 10:
        return None
    return suffix


# ── class ────────────────────────────────────────────────────────────────────


class OCRDedup:
    """Stateless deduplicator for OCR text.

    All state (the sliding window of previous texts) is provided by the
    caller, read from the universal pipeline history.
    """

    @staticmethod
    def dedup(text: str, previous_texts: list[str]) -> str | None:
        """Return deduplicated text, or *None* if fully duplicated.

        *previous_texts* is the ordered list of raw OCR texts from the
        pipeline history (oldest-first).
        """
        window = previous_texts

        if not window:
            return text

        dup_thresh = getattr(config, "OCR_DEDUP_SIMILARITY_THRESHOLD", 0.85)
        seg_thresh = getattr(config, "OCR_DEDUP_SEGMENT_THRESHOLD", 0.70)
        min_seg_len = getattr(config, "OCR_DEDUP_MIN_SEGMENT_LENGTH", 10)

        # ── Stage 1: whole-text near-duplicate ────────────────────────
        norm_text = _normalize(text)
        for prev in reversed(window):
            norm_prev = _normalize(prev)
            sim = SequenceMatcher(None, norm_prev, norm_text).ratio()
            if sim >= dup_thresh:
                # Before skipping, check if the new text has a genuinely
                # new tail (VN scrolling: old falls off the top, new
                # appears at the bottom, overall similarity stays high).
                suffix = _extract_new_suffix(norm_prev, norm_text)
                if suffix is not None:
                    log.info(
                        "ocr_dedup: near-dup (sim=%.2f) but new tail "
                        "detected — falling through to segment check",
                        sim,
                    )
                    break
                log.info("ocr_dedup: near-dup (sim=%.2f) — skip", sim)
                return None

        # ── Stage 2: segment-based overlap extraction ─────────────────
        cleaned = _strip_noise(text)
        new_segments = _split_segments(cleaned, min_len=min_seg_len)

        if not new_segments:
            log.info("ocr_dedup: no segments after noise removal — skip")
            return None

        # Collect normalised segments from *all* window entries.
        old_norm_segs: list[str] = []
        for prev in window:
            for seg in _split_segments(_strip_noise(prev), min_len=min_seg_len):
                old_norm_segs.append(_normalize(seg))

        if not old_norm_segs:
            # Window entries had no usable segments — pass through.
            return text

        kept: list[str] = []
        for seg in new_segments:
            result = _check_segment(_normalize(seg), old_norm_segs, seg_thresh)
            if result is not None:
                kept.append(result)

        if not kept:
            log.info("ocr_dedup: all segments are old — skip")
            return None

        result = " ".join(kept).strip()
        log.info(
            "ocr_dedup: kept %d/%d segments: %s",
            len(kept),
            len(new_segments),
            result[:80],
        )
        return result or None
