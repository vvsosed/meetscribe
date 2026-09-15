"""Google Cloud Speech-to-Text v2 (chirp_3) adapter."""

from __future__ import annotations

import logging

from google.api_core import exceptions as gexc

from .rotation import StreamClock
from .types import Segment, Word

log = logging.getLogger(__name__)

BACKOFF_START_S = 2.0
BACKOFF_CAP_S = 30.0
ESCALATE_AFTER_FAILURES = 5

# Retrying any of these is pointless: the configuration or the credentials are
# wrong and will stay wrong.
FATAL_ERRORS = (
    gexc.Unauthenticated,
    gexc.PermissionDenied,
    gexc.InvalidArgument,
    gexc.NotFound,
)


def duration_seconds(value) -> float:
    """protobuf Duration or timedelta -> float seconds. Tolerates None."""
    if value is None:
        return 0.0
    total_seconds = getattr(value, "total_seconds", None)
    return float(total_seconds()) if callable(total_seconds) else 0.0


def segment_from_result(result, clock: StreamClock, track: str) -> Segment | None:
    """Map one recognition result onto the session timeline.

    Returns None for results with nothing usable in them.
    """
    alternatives = getattr(result, "alternatives", None) or []
    if not alternatives:
        return None

    alternative = alternatives[0]
    text = (alternative.transcript or "").strip()
    if not text:
        return None

    end = clock.absolute(duration_seconds(getattr(result, "result_end_offset", None)))
    words = tuple(
        Word(
            word=w.word,
            start=clock.absolute(duration_seconds(w.start_offset)),
            end=clock.absolute(duration_seconds(w.end_offset)),
        )
        for w in (getattr(alternative, "words", None) or ())
    )

    return Segment(
        track=track,
        text=text,
        is_final=bool(getattr(result, "is_final", False)),
        t_start=words[0].start if words else end,
        t_end=end,
        confidence=getattr(alternative, "confidence", None) or None,
        words=words,
    )


def is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, FATAL_ERRORS)


def next_backoff(previous: float) -> float:
    return min(previous * 2, BACKOFF_CAP_S)
