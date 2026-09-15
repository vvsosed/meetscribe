from datetime import timedelta
from types import SimpleNamespace

from google.api_core import exceptions as gexc

from meetscribe.google import (
    BACKOFF_CAP_S,
    BACKOFF_START_S,
    duration_seconds,
    is_fatal,
    next_backoff,
    segment_from_result,
)
from meetscribe.rotation import StreamClock


def word(text, start, end):
    return SimpleNamespace(
        word=text,
        start_offset=timedelta(seconds=start),
        end_offset=timedelta(seconds=end),
    )


def result(transcript, *, is_final=True, end=1.0, words=(), confidence=0.9):
    alternative = SimpleNamespace(
        transcript=transcript, confidence=confidence, words=list(words)
    )
    return SimpleNamespace(
        alternatives=[alternative],
        is_final=is_final,
        result_end_offset=timedelta(seconds=end),
    )


def test_duration_of_none_is_zero():
    assert duration_seconds(None) == 0.0


def test_duration_reads_total_seconds():
    assert duration_seconds(timedelta(seconds=2, milliseconds=500)) == 2.5


def test_maps_a_result_onto_the_session_timeline():
    clock = StreamClock(offset=300.0)
    seg = segment_from_result(
        result("hello there", end=4.0, words=[word("hello", 1.0, 2.0), word("there", 2.0, 4.0)]),
        clock,
        track="system",
    )

    assert seg.text == "hello there"
    assert seg.track == "system"
    assert seg.is_final is True
    # Stream-relative times get the rotation offset applied.
    assert seg.t_start == 301.0
    assert seg.t_end == 304.0
    assert [w.word for w in seg.words] == ["hello", "there"]
    # Every boundary, not just the first: reading end_offset as start_offset
    # would otherwise leave this test green.
    assert (seg.words[0].start, seg.words[0].end) == (301.0, 302.0)
    assert (seg.words[1].start, seg.words[1].end) == (302.0, 304.0)


def test_interim_results_have_no_words():
    clock = StreamClock()
    seg = segment_from_result(result("partial", is_final=False, end=2.0), clock, "mic")

    assert seg.is_final is False
    assert seg.words == ()
    # With no word timings an interim collapses to a point at its end offset.
    assert seg.t_start == seg.t_end == 2.0


def test_empty_transcripts_are_dropped():
    clock = StreamClock()

    assert segment_from_result(result("   "), clock, "mic") is None
    assert segment_from_result(SimpleNamespace(alternatives=[]), clock, "mic") is None


def test_text_is_stripped():
    seg = segment_from_result(result("  spaced  "), StreamClock(), "mic")

    assert seg.text == "spaced"


def test_credential_errors_are_fatal():
    # Retrying these on a 2-second loop spins forever printing the same thing.
    assert is_fatal(gexc.Unauthenticated("bad credentials")) is True
    assert is_fatal(gexc.PermissionDenied("no access")) is True
    assert is_fatal(gexc.InvalidArgument("bad region")) is True
    assert is_fatal(gexc.NotFound("no such recognizer")) is True


def test_transport_errors_are_retryable():
    assert is_fatal(gexc.ServiceUnavailable("try later")) is False
    assert is_fatal(gexc.DeadlineExceeded("slow")) is False
    assert is_fatal(ConnectionResetError()) is False


def test_backoff_doubles_up_to_a_cap():
    delays = [BACKOFF_START_S]
    for _ in range(6):
        delays.append(next_backoff(delays[-1]))

    assert delays[:4] == [2.0, 4.0, 8.0, 16.0]
    assert delays[-1] == BACKOFF_CAP_S


import queue
import threading

from google.api_core import exceptions as gexc

from meetscribe.google import EngineWorker
from meetscribe.types import BLOCK_BYTES, AudioChunk, Segment
from meetscribe.vad import SilenceGate
from tests.conftest import FakeClock

PCM = b"\x01" * BLOCK_BYTES


def seg(text, track="mic"):
    return Segment(track=track, text=text, is_final=True, t_start=0.0, t_end=1.0)


class ScriptedSession:
    """Fails on connect, or consumes blocks and yields canned segments."""

    def __init__(
        self,
        clock_state,
        *,
        segments=(),
        error=None,
        clock=None,
        tick=0.0,
        stop=None,
        stop_after=None,
    ):
        self.clock_state = clock_state
        self.segments = list(segments)
        self.error = error
        self.clock = clock
        self.tick = tick
        self.stop = stop
        self.stop_after = stop_after
        self.consumed: list[bytes] = []

    def stream(self, pcm):
        # A real transport error surfaces when the stream opens, before any
        # audio has flowed. Raising here keeps the tests from having to drain
        # the whole queue first.
        if self.error is not None:
            raise self.error
        for block in pcm:
            self.consumed.append(block)
            if self.clock is not None and self.tick:
                self.clock.advance(self.tick)
            if self.stop_after and len(self.consumed) >= self.stop_after:
                self.stop.set()
        yield from self.segments


def filled_queue(count: int, start: float = 0.0) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    for i in range(count):
        q.put(AudioChunk(track="mic", pcm=PCM, t_start=start + i))
    return q


def test_emits_segments_from_the_session():
    stop = threading.Event()
    sessions = []

    def factory(clock_state):
        session = ScriptedSession(clock_state, segments=[seg("hello"), seg("world")])
        sessions.append(session)
        stop.set()  # one pass is enough; blocks() then yields nothing
        return session

    out: queue.Queue = queue.Queue()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(filled_queue(2), out, stop)

    assert [out.get_nowait().text for _ in range(2)] == ["hello", "world"]


def test_silence_gate_filters_blocks_before_they_are_sent():
    stop = threading.Event()
    sessions = []
    quiet = b"\x00" * BLOCK_BYTES

    def factory(clock_state):
        session = ScriptedSession(clock_state, stop=stop, stop_after=6)
        sessions.append(session)
        return session

    audio: queue.Queue = queue.Queue()
    audio.put(AudioChunk(track="mic", pcm=PCM, t_start=0.0))
    for i in range(10):
        audio.put(AudioChunk(track="mic", pcm=quiet, t_start=float(i + 1)))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=lambda pcm: pcm != quiet),
        clock=FakeClock(),
    )
    worker.run(audio, queue.Queue(), stop)

    # One speech block plus the five-block finalisation tail, nothing more.
    assert len(sessions[0].consumed) == 6


def test_rotates_and_carries_the_offset_forward():
    stop = threading.Event()
    clock = FakeClock()
    seen_offsets: list[float] = []

    def factory(clock_state):
        seen_offsets.append(clock_state.offset)
        if len(seen_offsets) == 2:
            stop.set()
        # Each consumed block advances the clock by 0.5 s, so two blocks hit
        # the 1 s rotation limit.
        return ScriptedSession(clock_state, clock=clock, tick=0.5)

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
        max_stream_s=1.0,
    )
    worker.run(filled_queue(6, start=100.0), queue.Queue(), stop)

    assert seen_offsets[0] == 0.0
    # The exact value, not merely positive: 101.0 is the t_start of the last
    # chunk the first stream consumed. Rotating to anything else would pass a
    # "> 0.0" check while silently corrupting the timeline.
    assert seen_offsets[1] == 101.0


def test_fatal_errors_stop_the_run_immediately():
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        return ScriptedSession(clock_state, error=gexc.Unauthenticated("bad creds"))

    clock = FakeClock()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    assert len(attempts) == 1  # no retry loop
    assert stop.is_set()
    assert clock.slept == []  # and no backoff sleep


def test_transport_errors_retry_with_growing_backoff():
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        if len(attempts) >= 4:  # three failures, then stop before the fourth sleep
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    clock = FakeClock()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    assert clock.slept == [2.0, 4.0, 8.0]


def test_repeated_failures_escalate_to_error_level(caplog):
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        if len(attempts) >= 7:  # escalation starts at the fifth failure
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    levels = [
        record.levelname
        for record in caplog.records
        if "speech stream error" in record.getMessage()
    ]
    # Four warnings, then ERROR for every failure after - a dead network must
    # not scroll past at warning level forever.
    assert levels == ["WARNING"] * 4 + ["ERROR"] * 2


def test_reports_audio_lost_while_offline(caplog):
    stop = threading.Event()
    attempts = []
    audio: queue.Queue = queue.Queue()
    audio.dropped = 0  # type: ignore[attr-defined]

    def factory(clock_state):
        attempts.append(1)
        audio.dropped += 30  # the pump kept dropping while we were offline
        if len(attempts) >= 2:
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(audio, queue.Queue(), stop)

    # An unmarked gap in a transcript reads as silence. Say how much went.
    assert any("3s of audio dropped" in r.getMessage() for r in caplog.records)


from meetscribe.google import GoogleConfig, recognizer_path, speech_endpoint


def test_regional_endpoint():
    assert speech_endpoint("eu") == "eu-speech.googleapis.com"
    assert speech_endpoint("us") == "us-speech.googleapis.com"


def test_global_endpoint_has_no_prefix():
    assert speech_endpoint("global") == "speech.googleapis.com"


def test_recognizer_path_uses_the_implicit_recognizer():
    path = recognizer_path("my-project", "eu")

    assert path == "projects/my-project/locations/eu/recognizers/_"


def test_config_defaults_match_the_spec():
    config = GoogleConfig(project_id="p")

    assert config.region == "eu"
    assert config.model == "chirp_3"
    assert config.language_codes == ("en-US",)
    assert config.interim is True
