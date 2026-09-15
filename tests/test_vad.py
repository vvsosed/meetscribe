from meetscribe.types import BLOCK_BYTES
from meetscribe.vad import SILENCE_TAIL_BLOCKS, SilenceGate

SPEECH = b"\x01" * BLOCK_BYTES
QUIET = b"\x00" * BLOCK_BYTES


def detector(pcm: bytes) -> bool:
    """Stand-in for webrtcvad: non-zero bytes count as speech."""
    return pcm != QUIET


def test_passes_everything_when_no_detector_is_available():
    gate = SilenceGate(detector=None)

    assert all(gate.allows(QUIET) for _ in range(100))


def test_passes_speech():
    gate = SilenceGate(detector=detector)

    assert gate.allows(SPEECH) is True


def test_passes_a_silence_tail_then_stops():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)

    passed = [gate.allows(QUIET) for _ in range(SILENCE_TAIL_BLOCKS + 3)]

    # The tail lets the engine finalise the utterance; after that we stop
    # paying to transmit dead air.
    assert passed[:SILENCE_TAIL_BLOCKS] == [True] * SILENCE_TAIL_BLOCKS
    assert passed[SILENCE_TAIL_BLOCKS:] == [False, False, False]


def test_tail_resets_when_speech_resumes():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)
    for _ in range(SILENCE_TAIL_BLOCKS + 2):
        gate.allows(QUIET)

    assert gate.allows(SPEECH) is True
    assert gate.allows(QUIET) is True  # tail counter went back to zero


def test_frame_size_divides_a_block_evenly():
    from meetscribe.vad import FRAME_BYTES

    # webrtcvad only accepts 10, 20 or 30 ms frames, so a 100 ms block has to
    # split into whole frames or the last one is silently dropped.
    assert BLOCK_BYTES % FRAME_BYTES == 0
