from meetscribe.recorder import build_argv, format_properties


def test_always_requests_the_audio_contract():
    argv = build_argv(node_name="meetscribe.mic.abc", media_name="meetscribe mic")

    # PipeWire does the resampling and downmixing for us. Nothing downstream
    # is allowed to assume any other format.
    assert "--rate" in argv and argv[argv.index("--rate") + 1] == "16000"
    assert "--channels" in argv and argv[argv.index("--channels") + 1] == "1"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "s16"
    assert argv[-1] == "-"
    assert "--raw" in argv


def test_property_values_stay_quoted():
    # An unquoted space here splits the property in two and pw-record drops it.
    props = format_properties({"media.name": "meetscribe system"})

    assert props == '{ media.name="meetscribe system" }'


def test_media_name_with_a_space_survives_argv_construction():
    argv = build_argv(node_name="meetscribe.system.abc", media_name="meetscribe system")
    properties = argv[argv.index("--properties") + 1]

    assert 'media.name="meetscribe system"' in properties


def test_sink_monitor_capture_sets_capture_sink():
    argv = build_argv(
        node_name="meetscribe.system.abc",
        media_name="meetscribe system",
        target=1001,
        capture_sink=True,
    )
    properties = argv[argv.index("--properties") + 1]

    # The Linux equivalent of WASAPI loopback: attach to the sink's monitor
    # ports rather than expecting it to be a source.
    assert 'stream.capture.sink="true"' in properties
    assert argv[argv.index("--target") + 1] == "1001"


def test_app_tap_disables_autoconnect():
    argv = build_argv(
        node_name="meetscribe.system.abc",
        media_name="meetscribe system",
        autoconnect=False,
    )
    properties = argv[argv.index("--properties") + 1]

    # Otherwise WirePlumber helpfully links our capture node to the default
    # microphone and we record the wrong thing.
    assert 'node.autoconnect="false"' in properties
    assert "--target" not in argv


def test_latency_is_configurable():
    argv = build_argv(
        node_name="n", media_name="m", latency="250ms"
    )

    assert argv[argv.index("--latency") + 1] == "250ms"
