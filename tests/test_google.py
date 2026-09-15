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
