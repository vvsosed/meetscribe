import pytest

import tempfile

from meetscribe.adapters import (
    STDERR_TAIL_BYTES,
    MissingToolError,
    PopenProcess,
    SystemClock,
    classify_link_output,
    parse_pw_version,
    require_tool,
)
from meetscribe.ports import LinkResult


def test_link_success():
    assert classify_link_output(0, "") is LinkResult.LINKED


def test_file_exists_means_already_linked():
    # pw-link says this when the pair is already connected. Benign.
    assert classify_link_output(1, "failed to link ports: File exists") is (
        LinkResult.ALREADY_LINKED
    )


def test_other_errors_are_failures():
    assert classify_link_output(1, "No such port") is LinkResult.FAILED


def test_parses_the_pipewire_version():
    assert parse_pw_version("pw-cli\nCompiled with libpipewire 1.0.5\n") == (1, 0, 5)


def test_unparseable_version_is_zero():
    assert parse_pw_version("something unexpected") == (0, 0, 0)


def test_require_tool_returns_the_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    assert require_tool("pw-dump") == "/usr/bin/pw-dump"


def test_require_tool_explains_how_to_install(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(MissingToolError) as excinfo:
        require_tool("pw-record")

    message = str(excinfo.value)
    assert "pw-record" in message
    assert "pacman" in message  # install hints for the user's distro


def test_system_clock_satisfies_the_port():
    from meetscribe.ports import Clock

    assert isinstance(SystemClock(), Clock)


def test_stderr_text_returns_the_tail_of_a_long_log():
    # A temp file rather than a pipe, so pw-record can log all meeting without
    # filling a 64 KB buffer and deadlocking. We keep the end of the log,
    # which is where the failure is.
    with tempfile.TemporaryFile() as handle:
        handle.write(b"x" * STDERR_TAIL_BYTES)
        handle.write(b"the actual error\n")

        text = PopenProcess(process=None, stderr_file=handle).stderr_text()

    assert "the actual error" in text
    assert len(text) <= STDERR_TAIL_BYTES


def test_stderr_text_is_empty_without_a_file():
    assert PopenProcess(process=None).stderr_text() == ""
