"""Command line entry point and wiring."""

from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import sys
import threading
from pathlib import Path

from .adapters import (
    MIN_PW_VERSION,
    MissingToolError,
    PwDumpGraphSource,
    PwLinkLinker,
    SubprocessLauncher,
    SystemClock,
    installed_pw_version,
)
from .capture import CaptureConfig, CaptureError, PipeWireCapture
from .google import EngineWorker, GoogleConfig, build_session_factory, project_from_environment
from .graph import PLAYBACK_STREAM, SINK, SOURCE, PwGraph
from .ports import Clock, GraphSource, Linker, ProcessLauncher
from .transcript import TranscriptWriter
from .vad import SilenceGate, webrtc_detector

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetscribe",
        description="Live transcription of any call on your machine.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "devices", help="list sinks, sources and apps currently playing audio"
    )

    run = sub.add_parser("run", help="start transcribing")

    sources = run.add_argument_group("audio sources")
    target = sources.add_mutually_exclusive_group()
    target.add_argument(
        "--app",
        metavar="NAME",
        help="tap only this application's audio (e.g. zoom, slack). "
        "Run `meetscribe devices` mid-call to see the options.",
    )
    target.add_argument(
        "--system", help="output device whose monitor to capture"
    )
    sources.add_argument("--mic", help="microphone node name or substring")
    sources.add_argument("--no-mic", action="store_true", help="skip your own voice")
    sources.add_argument(
        "--no-system", action="store_true", help="skip everyone else"
    )
    sources.add_argument(
        "--latency", default="100ms", help="PipeWire stream latency (default 100ms)"
    )

    stt = run.add_argument_group("speech to text")
    stt.add_argument(
        "--lang",
        action="append",
        dest="langs",
        help="BCP-47 code; repeat for multilingual recognition (default en-US)",
    )
    stt.add_argument("--model", default="chirp_3")
    stt.add_argument("--region", default="eu", help="GCP region (default eu)")
    stt.add_argument("--project", help="GCP project id")
    stt.add_argument(
        "--phrase",
        action="append",
        dest="phrases",
        help="boost a term (names, jargon); repeat as needed",
    )

    out = run.add_argument_group("output")
    out.add_argument("--out", type=Path, default=Path("transcripts"))
    out.add_argument(
        "--no-interim", action="store_true", help="only print finalised text"
    )
    out.add_argument("-v", "--verbose", action="store_true")
    return parser


def describe_graph(graph: PwGraph) -> str:
    """Human-readable graph summary. Run this while your call is live."""
    lines: list[str] = ["", "=== OUTPUT DEVICES (sinks) ==="]
    for node in graph.by_class(SINK):
        default = " [default]" if node.name == graph.default_sink else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== INPUT DEVICES (sources / microphones) ==="]
    for node in graph.by_class(SOURCE):
        if node.name.endswith(".monitor"):
            continue
        default = " [default]" if node.name == graph.default_source else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== APPLICATIONS CURRENTLY PLAYING AUDIO ==="]
    streams = graph.by_class(PLAYBACK_STREAM)
    if not streams:
        lines.append("  (none - start your Zoom/Slack call, then run this again)")
    for node in streams:
        lines.append(
            f"  serial={node.serial:<6} {node.app_name or '?'}"
            f"  binary={node.app_binary or '?'}  pid={node.pid}"
        )
        lines.append(f"      --app '{node.app_binary or node.app_name}'")
    lines.append("")
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    graph: GraphSource | None = None,
    launcher: ProcessLauncher | None = None,
    linker: Linker | None = None,
    clock: Clock | None = None,
) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    graph = graph or PwDumpGraphSource()
    launcher = launcher or SubprocessLauncher()
    linker = linker or PwLinkLinker()
    clock = clock or SystemClock()

    try:
        if args.cmd == "devices":
            print(describe_graph(graph.snapshot()))
            return 0
        return _run(args, graph, launcher, linker, clock)
    except json.JSONDecodeError as exc:
        # Not a RuntimeError, so the clause below would miss it and the user
        # would get a traceback instead of one clean line.
        print(
            f"Could not parse pw-dump output ({exc}). Is PipeWire running? "
            "Check with: pw-dump | head",
            file=sys.stderr,
        )
        return 1
    except (CaptureError, MissingToolError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _run(args, graph, launcher, linker, clock) -> int:
    if installed_pw_version() < MIN_PW_VERSION:
        log.warning(
            "PipeWire older than %s detected; target.object and "
            "stream.capture.sink may misbehave.",
            ".".join(str(p) for p in MIN_PW_VERSION),
        )

    google_config = GoogleConfig(
        project_id=args.project or project_from_environment(),
        region=args.region,
        model=args.model,
        language_codes=tuple(args.langs or ["en-US"]),
        phrases=tuple(args.phrases or ()),
        interim=not args.no_interim,
    )

    capture = PipeWireCapture(
        config=CaptureConfig(
            mic=args.mic,
            system=args.system,
            app=args.app,
            mic_enabled=not args.no_mic,
            system_enabled=not args.no_system,
            latency=args.latency,
        ),
        graph=graph,
        launcher=launcher,
        linker=linker,
        clock=clock,
    )

    segments: queue.Queue = queue.Queue()
    stop = threading.Event()
    writer = TranscriptWriter(args.out, show_interim=not args.no_interim)

    workers = [
        threading.Thread(
            target=EngineWorker(
                track=track,
                session_factory=build_session_factory(google_config, track),
                # A fresh detector per track, deliberately: webrtcvad adapts
                # to the noise floor across calls, so a shared instance would
                # let one track's loudness skew the other's classification.
                gate=SilenceGate(webrtc_detector()),
                clock=clock,
            ).run,
            args=(capture.queues[track], segments, stop),
            name=f"engine-{track}",
            daemon=True,
        )
        for track in capture.queues
    ]

    def handle_sigint(*_):
        stop.set()
        capture.stop.set()

    signal.signal(signal.SIGINT, handle_sigint)

    capture.start()
    for worker in workers:
        worker.start()

    where = f"app={args.app}" if args.app else "system audio"
    print(f"\nRecording {where} via Chirp 3. This session is being recorded. Ctrl-C to stop.\n")

    try:
        while not stop.is_set():
            if capture.all_tracks_dead():
                log.error("every capture track has died; stopping")
                break
            try:
                writer.write(segments.get(timeout=0.25))
            except queue.Empty:
                continue
    finally:
        stop.set()
        capture.shutdown()
        for worker in workers:
            worker.join(timeout=3.0)
        while True:  # drain anything finalised on the way out
            try:
                writer.write(segments.get_nowait())
            except queue.Empty:
                break
        path = writer.close()
        print(f"\nSaved:\n  {writer.jsonl_path}\n  {path}")

    return 0
