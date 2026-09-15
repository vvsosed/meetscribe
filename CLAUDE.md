# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

meetscribe transcribes any call on the machine (Zoom, Slack huddles, Meet, Teams, Discord,
a browser tab) by tapping **PipeWire** instead of integrating with each platform's API.
Mic and call audio are captured as two separate tracks, which gives "You" vs "Them"
separation for free — no diarization model needed.

## Current repository state — read this first

**The application does not exist yet.** There is no `meetscribe/` package and no
application source anywhere in the repo.

`docs/initial_research/` is **reference material, not the codebase.** Those files are
examples written up after an initial research spike, to prove out the PipeWire approach and
record what it taught. Read them like a design document that happens to have runnable
snippets attached.

- **Don't treat them as production code.** No refactoring, linting, restructuring or
  feature work in there, and don't extend them to cover new requirements. They are a record
  of what the spike found.
- **Don't assume they run.** They use relative imports (`from .engines import
  build_engine`) with no `__init__.py`, and `main.py` imports a `.capture` module
  (`DualCapture`, `list_devices`) that was never written — only the Linux/PipeWire path
  exists.
- **Do mine them for mechanics.** The graph handling, the additive `pw-link` tap and the
  5-minute Google stream rotation are the hard-won parts, and
  `docs/initial_research/README.md` is the closest thing this project has to a spec.

When real implementation starts, it belongs in a fresh package (`meetscribe/` at the repo
root) with its own `pyproject.toml` and `uv.lock` alongside it. The design section below is
what should carry over; the example files themselves should not be moved or promoted.

No tests, no linter/formatter config and no CI exist anywhere in the repo yet.

## Commands

**This project uses uv, not pip.** Never call `pip install` or activate `.venv` by hand;
`uv run` does both. `uv.lock` is committed and `uv sync` is what installs from it.

The repo's only uv project today is `docs/initial_research/` — `pyproject.toml`, `uv.lock`
and `.venv` all live there, and they describe what the **example files** need, not the
future application. The real package will get its own manifest at the repo root. Commands
below assume that directory; from the repo root, add `--directory docs/initial_research` to
any of them.

```bash
cd docs/initial_research        # the examples' environment

uv sync                          # base deps only (numpy, webrtcvad)
uv sync --extra google           # + the engine you actually need
uv sync --extra deepgram
uv sync --extra local            # faster-whisper; pulls ctranslate2, big
uv sync --all-extras             # every engine at once

uv add <pkg>                     # edits pyproject.toml + uv.lock together
uv add --optional google <pkg>   # add to an engine extra, not the base set
uv lock --upgrade                # refresh the lockfile

# verify the PipeWire toolchain BEFORE debugging anything in Python
pw-cli --version                   # needs >= 0.3.60
pw-dump | head                     # graph as JSON
pw-record --target=0 /tmp/t.wav    # Ctrl-C, then play it back

# the CLI the spike designed — not implemented yet, kept here as the target shape
uv run python -m meetscribe devices                       # run this MID-CALL
uv run python -m meetscribe run --app zoom --region eu --lang uk-UA --lang en-US
uv run python -m meetscribe run --engine local --model medium   # offline
```

There is one extra per engine, mirroring the lazy-import rule below: an engine's SDK is
declared in its own extra *and* imported inside the function body, so `uv sync --extra
deepgram` leaves grpc and ctranslate2 out of the environment entirely. A new engine needs
both halves — a new extra in `docs/initial_research/pyproject.toml` and a function-body
import.

Engine credentials: `GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT` (google),
`DEEPGRAM_API_KEY` (deepgram), none (local whisper).

## The design the spike established

Everything below is distilled from `docs/initial_research/` and is what a real
implementation should carry over. Module names refer to the example files, which are worth
reading before rebuilding any of this.

A three-stage pipeline wired together in `main.py`, with `threading.Queue` as every seam:

```
capture backend ──► queues[track] ──► engine.run() thread per track ──► out_q ──► TranscriptWriter
   (pipewire.py)     AudioChunk        (engines.py)                    Segment     (transcript.py)
```

- **`pipewire.py`** — parses `pw-dump` JSON into `PwGraph`/`PwNode`/`PwPort`, spawns one
  `pw-record` subprocess per track (`PwRecorder`), and for `--app` runs an `AppTap` watcher.
  `PipeWireCapture` owns the recorders and exposes `.queues: dict[track, Queue]`.
- **`engines.py`** — `Engine` is a Protocol with a single method
  `run(track, audio_q, out_q, stop)`. `ChunkReader` adapts a queue into a PCM generator and
  applies VAD gating. `build_engine(name, cfg)` is the only construction path.
- **`transcript.py`** — `TranscriptWriter` is the single consumer of `out_q`; console +
  `.jsonl` + `.md`.

Swapping an engine is a config change, not a code change — a property worth preserving.

### Invariants

- **Audio contract:** s16 / 16 kHz / mono, 100 ms blocks = `BLOCK_BYTES` 3200. PipeWire does
  the resampling and downmixing (`--rate/--channels/--format` on `pw-record`), which is why
  the capture path has no numpy in it. Don't add resampling in Python.
- **Track names** are `"mic"` and `"system"`; `transcript.py:LABELS` maps them to You/Them.
  Adding a track means touching `LABELS`/`COLORS` too.
- **Identify PipeWire nodes by `object.serial`, never `object.id`** — ids get reused.
- **Engine deps are imported lazily** inside `__init__`/`run` (google, websockets,
  faster_whisper, numpy, webrtcvad) so an unused engine's package stays optional — that is
  what makes the per-engine extras in `pyproject.toml` work. A top-level import would break
  `uv sync --extra deepgram` for everyone who didn't install the others. Keep new engine
  imports inside the function body.
- **Shutdown:** SIGINT sets `stop` + `capture.stop`; `main` joins workers, then *drains
  `out_q`* for finals the engines emitted on the way out, then `writer.close()`. JSONL is
  append-only and flushed per final segment, so an unclean exit still keeps everything.
  `.md` is only rendered in `close()`.

### Non-obvious mechanics

- **Per-app tap is additive.** The capture node is created with `node.autoconnect=false`
  (so WirePlumber doesn't link it to the default mic), then `pw-link` connects the app's
  output ports to it. The app's existing link to the speakers is untouched — the user still
  hears the call. Whole-system capture instead uses `stream.capture.sink=true` against the
  sink's monitor (the Linux equivalent of WASAPI loopback).
- **`AppTap` re-scans the graph every 2 s** and is not optional: Zoom doesn't create its
  audio stream until the meeting starts, so a recorder that resolves nodes once at launch
  records silence. The same watcher covers reconnects when someone switches headphones.
  Already-made links are deduped in `_linked`; `pw-link` returning "File exists" is benign.
- **`pw-record --properties` is JSON-ish** — property values must stay quoted or an
  unquoted space silently splits the property.
- **Google caps `StreamingRecognize` at 5 minutes.** `GoogleV2Engine.MAX_STREAM_SECONDS`
  is 240 s; the run loop tears the stream down and reopens, carrying `offset` forward so
  timestamps stay continuous. Without the rotation, transcription silently stops mid-meeting.
- **Chirp 3 does not diarize in streaming mode** (only `Recognize`/`BatchRecognize`).
  Live per-speaker labels come from Deepgram, which sets `Segment.speaker`.
- **VAD gating** drops silence to cut API cost, but `ChunkReader` deliberately lets ~5
  silent blocks through after speech so the engine can finalise the utterance.
- **Whisper is not streaming.** `LocalWhisperEngine` buffers until a pause
  (`silence_to_flush_s`) or `max_utterance_s`, so latency is one utterance.

## Things that bite at runtime

- **Echo on speakers:** the mic re-transcribes the call, so "Them" shows up twice.
  Headphones fix it; otherwise `pactl load-module module-echo-cancel` and point `--mic` at
  the resulting `echo-cancel-source`.
- **Chirp 3 regions:** `us`, `eu`, `asia-northeast1`, `asia-southeast1` are GA;
  `europe-west2/3` are preview.
- **Wayland is irrelevant** here — audio capture needs no portal permission, that's video.
- **Consent:** the project targets UA/EU users, where recording participants without telling
  them is a legal question. The tool is expected to announce it.
