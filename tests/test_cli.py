import json

import pytest

from meetscribe.cli import build_parser, describe_graph, main
from tests.conftest import FakeClock, FakeGraphSource, FakeLauncher, FakeLinker


def parse(*args):
    return build_parser().parse_args(["run", *args])


def test_defaults_match_the_spec():
    args = parse()

    assert args.region == "eu"
    assert args.model == "chirp_3"
    assert args.langs is None  # resolved to ["en-US"] during wiring
    assert args.latency == "100ms"
    assert str(args.out) == "transcripts"
    assert args.no_interim is False


def test_lang_is_repeatable():
    args = parse("--lang", "uk-UA", "--lang", "en-US")

    assert args.langs == ["uk-UA", "en-US"]


def test_phrase_is_repeatable():
    args = parse("--phrase", "Kubernetes", "--phrase", "Poltava")

    assert args.phrases == ["Kubernetes", "Poltava"]


def test_app_and_system_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse("--app", "zoom", "--system", "speakers")


def test_tracks_can_be_disabled_individually():
    assert parse("--no-mic").no_mic is True
    assert parse("--no-system").no_system is True


def test_devices_lists_sinks_sources_and_apps(zoom_graph):
    text = describe_graph(zoom_graph)

    assert "ZOOM VoiceEngine" in text
    assert "--app 'zoom'" in text
    assert "alsa_output.pci-0000_00_1f.3.analog-stereo" in text
    assert "1204" in text  # serial, the thing you actually need


def test_devices_marks_the_defaults(idle_graph):
    text = describe_graph(idle_graph)

    assert text.count("[default]") == 2


def test_devices_hides_monitor_sources(idle_graph):
    text = describe_graph(idle_graph)

    # A monitor is not something you would ever pass to --mic.
    assert ".analog-stereo.monitor" not in text


def test_devices_explains_an_empty_application_list(idle_graph):
    text = describe_graph(idle_graph)

    assert "start your" in text.lower()


def test_devices_command_returns_zero(capsys, zoom_graph):
    code = main(["devices"], graph=FakeGraphSource(zoom_graph))

    assert code == 0
    assert "APPLICATIONS" in capsys.readouterr().out


def test_malformed_pw_dump_reports_cleanly(capsys):
    class BrokenGraph:
        def snapshot(self):
            raise json.JSONDecodeError("Expecting value", "", 0)

    code = main(["devices"], graph=BrokenGraph())

    assert code == 1
    assert "pw-dump" in capsys.readouterr().err


def test_run_reports_a_missing_device_without_a_traceback(capsys, idle_graph):
    code = main(
        ["run", "--mic", "nonexistent-device", "--project", "p"],
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
    )

    assert code == 1
    assert "meetscribe devices" in capsys.readouterr().err


def test_run_drains_and_closes_the_writer_when_every_track_dies(tmp_path, idle_graph):
    # pw-record fails at startup - a bad target, say - so the pump finds a
    # dead process, capture marks the track dead, and the main loop's
    # all_tracks_dead() check breaks out into the finally block. This is the
    # only test that reaches past capture construction into worker startup
    # and the shutdown sequence.
    class DyingLauncher(FakeLauncher):
        def spawn(self, argv):
            process = super().spawn(argv)
            process.die(returncode=1, stderr="no such target")
            return process

    class SilentSession:
        def stream(self, pcm):
            for _ in pcm:
                pass
            return iter(())

    def fake_factory(config, track):
        return lambda stream_clock: SilentSession()

    code = main(
        ["run", "--no-system", "--project", "p", "--out", str(tmp_path)],
        graph=FakeGraphSource(idle_graph),
        launcher=DyingLauncher(script=b""),
        linker=FakeLinker(),
        clock=FakeClock(),
        session_factory=fake_factory,
    )

    assert code == 0
    assert list(tmp_path.glob("*.jsonl")), "writer should have created its record"
    assert list(tmp_path.glob("*.md")), "close() should have rendered the markdown"
