from meetscribe.types import BLOCK_BYTES, BLOCK_MS, TARGET_RATE, AudioChunk, Segment, Word


def test_block_bytes_is_derived_not_hardcoded():
    # 100 ms of 16 kHz mono signed 16-bit audio.
    assert BLOCK_BYTES == TARGET_RATE * 2 * BLOCK_MS // 1000
    assert BLOCK_BYTES == 3200


def test_audio_chunk_is_immutable():
    chunk = AudioChunk(track="mic", pcm=b"\x00" * BLOCK_BYTES, t_start=1.5)

    assert chunk.track == "mic"
    assert len(chunk.pcm) == BLOCK_BYTES


def test_segment_defaults_to_no_words():
    seg = Segment(track="system", text="hello", is_final=True, t_start=0.0, t_end=1.0)

    assert seg.words == ()
    assert seg.confidence is None


def test_segment_carries_words():
    seg = Segment(
        track="mic",
        text="hi there",
        is_final=True,
        t_start=0.0,
        t_end=1.0,
        words=(Word(word="hi", start=0.0, end=0.4), Word(word="there", start=0.4, end=1.0)),
    )

    assert [w.word for w in seg.words] == ["hi", "there"]
