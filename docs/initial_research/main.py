"""
meetscribe - live transcription of any call on your machine.

Linux/PipeWire:
    uv run python -m meetscribe devices
    uv run python -m meetscribe run --app zoom --engine google --lang uk-UA --lang en-US
    uv run python -m meetscribe run --engine local --model small
"""

from __future__ import annotations

import argparse
import logging
import platform
import queue
import signal
import sys
import threading
from pathlib import Path

from .engines import build_engine
from .transcript import TranscriptWriter

IS_LINUX = platform.system() == "Linux"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meetscribe", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices",
                   help="list sinks, sources and apps currently playing audio")

    r = sub.add_parser("run", help="start transcribing")

    src = r.add_argument_group("audio sources")
    src.add_argument("--app", default=None, metavar="NAME",
                     help="tap only this application's audio "
                          "(e.g. zoom, slack, firefox). PipeWire only. "
                          "Run `devices` mid-call to see the options.")
    src.add_argument("--mic", default=None,
                     help="microphone node name or substring")
    src.add_argument("--system", default=None,
                     help="output device whose monitor to capture "
                          "(ignored when --app is used)")
    src.add_argument("--no-mic", action="store_true", help="skip your own voice")
    src.add_argument("--no-system", action="store_true",
                     help="skip everyone else")
    src.add_argument("--latency", default="100ms",
                     help="PipeWire stream latency (default 100ms)")

    stt = r.add_argument_group("speech to text")
    stt.add_argument("--engine", choices=("google", "deepgram", "local"),
                     default="google")
    stt.add_argument("--lang", action="append", dest="langs", default=None,
                     help="BCP-47 code; repeat for multilingual (google)")
    stt.add_argument("--model", default=None,
                     help="chirp_3 | nova-3 | whisper size (small/large-v3)")
    stt.add_argument("--region", default="eu", help="GCP region")
    stt.add_argument("--project", default=None, help="GCP project id")
    stt.add_argument("--phrase", action="append", dest="phrases", default=None,
                     help="boost a term (names, jargon); repeat as needed")

    out = r.add_argument_group("output")
    out.add_argument("--out", type=Path, default=Path("transcripts"))
    out.add_argument("--no-interim", action="store_true",
                     help="only print finalised text")
    out.add_argument("-v", "--verbose", action="store_true")
    return p


def make_capture(args):
    """PipeWire on Linux; the portable soundcard backend elsewhere."""
    if IS_LINUX:
        from .pipewire import PipeWireCapture
        return PipeWireCapture(
            mic=args.mic, system=args.system, app=args.app,
            mic_enabled=not args.no_mic,
            system_enabled=not args.no_system,
            latency=args.latency)

    if args.app:
        sys.exit("--app (per-application capture) requires Linux/PipeWire.")
    from .capture import DualCapture
    return DualCapture(mic_name=args.mic, system_name=args.system,
                       mic_enabled=not args.no_mic,
                       system_enabled=not args.no_system)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if args.cmd == "devices":
        if IS_LINUX:
            from .pipewire import describe_graph
            describe_graph()
        else:
            from .capture import list_devices
            list_devices()
        return 0

    cfg = {
        "project_id": args.project,
        "region": args.region,
        "language_codes": args.langs or ["en-US"],
        "language": (args.langs or ["multi"])[0],
        "phrases": args.phrases,
    }
    if args.model:
        cfg["model"] = args.model

    engine = build_engine(args.engine, cfg)
    capture = make_capture(args)

    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    writer = TranscriptWriter(args.out, show_interim=not args.no_interim)

    workers = [
        threading.Thread(target=engine.run, args=(track, q, out_q, stop),
                         name=f"engine-{track}", daemon=True)
        for track, q in capture.queues.items()
    ]

    def handle_sigint(*_):
        stop.set()
        capture.stop.set()

    signal.signal(signal.SIGINT, handle_sigint)

    capture.start()
    for w in workers:
        w.start()

    where = f"app={args.app}" if args.app else "system audio"
    print(f"\nRecording {where} via {args.engine}. Ctrl-C to stop.\n")

    try:
        while not stop.is_set():
            try:
                writer.write(out_q.get(timeout=0.25))
            except queue.Empty:
                continue
    finally:
        stop.set()
        capture.shutdown()
        for w in workers:
            w.join(timeout=3.0)
        while True:  # drain anything the engines finalised on the way out
            try:
                writer.write(out_q.get_nowait())
            except queue.Empty:
                break
        writer.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
