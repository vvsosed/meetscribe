"""Google Cloud Speech-to-Text v2 (chirp_3) adapter."""

from __future__ import annotations

import logging
import os
import queue as queue_module
import threading
from dataclasses import dataclass
from typing import Callable, Iterator

from google.api_core import exceptions as gexc

from .ports import Clock, SpeechSession
from .rotation import MAX_STREAM_SECONDS, StreamClock
from .types import BLOCK_MS, TARGET_RATE, Segment, Word
from .vad import SilenceGate

log = logging.getLogger(__name__)

BACKOFF_START_S = 2.0
BACKOFF_CAP_S = 30.0
ESCALATE_AFTER_FAILURES = 5

# Healthy streams are rotated at MAX_STREAM_SECONDS. A stream still open well
# past that is stalled - gRPC sets no deadline and no keepalive, so a
# black-holed connection would otherwise leave this track silently
# transcribing nothing for the rest of the meeting. DeadlineExceeded is not
# fatal, so EngineWorker reconnects.
STREAM_TIMEOUT_S = MAX_STREAM_SECONDS + 30

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

    @staticmethod
    def _dropped(audio_q) -> int:
        """Blocks the queue has discarded, when it counts them.

        A plain queue.Queue does not, so this reports 0 rather than failing.
        """
        return getattr(audio_q, "dropped", 0)

    def run(
        self,
        audio_q,
        out_q: queue_module.Queue,
        stop: threading.Event,
    ) -> None:
        stream_clock = StreamClock(max_stream_s=self._max_stream_s)
        backoff = BACKOFF_START_S
        consecutive_failures = 0
        dropped_at_success = self._dropped(audio_q)

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
                dropped_at_success = self._dropped(audio_q)
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
                lost_blocks = self._dropped(audio_q) - dropped_at_success
                lost = (
                    f"; {lost_blocks * BLOCK_MS / 1000:.0f}s of audio dropped "
                    "while offline"
                    if lost_blocks
                    else ""
                )
                log.log(
                    level,
                    "speech stream error (%s), retrying in %.0fs: %s%s",
                    self._track,
                    backoff,
                    exc,
                    lost,
                )
                self._clock.sleep(backoff)
                backoff = next_backoff(backoff)

            stream_clock = stream_clock.rotated(last_chunk_t)


def speech_endpoint(region: str) -> str:
    if region == "global":
        return "speech.googleapis.com"
    return f"{region}-speech.googleapis.com"


def recognizer_path(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}/recognizers/_"


@dataclass(frozen=True)
class GoogleConfig:
    project_id: str
    region: str = "eu"
    model: str = "chirp_3"
    language_codes: tuple[str, ...] = ("en-US",)
    phrases: tuple[str, ...] = ()
    interim: bool = True


class GoogleSpeechSession:
    """One StreamingRecognize call. Discarded and rebuilt on every rotation."""

    def __init__(self, config: GoogleConfig, client, stream_clock: StreamClock, track: str):
        self._config = config
        self._client = client
        self._clock = stream_clock
        self._track = track

    def _config_request(self):
        from google.cloud.speech_v2.types import cloud_speech as cs

        adaptation = None
        if self._config.phrases:
            adaptation = cs.SpeechAdaptation(
                phrase_sets=[
                    cs.SpeechAdaptation.AdaptationPhraseSet(
                        inline_phrase_set=cs.PhraseSet(
                            phrases=[
                                # 0-20, where high values start degrading general
                                # accuracy. 15 is aggressive enough for names and
                                # jargon without that trade-off.
                                cs.PhraseSet.Phrase(value=p, boost=15.0)
                                for p in self._config.phrases
                            ]
                        )
                    )
                ]
            )

        recognition_config = cs.RecognitionConfig(
            explicit_decoding_config=cs.ExplicitDecodingConfig(
                encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=TARGET_RATE,
                audio_channel_count=1,
            ),
            language_codes=list(self._config.language_codes),
            model=self._config.model,
            features=cs.RecognitionFeatures(
                enable_automatic_punctuation=True,
                enable_word_time_offsets=True,
            ),
            **({"adaptation": adaptation} if adaptation else {}),
        )

        streaming_config = cs.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=cs.StreamingRecognitionFeatures(
                interim_results=self._config.interim,
            ),
        )
        return cs.StreamingRecognizeRequest(
            recognizer=recognizer_path(self._config.project_id, self._config.region),
            streaming_config=streaming_config,
        )

    def stream(self, pcm: Iterator[bytes]) -> Iterator[Segment]:
        from google.cloud.speech_v2.types import cloud_speech as cs

        def requests():
            yield self._config_request()
            for block in pcm:
                yield cs.StreamingRecognizeRequest(audio=block)

        for response in self._client.streaming_recognize(
            requests=requests(), timeout=STREAM_TIMEOUT_S
        ):
            for result in response.results:
                segment = segment_from_result(result, self._clock, self._track)
                if segment is not None:
                    yield segment


def build_session_factory(config: GoogleConfig, track: str) -> SessionFactory:
    """Create the factory the EngineWorker calls on every rotation."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient

    client = SpeechClient(
        client_options=ClientOptions(api_endpoint=speech_endpoint(config.region))
    )

    def factory(stream_clock: StreamClock) -> SpeechSession:
        return GoogleSpeechSession(config, client, stream_clock, track)

    return factory


def project_from_environment() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "No GCP project. Pass --project or set GOOGLE_CLOUD_PROJECT, and "
            "point GOOGLE_APPLICATION_CREDENTIALS at a service account key."
        )
    return project
