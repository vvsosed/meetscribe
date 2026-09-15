from pathlib import Path

import pytest

from meetscribe.graph import PLAYBACK_STREAM, SINK, SOURCE, parse_graph

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def idle():
    return parse_graph((FIXTURES / "pw_dump_idle.json").read_text())


@pytest.fixture
def zoom():
    return parse_graph((FIXTURES / "pw_dump_zoom_active.json").read_text())


def test_nodes_are_identified_by_serial_not_id(idle):
    sink = idle.node_by_name("alsa_output.pci-0000_00_1f.3.analog-stereo")

    # Ids get recycled when nodes come and go; serials do not. Anything that
    # targets a node must use the serial.
    assert sink.id == 40
    assert sink.serial == 1001


def test_nodes_without_a_media_class_are_skipped(idle):
    assert idle.node_by_name("node-without-media-class") is None


def test_filters_by_media_class(idle, zoom):
    assert [n.serial for n in idle.by_class(SINK)] == [1001]
    assert [n.serial for n in zoom.by_class(PLAYBACK_STREAM)] == [1204, 1250]


def test_reads_defaults_from_metadata(idle):
    assert idle.default_sink == "alsa_output.pci-0000_00_1f.3.analog-stereo"
    assert idle.default_source == "alsa_input.pci-0000_00_1f.3.analog-stereo"


def test_find_matches_on_substring_of_binary(zoom):
    node = zoom.find("zoom", PLAYBACK_STREAM)

    assert node.serial == 1204
    assert node.label == "ZOOM VoiceEngine"


def test_find_prefers_an_exact_node_name(idle):
    node = idle.find("alsa_input.pci-0000_00_1f.3.analog-stereo", SOURCE)

    assert node.serial == 1002


def test_find_returns_none_when_nothing_matches(idle):
    assert idle.find("obs-studio", PLAYBACK_STREAM) is None


def test_ports_are_filtered_by_node_and_direction(zoom):
    outs = zoom.ports_of(55, "out")

    assert [p.name for p in outs] == ["output_FL", "output_FR"]
    assert zoom.ports_of(55, "in") == ()


def test_matching_is_case_insensitive(zoom):
    assert zoom.find("ZOOM", PLAYBACK_STREAM) is not None
    assert zoom.find("Spotify", PLAYBACK_STREAM).app_binary == "spotify"
