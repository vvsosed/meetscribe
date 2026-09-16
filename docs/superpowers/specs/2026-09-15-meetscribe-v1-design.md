# meetscribe v1 — design

- **Date:** 2026-09-15
- **Status:** approved, ready for implementation planning
- **Supersedes:** nothing. First spec for the application itself.

## Context

The repository contains no application code. `docs/initial_research/` holds a
research spike — example files that proved the PipeWire approach works and
recorded what it taught. That spike is reference material, not a codebase: it
uses relative imports with no `__init__.py`, and its `main.py` imports a
`.capture` module that was never written.

This spec defines the first real version of meetscribe, written clean-room
against the invariants the spike established rather than ported from it.

## Scope

### In

- Linux only, via PipeWire
- Google Cloud Speech-to-Text v2 (`chirp_3`), streaming
- Two capture tracks: microphone, plus either a per-application tap or a
  sink's monitor (the default sink, or whichever `--system` names)
- Live console output, append-only JSONL, rendered Markdown on exit
- Fixture-based tests at every OS and network boundary

### Out (and why)

| Dropped | Reason |
|---|---|
| Non-Linux capture backend | Personal tool, single machine. This is the module the spike never wrote. |
| Deepgram engine | One engine is enough for v1. |
| Local whisper engine | Same. Also removes `faster-whisper` and `ctranslate2`. |
| `numpy` | Only the portable backend and whisper needed it. |
| `Segment.speaker` / diarization | Chirp 3 does not diarize in streaming mode, so the field would be permanently `None`. Track separation already gives You/Them. |
| `--engine` flag | Only one engine exists. |
| Platform-conditional dependency markers | Linux-only makes them dead weight. |

## Architecture

Ports and adapters. Every place the spike reached directly for the OS or the
network becomes an interface with a real implementation and a fake.

| Port | Method | Real | Fake |
|---|---|---|---|
| `GraphSource` | `snapshot() -> PwGraph` | runs `pw-dump` | returns fixture JSON |
| `ProcessLauncher` | `spawn(argv) -> ManagedProcess` | `subprocess.Popen` | scripted PCM on stdout |
| `Linker` | `link(src, dst) -> LinkResult` | runs `pw-link` | records calls |
| `SpeechSession` | `stream(pcm) -> Iterator[Segment]` | gRPC client | replays canned segments |
| `Clock` | `monotonic()`, `sleep()` | `time` | advances on demand |

### Module layout

Line counts are estimates, included to keep any single module from repeating
the spike's 492-line `pipewire.py`.

```
meetscribe/
  __main__.py      #  ~10   python -m meetscribe
  cli.py           # ~110   argparse, wiring, lifecycle, signal handling
  types.py         #  ~30   AudioChunk, Segment, Word — pure values
  ports.py         #  ~40   the five Protocols above
  graph.py         # ~100   pw-dump JSON text -> PwGraph (pure)
  adapters.py      #  ~80   real ports: PwDumpGraphSource, SubprocessLauncher,
                   #        PwLinkLinker, SystemClock, plus the tool-presence check
  recorder.py      #  ~90   argv construction + stdout pump
  tap.py           #  ~70   AppTap link watcher
  capture.py       #  ~90   composes recorder/tap, resolves targets, owns queues
  google.py        # ~130   Chirp 3 adapter
  rotation.py      #  ~80   StreamClock and AudioTimeline — pure time math
  vad.py           #  ~50   silence gating
  transcript.py    # ~110   writer + Markdown rendering
```

`adapters.py` is the only module that shells out. `graph.py` holds the parsing,
`adapters.py` holds the `pw-dump` invocation that feeds it, and the real
`SpeechSession` lives in `google.py` next to its mapping function. Everything
else in the package is reachable in tests without touching the OS.

`graph.py` takes **text** and returns data. Parsing `pw-dump` is where the
fiddly logic lives — serial vs id, the metadata walk for defaults, port
direction — and as a pure function it is testable against a recorded fixture
with no machinery at all.

## Interfaces

### Value types

```python
@dataclass(frozen=True)
class AudioChunk:
    track: str        # "mic" | "system"
    pcm: bytes        # s16 mono 16 kHz — exactly BLOCK_BYTES
    t_start: float    # seconds since capture t0

@dataclass(frozen=True)
class Word:
    word: str
    start: float      # absolute seconds, offset already applied
    end: float

@dataclass(frozen=True)
class Segment:
    track: str
    text: str
    is_final: bool
    t_start: float
    t_end: float
    confidence: float | None = None
    words: tuple[Word, ...] = ()
```

`BLOCK_BYTES` is derived, never written as a literal:
`TARGET_RATE * 2 * BLOCK_MS // 1000` = 3200 at 16 kHz and 100 ms.

### The STT boundary yields domain types

`SpeechSession.stream()` returns `Segment`, not protobufs. The Google-specific
mapping lives inside the adapter as a pure
`segment_from_result(result, offset, track) -> Segment`, unit tested with
hand-built stubs. If the port exposed protobufs, every fake in the suite would
have to construct them merely to say "the engine returned some text".

### Linker returns an enum

`LinkResult` is `LINKED | ALREADY_LINKED | FAILED`. `pw-link` reporting
"File exists" is a benign duplicate, not a failure, and a bool erases that
distinction exactly where it matters.

### Rotation is a value, not a loop variable

```python
@dataclass(frozen=True)
class StreamClock:
    offset: float = 0.0
    max_stream_s: float = 240.0

    def should_rotate(self, stream_age_s: float) -> bool
    def absolute(self, stream_relative_s: float) -> float
    def rotated(self, last_chunk_t: float) -> StreamClock
```

### Audio time is not elapsed time

`StreamClock` alone is not enough, and the first version of this design got
that wrong. The silence gate drops blocks before they are sent, so the
engine's result offsets count **the audio it received**, not elapsed time.
Adding a real-time offset to an audio-relative position stamps the transcript
early by however much silence was dropped.

`AudioTimeline` closes that gap. The worker records the capture time of every
block it actually sends, and the mapping back is a lookup rather than an
addition:

```python
class AudioTimeline:
    offset: float                       # fallback when nothing was sent
    def sent(self, real_t: float) -> None
    def absolute(self, audio_seconds: float) -> float
```

It exposes the same `offset` and `absolute()` surface as `StreamClock`, so the
session and `segment_from_result` are unchanged — the worker simply hands the
timeline to the factory instead of the clock. One timeline per stream, one
float per block sent, discarded at each rotation.

Without it the error grows with gated silence inside each rotation window, and
the two tracks gate different amounts — the microphone while you listen, the
system track while you talk — so they drift apart from each other and the
Markdown, which sorts by timestamp, interleaves You and Them wrongly. That
ordering is the feature the two-track design exists to provide.

Google closes any single `StreamingRecognize` call at 5 minutes. Rotating at
240 s stays safely under it. The failure mode is silent timestamp drift across
a long meeting, which live testing does not catch — hence a pure type that can
be rotated repeatedly in a test.

## Data flow and concurrency

```
                  ┌── recorder(mic) ────► Queue[AudioChunk] ──► engine worker ──┐
 PipeWireCapture ─┤                                                             ├─► Queue[Segment]
                  └── recorder(system) ─► Queue[AudioChunk] ──► engine worker ──┘         │
                             ▲                                                            ▼
                      AppTap watcher                                        TranscriptWriter (main)
                       (only with --app)
```

Threads: one stdout pump per recorder (max 2), one engine worker per track
(max 2), one AppTap watcher when `--app` is given. Five plus main, at most.

One engine worker **per track** is what produces You/Them separation: two
independent Google streams, no diarization model required.

### Backpressure

Queues are bounded at 400 chunks, about 40 seconds of audio. On overflow the
incoming chunk is dropped and counted, with a log line at most once per 100
drops and the total reported at shutdown.

The spike used a blocking `put()`, which stalls the stdout pump and eventually
`pw-record` itself. Audio is lost under either policy if the network is down
for 40 seconds; dropping keeps the pipeline alive and makes the loss visible.

### Shutdown

SIGINT sets the stop event and `capture.stop`. Recorders terminate, engine
workers join with a 3-second timeout, `out_q` drains for finals emitted on the way out,
then the writer closes and renders Markdown. JSONL is append-only and flushed
per final segment, so an unclean exit still keeps everything up to that moment.

## Invariants and how they are pinned

Written clean-room, these traps are easy to lose. Each one gets a test, so
losing it is a red suite rather than a silent regression found mid-meeting.

| Invariant | Test |
|---|---|
| s16 / 16 kHz / mono; PipeWire resamples, never Python | argv asserts `--rate 16000 --channels 1 --format s16`; `BLOCK_BYTES` derived |
| `object.serial`, never `object.id` — ids get reused | fixture with divergent id and serial; lookup must use serial |
| `--properties` values must stay quoted | argv test: a value containing a space survives round-trip |
| `node.autoconnect=false` on the tap node | argv test — otherwise WirePlumber links it to the default mic |
| `stream.capture.sink=true` for monitor capture | argv test |
| App streams appear late (Zoom starts on meeting join), so the graph is re-scanned every 2 s | fake `GraphSource`: empty snapshot, then populated, link on second poll |
| Links are additive and deduped | same snapshot twice yields one `link()` call; `ALREADY_LINKED` is not an error |
| Google's 5-minute cap | `StreamClock` rotated three times; timestamps monotonic and gap-free |
| Gated silence must not shift the timeline | a worker driven with a gating `SilenceGate` and an engine that numbers results from the audio it received |
| VAD keeps a silence tail so finals land | speech then silence: exactly 5 silent blocks pass, then none |
| A stream is never sent nothing | worker keepalive fires both when the gate drops everything and when the queue goes empty entirely |
| JSONL survives an unclean exit | write finals, skip `close()`, assert the file is complete |
| Pipe `read()` can return short | fake process emits short reads; chunks still exactly `BLOCK_BYTES` |
| PipeWire >= 0.3.60 for `target.object` and `stream.capture.sink` | startup check warns and continues |

## Error handling

**Fail fast at startup** when `pw-dump`, `pw-record` or `pw-link` is missing
(with distro install hints), when `--mic` or `--system` cannot be resolved, and
when the capture node does not appear within 5 seconds. That last case means
`pw-record` is broken; the message says so and suggests
`pw-record --target=0 /tmp/t.wav`.

**A missing `--app` match is not an error.** The application legitimately may
not be playing yet — that is the entire reason the watcher exists. Log
`watching for streams matching '<pattern>'` and keep polling.

Two deliberate divergences from the spike:

**Dead tracks must be visible.** If `pw-record` exits mid-session the spike's
pump thread simply ends; the track goes silent for the rest of the meeting
while the tool still reports that it is recording. v1 logs the failure loudly,
marks the track dead, and stops the run if every track is dead rather than
producing an empty transcript.

**Not every Google error is retryable.** The spike retried everything on a
2-second loop, so bad credentials or a missing project spin forever printing
the same warning. Authentication and configuration failures are fatal
immediately. Only transport errors get the backoff: 2 seconds, doubling to a
30-second cap. After five consecutive transport failures the message escalates
to ERROR so a dead network surfaces instead of scrolling past at warning level.

Write errors on the JSONL file propagate. Losing the transcript silently is
worse than crashing.

## Testing

pytest. No audio hardware, no network, no API spend; the suite runs in well
under a second.

```
tests/
  fixtures/
    pw_dump_idle.json          # sinks and sources, no app streams
    pw_dump_zoom_active.json   # the above plus a Stream/Output/Audio node
  conftest.py                  # FakeGraphSource, FakeLauncher,
                               # FakeLinker, FakeClock
  test_graph.py   test_recorder.py   test_tap.py
  test_rotation.py  test_vad.py  test_transcript.py  test_cli.py
```

The fakes are the real deliverable of the port design. `FakeLauncher` records
argv and replays a scripted byte stream including deliberately short reads.
The speech double lives beside its own tests in `test_google.py` rather than in
`conftest.py`: it has to raise on connect and interleave with the rotation
clock, which is specific enough that a shared fake would not serve it. `FakeClock` advances on demand, so the 240-second
rotation and the 2-second tap interval both test in microseconds.

### Fixture capture

Fixtures come off a real machine: run `pw-dump` while an application is
playing. Raw output carries the username, hostname, device serial numbers and
pids, so it must be scrubbed before committing. This is a one-time manual step
performed carefully.

### Not covered by tests

That `pw-link` actually routes audio, that Chirp 3 returns sensible Ukrainian,
and end-to-end audio fidelity. These need a real meeting. The complement is a
manual smoke checklist kept in the repo: run `devices` mid-call, run with
`--app`, confirm both tracks produce text, Ctrl-C, inspect the Markdown.

## Packaging and CLI

`pyproject.toml` and `uv.lock` live at the repository root for the real
package. `[tool.uv] package = false` is removed and
`[project.scripts] meetscribe = "meetscribe.cli:main"` is added, so
`uv run meetscribe` works alongside `python -m meetscribe`.

- Runtime dependencies: `google-cloud-speech`, `webrtcvad-wheels`
- Dev dependency group: `pytest`
- `requires-python = ">=3.11"`

`docs/initial_research/` keeps its own manifest untouched. It still references
all three engines and remains reference material.

```
meetscribe devices
meetscribe run [--app NAME | --system NAME] [--mic NAME] [--no-mic] [--no-system]
               [--lang CODE]...  [--region eu] [--project ID] [--phrase TERM]...
               [--model chirp_3] [--latency 100ms] [--out DIR] [--no-interim] [-v]
```

Defaults: `--region eu`, `--model chirp_3`, `--lang en-US` (repeatable for
multilingual recognition), `--latency 100ms`, `--out transcripts`. Both `--mic`
and `--system` accept a node name or any substring of one.

`devices` is run mid-call, since applications only appear in the graph once
they are actually playing audio.

## Operational notes

These affect how the tool is used rather than how it is built, and belong in
the README rather than the code:

- **Echo.** Without headphones the microphone re-transcribes the call, so the
  other party appears on both tracks. `module-echo-cancel` and pointing
  `--mic` at the resulting `echo-cancel-source` is the workaround.
- **Chirp 3 regions.** `us`, `eu`, `asia-northeast1` and `asia-southeast1` are
  GA; `europe-west2` and `europe-west3` are preview. Default to `eu`.
- **Consent.** Recording participants without telling them is a legal question
  in Ukraine and the EU. The tool announces that it is recording.
