# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

meetscribe transcribes any call on the machine (Zoom, Slack huddles, Meet, Teams, Discord,
a browser tab) by tapping **PipeWire** instead of integrating with each platform's API.
Mic and call audio are captured as two separate tracks, which gives "You" vs "Them"
separation for free — no diarization model needed.

## Repository layout — read this first

`meetscribe/` is the application: `types`, `rotation`, `graph`, `ports`, `vad`, `recorder`,
`tap`, `capture`, `adapters`, `google`, `transcript`, `cli` and `__main__`, wired together in
`cli.py`.

`tests/` holds 145 tests that run with no audio hardware, no network and no credentials —
every subprocess, socket and clock the package touches sits behind a `Protocol` in
`ports.py`, with a real implementation in `adapters.py`/`google.py` and a fake in
`tests/conftest.py`.

`docs/superpowers/specs/` holds the design this package implements, and
`docs/superpowers/plans/` the implementation plan it was built from. `docs/manual-smoke.md`
is a checklist for what the automated tests cannot verify — real audio, real timing, real
shutdown — and is worth running by hand before trusting a change to capture, rotation or
timestamps.

`docs/initial_research/` is **reference material, not the codebase.** It is the research
spike that preceded this package: a write-up plus worked examples, kept to record what the
spike found and prove out the PipeWire approach. It has its own `pyproject.toml` and
`uv.lock`, is not wired up to run, and must not be refactored, extended, or imported from.

The `pyproject.toml` and `uv.lock` at the repository root belong to the real `meetscribe`
package, not to `docs/initial_research/`.

`tests/fixtures/pw_dump_real.json` is **not yet captured.** A schema-regression test against
a real, scrubbed `pw-dump` capture is still outstanding — capturing and scrubbing one (it
would contain a username, hostname, device serials and pids) is the repository owner's job,
not an agent's.

## Commands

**This project uses uv, not pip.** Never call `pip install` or activate `.venv` by hand;
`uv run` does both. `uv.lock` is committed and `uv sync` is what installs from it.

`pyproject.toml` and `uv.lock` live at the repository root — that is the real `meetscribe`
package's manifest, not `docs/initial_research/`'s. Run every command below from the repo
root; there is no need to `cd` or pass `--directory` anywhere.

```bash
uv sync                          # install from uv.lock

uv add <pkg>                     # edits pyproject.toml + uv.lock together
uv lock --upgrade                # refresh the lockfile

# verify the PipeWire toolchain BEFORE debugging anything in Python
pw-cli --version                   # needs >= 0.3.60
pw-dump | head                     # graph as JSON
pw-record --target=0 /tmp/t.wav    # Ctrl-C, then play it back

uv run pytest                                              # 145 tests, no audio/network/creds needed
uv run meetscribe devices                                  # run this MID-CALL
uv run meetscribe run --app zoom --lang uk-UA --lang en-US
uv run python -m meetscribe --help
```

The package has one dependency group, not one extra per engine: `google-cloud-speech` and
`webrtcvad-wheels` are plain dependencies in `pyproject.toml`, because Google Cloud Speech is
the only engine implemented. Both are still imported lazily, inside the function bodies that
need them (`google.py`, `vad.py:webrtc_detector`) rather than at module level — see
Invariants below.

Engine credentials: `GOOGLE_APPLICATION_CREDENTIALS` (service account key path) +
`GOOGLE_CLOUD_PROJECT` (or `--project`).

## Architecture

This is the pipeline as built. The diagram and per-file notes below still use the stage
names from `docs/initial_research/`'s example files (`pipewire.py`, `engines.py`); the
modules that actually implement each stage are the ones listed in Repository layout above
(`graph.py`/`adapters.py`/`recorder.py`/`tap.py`/`capture.py` for capture, `google.py` for
the engine — there is only the one engine, not a pluggable set — `transcript.py` unchanged).

A three-stage pipeline wired together in `cli.py`, with `threading.Queue` as every seam:

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
- **Google's SDK is imported lazily**, inside the function bodies that need it
  (`google.py`: `_config_request`, `stream`, `build_session_factory`) rather than at module
  level, and `webrtcvad` the same way (`vad.py:webrtc_detector`, with a fallback to no-op
  gating if it is missing). Keep new imports of either inside the function body, not at the
  top of the module.
- **Shutdown:** SIGINT sets `stop` + `capture.stop`; `main` joins workers, then *drains
  `out_q`* for finals the engines emitted on the way out, then `writer.close()`. JSONL is
  append-only and flushed per final segment, so an unclean exit still keeps everything.
  `.md` is only rendered in `close()`.
- **Audio time is not elapsed time.** The silence gate (`vad.py`) drops blocks before they
  are sent, so an engine's result offsets count only the audio it actually received.
  `AudioTimeline` in `rotation.py` maps those offsets back onto real capture times. Getting
  this wrong stamps the transcript early and, because the two tracks drop different amounts
  of silence, scrambles the You/Them ordering in the saved Markdown.

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
- **Google caps `StreamingRecognize` at 5 minutes.** `rotation.py:MAX_STREAM_SECONDS` is
  240 s; `EngineWorker.run` (`google.py`) tears the stream down and reopens at that mark,
  carrying `StreamClock.offset` forward so timestamps stay continuous. Without the rotation,
  transcription silently stops mid-meeting.
- **Chirp 3 does not diarize in streaming mode** (only `Recognize`/`BatchRecognize`), and
  this package does not lean on it to separate speakers anyway: capturing mic and system
  audio as two independent tracks (`MIC`/`SYSTEM` in `types.py`) is what gives You/Them
  separation, with its own `SilenceGate` and engine worker per track.
- **VAD gating** drops silence to cut API cost, but `SilenceGate.allows` (`vad.py`)
  deliberately lets `SILENCE_TAIL_BLOCKS` (5) silent blocks through after speech so the
  engine can finalise the utterance.

## Things that bite at runtime

- **Echo on speakers:** the mic re-transcribes the call, so "Them" shows up twice.
  Headphones fix it; otherwise `pactl load-module module-echo-cancel` and point `--mic` at
  the resulting `echo-cancel-source`.
- **Chirp 3 regions:** `us`, `eu`, `asia-northeast1`, `asia-southeast1` are GA;
  `europe-west2/3` are preview.
- **Wayland is irrelevant** here — audio capture needs no portal permission, that's video.
- **Consent:** the project targets UA/EU users, where recording participants without telling
  them is a legal question. The tool is expected to announce it.
