"""Google Cloud Speech-to-Text v2 (chirp_3) adapter."""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from typing import Callable, Iterator

from google.api_core import exceptions as gexc

from .ports import Clock, SpeechSession
from .rotation import MAX_STREAM_SECONDS, StreamClock
from .types import Segment, Word
from .vad import SilenceGate

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

    # Direct attribute access, not getattr with a default: on a real protobuf
    # these fields are always present, so a default could only ever mask an
    # upstream rename - turning a loud AttributeError into 0.0 timestamps
    # written straight to the durable JSONL.
    end = clock.absolute(duration_seconds(result.result_end_offset))
    words = tuple(
        Word(
            word=w.word,
            start=clock.absolute(duration_seconds(w.start_offset)),
            end=clock.absolute(duration_seconds(w.end_offset)),
        )
        for w in (alternative.words or ())
    )

    return Segment(
        track=track,
        text=text,
        is_final=bool(result.is_final),
        t_start=words[0].start if words else end,
        t_end=end,
        confidence=alternative.confidence or None,
        words=words,
    )


def is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, FATAL_ERRORS)


def next_backoff(previous: float) -> float:
    return min(previous * 2, BACKOFF_CAP_S)


SessionFactory = Callable[[StreamClock], SpeechSession]


class EngineWorker:
    """Drives one track's audio through rotating recognition streams."""

    def __init__(
        self,
        track: str,
        session_factory: SessionFactory,
        gate: SilenceGate,
        clock: Clock,
        max_stream_s: float = MAX_STREAM_SECONDS,
    ):
        self._track = track
        self._factory = session_factory
        self._gate = gate
        self._clock = clock
        self._max_stream_s = max_stream_s

    def run(
        self,
        audio_q,
        out_q: queue_module.Queue,
        stop: threading.Event,
    ) -> None:
        stream_clock = StreamClock(max_stream_s=self._max_stream_s)
        backoff = BACKOFF_START_S
        consecutive_failures = 0

        while not stop.is_set():
            last_chunk_t = stream_clock.offset
            started = self._clock.monotonic()

            def blocks() -> Iterator[bytes]:
                nonlocal last_chunk_t
                while not stop.is_set():
                    if stream_clock.should_rotate(self._clock.monotonic() - started):
                        log.debug("rotating %s stream", self._track)
                        return
                    try:
                        chunk = audio_q.get(timeout=0.25)
                    except queue_module.Empty:
                        continue
                    last_chunk_t = chunk.t_start
                    if self._gate.allows(chunk.pcm):
                        yield chunk.pcm

            try:
                # No stop check inside this loop. blocks() already returns
                # when stop is set, which ends the stream on its own, and
                # breaking out here would discard finals the engine emitted
                # on the way out - exactly the ones cli.py drains out_q for
                # after Ctrl-C.
                for segment in self._factory(stream_clock).stream(blocks()):
                    out_q.put(segment)
                consecutive_failures = 0
                backoff = BACKOFF_START_S
            except Exception as exc:
                if stop.is_set():
                    break
                if is_fatal(exc):
                    log.error(
                        "speech configuration error (%s), not retryable: %s",
                        self._track,
                        exc,
                    )
                    stop.set()
                    return
                consecutive_failures += 1
                level = (
                    logging.ERROR
                    if consecutive_failures >= ESCALATE_AFTER_FAILURES
                    else logging.WARNING
                )
                log.log(
                    level,
                    "speech stream error (%s), retrying in %.0fs: %s",
                    self._track,
                    backoff,
                    exc,
                )
                self._clock.sleep(backoff)
                backoff = next_backoff(backoff)

            stream_clock = stream_clock.rotated(last_chunk_t)
