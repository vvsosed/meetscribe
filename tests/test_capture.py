import pytest

from meetscribe.capture import (
    DROP_LOG_EVERY,
    NODE_APPEAR_TIMEOUT_S,
    QUEUE_BLOCKS,
    CaptureConfig,
    CaptureError,
    DroppingQueue,
    PipeWireCapture,
    plan_recorders,
)
from meetscribe.types import BLOCK_BYTES, MIC, SYSTEM, AudioChunk
from tests.conftest import FakeClock, FakeGraphSource, FakeLauncher


def test_mic_defaults_to_the_default_source(idle_graph):
    specs = plan_recorders(idle_graph, CaptureConfig())
    mic = next(s for s in specs if s.track == MIC)

    assert mic.target == 1002  # serial, not id
    assert mic.capture_sink is False


def test_system_defaults_to_the_default_sink_monitor(idle_graph):
    specs = plan_recorders(idle_graph, CaptureConfig())
    system = next(s for s in specs if s.track == SYSTEM)

    assert system.target == 1001
    assert system.capture_sink is True


def test_app_mode_takes_no_target_and_disables_autoconnect(zoom_graph):
    specs = plan_recorders(zoom_graph, CaptureConfig(app="zoom"))
    system = next(s for s in specs if s.track == SYSTEM)

    # We make our own links with pw-link, so WirePlumber must keep its hands off.
    assert system.target is None
    assert system.autoconnect is False
    assert system.capture_sink is False


def test_mic_can_be_named_by_substring(idle_graph):
    specs = plan_recorders(idle_graph, CaptureConfig(mic="Microphone"))

    assert specs[0].target == 1002


def test_unknown_mic_is_an_error_with_a_hint(idle_graph):
    with pytest.raises(CaptureError) as excinfo:
        plan_recorders(idle_graph, CaptureConfig(mic="nonexistent-device"))

    assert "meetscribe devices" in str(excinfo.value)


def test_no_mic_yields_one_track(idle_graph):
    specs = plan_recorders(idle_graph, CaptureConfig(mic_enabled=False))

    assert [s.track for s in specs] == [SYSTEM]


def test_both_tracks_disabled_is_an_error(idle_graph):
    with pytest.raises(CaptureError):
        plan_recorders(
            idle_graph, CaptureConfig(mic_enabled=False, system_enabled=False)
        )


def test_latency_propagates_to_every_spec(idle_graph):
    specs = plan_recorders(idle_graph, CaptureConfig(latency="250ms"))

    assert all(s.latency == "250ms" for s in specs)


def test_queue_drops_instead_of_blocking():
    queue = DroppingQueue(maxsize=2)

    assert queue.put("a") is True
    assert queue.put("b") is True
    assert queue.put("c") is False
    assert queue.dropped == 1


def test_drop_logging_is_rate_limited(caplog):
    queue = DroppingQueue(maxsize=1)
    queue.put("keep")

    for _ in range(DROP_LOG_EVERY * 2):
        queue.put("drop")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # one per DROP_LOG_EVERY, not one per drop


def test_queue_size_is_about_forty_seconds_of_audio():
    assert QUEUE_BLOCKS == 400


def test_waits_for_the_capture_node_to_register(idle_graph):
    from dataclasses import replace

    from meetscribe.graph import PwNode

    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=None,
        clock=FakeClock(),
    )
    recorder = capture.recorders[MIC]
    registered = replace(
        idle_graph,
        nodes=idle_graph.nodes
        + (
            PwNode(
                id=70,
                serial=2000,
                name=recorder.node_name,
                description="",
                media_class="Stream/Input/Audio",
            ),
        ),
    )
    capture._graph = FakeGraphSource(idle_graph, registered)

    capture.await_capture_node(recorder)  # must not raise


def test_missing_capture_node_is_a_diagnosable_error(idle_graph):
    clock = FakeClock()
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=None,
        clock=clock,
    )

    with pytest.raises(CaptureError) as excinfo:
        capture.await_capture_node(capture.recorders[MIC])

    # This means pw-record itself is broken, so say how to check that.
    assert "pw-record --target=0" in str(excinfo.value)
    assert clock.monotonic() >= NODE_APPEAR_TIMEOUT_S


def test_pump_puts_timestamped_chunks(idle_graph):
    launcher = FakeLauncher(script=b"\x01" * (BLOCK_BYTES * 3))
    clock = FakeClock()
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=launcher,
        linker=None,
        clock=clock,
    )
    # Deliberately not capture.start(): that would start a pump thread and
    # race this one for the same stream.
    recorder = capture.recorders[MIC]
    recorder.start()
    capture.pump(MIC, recorder)

    chunks = [capture.queues[MIC].get(timeout=0) for _ in range(3)]
    assert all(isinstance(c, AudioChunk) for c in chunks)
    assert all(len(c.pcm) == BLOCK_BYTES for c in chunks)
    assert [c.track for c in chunks] == [MIC, MIC, MIC]


def test_pump_marks_a_track_dead_when_pw_record_exits(idle_graph):
    launcher = FakeLauncher(script=b"")
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=launcher,
        linker=None,
        clock=FakeClock(),
    )
    recorder = capture.recorders[MIC]
    recorder.start()
    launcher.processes[0].die(returncode=1, stderr="no such target")

    capture.pump(MIC, recorder)

    # Silent death means the track goes quiet for the rest of the meeting.
    assert capture.dead_tracks == {MIC}
    assert capture.all_tracks_dead() is True
