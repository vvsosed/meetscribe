import threading
from dataclasses import replace

from meetscribe.graph import PwGraph, PwNode, PwPort
from meetscribe.ports import LinkResult
from meetscribe.tap import POLL_INTERVAL_S, AppTap
from tests.conftest import FakeClock, FakeGraphSource, FakeLinker

CAPTURE_NODE = "meetscribe.system.deadbeef"


def with_capture_node(graph: PwGraph) -> PwGraph:
    """Our pw-record node, as it appears once the process is running."""
    node = PwNode(
        id=70,
        serial=2000,
        name=CAPTURE_NODE,
        description="",
        media_class="Stream/Input/Audio",
    )
    port = PwPort(id=700, node_id=70, name="input_MONO", direction="in")
    return replace(graph, nodes=graph.nodes + (node,), ports=graph.ports + (port,))


def make_tap(*snapshots, linker=None, clock=None):
    return AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=FakeGraphSource(*snapshots),
        linker=linker or FakeLinker(),
        clock=clock or FakeClock(),
    )


def test_links_every_app_channel_into_our_mono_input(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    assert tap.poll_once() == 2
    # Both of Zoom's channels fan into our single input; PipeWire sums them.
    assert linker.links == [(60, 700), (61, 700)]


def test_ignores_applications_that_do_not_match(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()

    # Spotify's port is 62 and must never be linked.
    assert 62 not in [src for src, _ in linker.links]


def test_waits_for_the_stream_to_appear(idle_graph, zoom_graph):
    linker = FakeLinker()
    tap = make_tap(
        with_capture_node(idle_graph),
        with_capture_node(zoom_graph),
        linker=linker,
    )

    # Zoom has not started its stream yet.
    assert tap.poll_once() == 0
    assert linker.links == []

    # Meeting starts.
    assert tap.poll_once() == 2


def test_does_not_relink_on_every_poll(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()
    tap.poll_once()
    tap.poll_once()

    assert len(linker.links) == 2


def test_does_nothing_until_our_capture_node_exists(zoom_graph):
    linker = FakeLinker()
    tap = make_tap(zoom_graph, linker=linker)  # no capture node in the graph

    assert tap.poll_once() == 0
    assert linker.links == []


def test_already_linked_is_not_treated_as_a_failure(zoom_graph):
    linker = FakeLinker(result=LinkResult.ALREADY_LINKED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    # Nothing new was created, but nothing went wrong either.
    assert tap.poll_once() == 0
    assert len(linker.links) == 2


def test_records_which_applications_were_tapped(zoom_graph):
    tap = make_tap(with_capture_node(zoom_graph))

    tap.poll_once()

    assert tap.tapped_labels == {"ZOOM VoiceEngine"}


def test_run_polls_on_the_interval_until_stopped(zoom_graph):
    stop = threading.Event()
    clock = FakeClock()
    graph = FakeGraphSource(with_capture_node(zoom_graph))
    linker = FakeLinker()
    tap = AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=graph,
        linker=linker,
        clock=clock,
    )

    original_sleep = clock.sleep

    def sleep_and_maybe_stop(seconds):
        original_sleep(seconds)
        if len(clock.slept) >= 3:
            stop.set()

    clock.sleep = sleep_and_maybe_stop  # type: ignore[method-assign]
    tap.run(stop)

    assert clock.slept == [POLL_INTERVAL_S] * 3
    assert graph.calls == 3
