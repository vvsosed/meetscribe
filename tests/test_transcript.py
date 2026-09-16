import json
import os
import shutil

from meetscribe.transcript import TranscriptWriter, _interim_width, hhmmss, render_markdown
from meetscribe.types import Segment


def final(track, text, t):
    return Segment(track=track, text=text, is_final=True, t_start=t, t_end=t + 1)


def test_hhmmss():
    assert hhmmss(0) == "00:00:00"
    assert hhmmss(61.9) == "00:01:01"
    assert hhmmss(3661) == "01:01:01"


def test_markdown_groups_consecutive_lines_under_one_heading():
    records = [
        {"t": 0.0, "track": "mic", "text": "hello"},
        {"t": 1.0, "track": "mic", "text": "still me"},
        {"t": 2.0, "track": "system", "text": "hi back"},
    ]

    markdown = render_markdown("session-1", records)

    assert markdown.count("**You**") == 1
    assert markdown.count("**Them**") == 1
    assert "hello\nstill me" in markdown


def test_markdown_sorts_by_time():
    records = [
        {"t": 5.0, "track": "mic", "text": "second"},
        {"t": 1.0, "track": "system", "text": "first"},
    ]

    markdown = render_markdown("s", records)

    assert markdown.index("first") < markdown.index("second")


def test_markdown_starts_a_new_heading_when_the_speaker_changes_back():
    records = [
        {"t": 0.0, "track": "mic", "text": "a"},
        {"t": 1.0, "track": "system", "text": "b"},
        {"t": 2.0, "track": "mic", "text": "c"},
    ]

    assert render_markdown("s", records).count("**You**") == 2


def test_jsonl_is_written_per_final_segment(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "one", 0.0))
    writer.write(final("system", "two", 1.0))

    # Deliberately no close(): an unclean exit must still leave the record.
    lines = (tmp_path / "sess.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["text"] for line in lines] == ["one", "two"]


def test_interim_segments_never_reach_the_jsonl(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(Segment(track="mic", text="partial", is_final=False, t_start=0.0, t_end=1.0))

    assert (tmp_path / "sess.jsonl").read_text() == ""


def test_close_writes_markdown_and_returns_its_path(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")
    writer.write(final("mic", "hello", 0.0))

    path = writer.close()

    assert path == tmp_path / "sess.md"
    assert "# Transcript sess" in path.read_text()
    assert "hello" in path.read_text()


def test_jsonl_records_carry_timing_and_confidence(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")
    writer.write(
        Segment(
            track="system", text="x", is_final=True, t_start=1.0, t_end=2.0, confidence=0.87
        )
    )

    record = json.loads((tmp_path / "sess.jsonl").read_text().strip())
    assert record["t"] == 1.0
    assert record["t_end"] == 2.0
    assert record["track"] == "system"
    assert record["confidence"] == 0.87
    assert "wall_clock" in record


def test_creates_the_output_directory(tmp_path):
    target = tmp_path / "nested" / "transcripts"

    TranscriptWriter(target, session="sess")

    assert target.is_dir()


def test_final_lines_print_a_stamp_and_a_speaker_label(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "hello", 61.0))
    writer.write(final("system", "hi back", 62.0))

    out = capsys.readouterr().out
    assert "00:01:01 You: hello" in out
    assert "00:01:02 Them: hi back" in out


def test_console_output_is_plain_when_stdout_is_not_a_tty(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "hello", 0.0))

    # No cursor to rewrite and no point in colour under a pipe.
    assert "\x1b[" not in capsys.readouterr().out


def test_interim_lines_are_silent_when_stdout_is_not_a_tty(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(
        Segment(track="mic", text="partial", is_final=False, t_start=0.0, t_end=1.0)
    )

    assert capsys.readouterr().out == ""


def test_interim_width_follows_the_terminal(monkeypatch):
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((40, 24))
    )

    # A fixed width wider than the terminal wraps, and the \r erase then
    # clears only one of the two rows.
    assert _interim_width() == 39
