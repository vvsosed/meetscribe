# meetscribe v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working version of meetscribe — a console tool that transcribes a live call on Linux by capturing PipeWire audio on two tracks (your mic, and either one application or everything you hear) and streaming both to Google Chirp 3.

**Architecture:** Ports and adapters. Every place the code would touch the OS or the network sits behind a Protocol (`GraphSource`, `ProcessLauncher`, `Linker`, `SpeechSession`, `Clock`) with a real implementation and a fake, so the whole pipeline is testable with no audio hardware, no network and no API spend. Audio flows capture → one bounded queue per track → one engine worker per track → a single segment queue → the transcript writer on the main thread.

**Tech Stack:** Python 3.11+, uv, pytest, PipeWire CLI tools (`pw-dump`, `pw-record`, `pw-link`), `google-cloud-speech` (Speech-to-Text v2, `chirp_3`), `webrtcvad-wheels`.

**Spec:** `docs/superpowers/specs/2026-09-15-meetscribe-v1-design.md`

---

## Background for the implementer

You do not need to know PipeWire going in. Five facts explain most of the design:

1. **PipeWire is a graph.** `pw-dump` prints every node (devices, and each app's audio stream) and port as JSON. You find things by filtering `media.class`: `Audio/Sink` is an output device, `Audio/Source` a microphone, `Stream/Output/Audio` an application currently playing sound.
2. **One output port can feed many input ports.** That is why this tool exists: we can link Zoom's output into our recorder *in addition to* its existing link to your speakers. Zoom keeps playing normally and we get an identical copy.
3. **`pw-record` does the hard parts.** It resamples to 16 kHz mono s16 for us and writes raw PCM to stdout. We never touch sample-rate conversion in Python.
4. **Applications appear late.** Zoom creates its audio stream when the meeting starts, not when the app launches. Anything that resolves nodes once at startup records silence. Hence a watcher that re-scans every 2 seconds.
5. **Identify nodes by `object.serial`, never `object.id`.** Ids get recycled when nodes come and go; serials do not.

Two rules that will bite you if you forget them:

- `pw-record --properties` takes a JSON-ish string. Every value must stay quoted, or an unquoted space silently splits the property in two and the flag is ignored.
- A single Google `StreamingRecognize` call is closed by the server at 5 minutes. Meetings are longer, so we tear the stream down at 4 minutes and open a fresh one, carrying a time offset forward so timestamps stay continuous.

## File structure

Every file has one responsibility. `adapters.py` is the **only** module that shells out to a subprocess, and `google.py` is the only one that talks to the network — that boundary is what makes the rest testable.

| File | Responsibility |
|---|---|
| `pyproject.toml` | Root package manifest, deps, `meetscribe` entry point |
| `meetscribe/__init__.py` | Empty marker |
| `meetscribe/__main__.py` | `python -m meetscribe` entry |
| `meetscribe/types.py` | `AudioChunk`, `Word`, `Segment`, audio constants. No imports beyond stdlib |
| `meetscribe/ports.py` | The five Protocols plus `LinkResult` |
| `meetscribe/graph.py` | Parse `pw-dump` text into `PwGraph`. Pure — takes a string |
| `meetscribe/rotation.py` | `StreamClock`: offset math across stream rotations. Pure |
| `meetscribe/vad.py` | `SilenceGate`: drop silence, keep a tail so finals land |
| `meetscribe/recorder.py` | Build `pw-record` argv; frame its stdout into fixed blocks |
| `meetscribe/tap.py` | `AppTap`: watch the graph, link matching app streams to our node |
| `meetscribe/capture.py` | Resolve targets, own recorders and queues, start/stop |
| `meetscribe/google.py` | Chirp 3 adapter, result mapping, retry/rotation worker loop |
| `meetscribe/transcript.py` | Live console line, append-only JSONL, Markdown render |
| `meetscribe/adapters.py` | Real ports: run `pw-dump`/`pw-record`/`pw-link`, system clock |
| `meetscribe/cli.py` | argparse, wiring, signal handling, shutdown drain |
| `tests/conftest.py` | The fakes: graph source, launcher, linker, clock, speech |
| `tests/fixtures/*.json` | Hand-authored `pw-dump` samples |

Build order is bottom-up: pure modules first (they need nothing), then modules that take ports, then the adapters, then the CLI that wires it all together.

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `meetscribe/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_smoke.py`:

```python
def test_package_imports():
    import meetscribe

    assert meetscribe is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_smoke.py -v`

Expected: FAIL — the project has no `pyproject.toml` at the root yet, so `uv run` errors with `No \`pyproject.toml\` found`.

- [ ] **Step 3: Create the manifest**

Create `pyproject.toml` at the repository root. Note this is a *new* file — `docs/initial_research/pyproject.toml` belongs to the research examples and is left completely alone.

```toml
[project]
name = "meetscribe"
version = "0.1.0"
description = "Live transcription of any call on your machine, captured from PipeWire"
requires-python = ">=3.11"

dependencies = [
    "google-cloud-speech>=2.27.0",
    "webrtcvad-wheels>=2.0.14",
]

[project.scripts]
meetscribe = "meetscribe.cli:main"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["meetscribe"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Create the package and test packages**

```bash
mkdir -p meetscribe tests/fixtures
touch meetscribe/__init__.py tests/__init__.py
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_smoke.py -v`

Expected: PASS, 1 passed. uv creates `.venv/` and installs the dependencies on first run.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock meetscribe/__init__.py tests/__init__.py tests/test_smoke.py
git commit -m "feat: scaffold meetscribe package with uv and pytest"
```

---

## Task 2: Value types

**Files:**
- Create: `meetscribe/types.py`
- Create: `tests/test_types.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_types.py`:

```python
import dataclasses

import pytest

from meetscribe.types import BLOCK_BYTES, BLOCK_MS, TARGET_RATE, AudioChunk, Segment, Word


def test_block_bytes_is_derived_not_hardcoded():
    # 100 ms of 16 kHz mono signed 16-bit audio.
    assert BLOCK_BYTES == TARGET_RATE * 2 * BLOCK_MS // 1000
    assert BLOCK_BYTES == 3200


def test_audio_chunk_is_immutable():
    chunk = AudioChunk(track="mic", pcm=b"\x00" * BLOCK_BYTES, t_start=1.5)

    assert chunk.track == "mic"
    assert len(chunk.pcm) == BLOCK_BYTES

    # frozen=True is what stops a producer mutating a chunk it has already
    # handed to a queue, so pin it rather than assuming it.
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.track = "system"


def test_segment_is_immutable():
    seg = Segment(track="mic", text="hello", is_final=True, t_start=0.0, t_end=1.0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        seg.text = "changed"


def test_segment_defaults_to_no_words():
    seg = Segment(track="system", text="hello", is_final=True, t_start=0.0, t_end=1.0)

    assert seg.words == ()
    assert seg.confidence is None


def test_segment_carries_words():
    seg = Segment(
        track="mic",
        text="hi there",
        is_final=True,
        t_start=0.0,
        t_end=1.0,
        words=(Word(word="hi", start=0.0, end=0.4), Word(word="there", start=0.4, end=1.0)),
    )

    assert [w.word for w in seg.words] == ["hi", "there"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_types.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.types'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/types.py`:

```python
"""Value types shared by every layer. Imports nothing but the standard library."""

from __future__ import annotations

from dataclasses import dataclass

TARGET_RATE = 16_000
BLOCK_MS = 100
# pw-record hands us signed 16-bit mono, so two bytes per sample.
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000

MIC = "mic"
SYSTEM = "system"


@dataclass(frozen=True)
class AudioChunk:
    track: str
    pcm: bytes
    t_start: float


@dataclass(frozen=True)
class Word:
    word: str
    start: float
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_types.py -v`

Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/types.py tests/test_types.py
git commit -m "feat: add core value types and audio constants"
```

---

## Task 3: StreamClock rotation math

Google closes any single streaming call at 5 minutes. We rotate at 4. The bug this guards against is silent timestamp drift across a long meeting — you would never notice it by running the tool, which is exactly why it gets a pure type and its own tests.

**Files:**
- Create: `meetscribe/rotation.py`
- Create: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rotation.py`:

```python
from meetscribe.rotation import MAX_STREAM_SECONDS, StreamClock


def test_rotation_threshold_stays_under_googles_five_minute_cap():
    assert MAX_STREAM_SECONDS < 300


def test_does_not_rotate_before_the_limit():
    clock = StreamClock()

    assert clock.should_rotate(239.0) is False
    assert clock.should_rotate(240.0) is True


def test_absolute_applies_the_offset():
    clock = StreamClock(offset=100.0)

    assert clock.absolute(5.0) == 105.0


def test_timestamps_stay_continuous_across_three_rotations():
    clock = StreamClock()
    seen = []

    # Each stream runs for 240 s of audio, reporting times relative to itself.
    for _ in range(3):
        for relative in (10.0, 120.0, 239.0):
            seen.append(clock.absolute(relative))
        clock = clock.rotated(last_chunk_t=clock.absolute(240.0))

    assert seen == sorted(seen), "timestamps must never go backwards"
    assert seen[0] == 10.0
    assert seen[3] == 250.0
    assert seen[6] == 490.0


def test_rotation_never_moves_the_offset_backwards():
    clock = StreamClock(offset=500.0)

    # A late or duplicated chunk reporting an earlier time must not rewind us.
    assert clock.rotated(last_chunk_t=10.0).offset == 500.0


def test_rotation_preserves_the_configured_interval():
    # Losing max_stream_s here would silently reset the rotation interval to
    # the 240 s default after the first rotation, with no other test noticing.
    clock = StreamClock(max_stream_s=1.0)

    rotated = clock.rotated(last_chunk_t=5.0)

    assert rotated.max_stream_s == 1.0
    assert rotated.should_rotate(1.0) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.rotation'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/rotation.py`:

```python
"""Offset bookkeeping across rotated recognition streams.

A single Google StreamingRecognize call is closed by the server at five
minutes. We tear ours down at four and open a fresh one. Each new stream
reports timestamps relative to itself, so we carry an offset forward to keep
the transcript on one continuous timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

MAX_STREAM_SECONDS = 240.0


@dataclass(frozen=True)
class StreamClock:
    offset: float = 0.0
    max_stream_s: float = MAX_STREAM_SECONDS

    def should_rotate(self, stream_age_s: float) -> bool:
        return stream_age_s >= self.max_stream_s

    def absolute(self, stream_relative_s: float) -> float:
        """Map a time reported by the current stream onto the session timeline."""
        return self.offset + stream_relative_s

    def rotated(self, last_chunk_t: float) -> StreamClock:
        """Clock for the next stream. max() guards against rewinding.

        `last_chunk_t` must already be on the session-absolute timeline — a
        raw `AudioChunk.t_start`, not a time reported by the closing stream.
        Do not pass it through `absolute()` first: that double-applies the
        offset and compounds on every rotation.
        """
        return replace(self, offset=max(self.offset, last_chunk_t))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rotation.py -v`

Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/rotation.py tests/test_rotation.py
git commit -m "feat: add StreamClock for continuous timestamps across rotations"
```

---

## Task 4: Parse the PipeWire graph

`pw-dump` emits a JSON array mixing nodes, ports and metadata. This module turns that text into data and nothing else — it never runs a subprocess, which is what makes it testable against a fixture.

**Files:**
- Create: `tests/fixtures/pw_dump_idle.json`
- Create: `tests/fixtures/pw_dump_zoom_active.json`
- Create: `meetscribe/graph.py`
- Create: `tests/test_graph.py`

- [ ] **Step 1: Create the fixtures**

These are hand-authored and deliberately minimal. Note that `id` and `object.serial` differ everywhere — that divergence is the point of one of the tests below. Task 16 covers capturing a real `pw-dump` later as a schema regression guard.

Create `tests/fixtures/pw_dump_idle.json`:

```json
[
  {
    "id": 40,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1001,
        "media.class": "Audio/Sink",
        "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
        "node.description": "Built-in Audio Analog Stereo"
      }
    }
  },
  {
    "id": 42,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1002,
        "media.class": "Audio/Source",
        "node.name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "node.description": "Built-in Audio Analog Stereo Microphone"
      }
    }
  },
  {
    "id": 43,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1003,
        "media.class": "Audio/Source",
        "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        "node.description": "Monitor of Built-in Audio"
      }
    }
  },
  {
    "id": 44,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1004,
        "node.name": "node-without-media-class"
      }
    }
  },
  {
    "id": 50,
    "type": "PipeWire:Interface:Port",
    "info": {
      "props": {
        "node.id": 40,
        "port.name": "playback_FL",
        "port.direction": "in",
        "audio.channel": "FL"
      }
    }
  },
  {
    "id": 51,
    "type": "PipeWire:Interface:Port",
    "info": {
      "props": {
        "node.id": 40,
        "port.name": "monitor_FL",
        "port.direction": "out",
        "audio.channel": "FL"
      }
    }
  },
  {
    "id": 99,
    "type": "PipeWire:Interface:Metadata",
    "props": { "metadata.name": "default" },
    "metadata": [
      {
        "key": "default.audio.sink",
        "value": { "name": "alsa_output.pci-0000_00_1f.3.analog-stereo" }
      },
      {
        "key": "default.audio.source",
        "value": { "name": "alsa_input.pci-0000_00_1f.3.analog-stereo" }
      }
    ]
  }
]
```

Create `tests/fixtures/pw_dump_zoom_active.json` — the same graph plus a live Zoom stream and its two output ports:

```json
[
  {
    "id": 40,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1001,
        "media.class": "Audio/Sink",
        "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
        "node.description": "Built-in Audio Analog Stereo"
      }
    }
  },
  {
    "id": 42,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1002,
        "media.class": "Audio/Source",
        "node.name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "node.description": "Built-in Audio Analog Stereo Microphone"
      }
    }
  },
  {
    "id": 55,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1204,
        "media.class": "Stream/Output/Audio",
        "node.name": "ZOOM VoiceEngine",
        "node.description": "ZOOM VoiceEngine",
        "application.name": "ZOOM VoiceEngine",
        "application.process.binary": "zoom",
        "application.process.id": 44321
      }
    }
  },
  {
    "id": 56,
    "type": "PipeWire:Interface:Node",
    "info": {
      "props": {
        "object.serial": 1250,
        "media.class": "Stream/Output/Audio",
        "node.name": "spotify",
        "node.description": "Spotify",
        "application.name": "Spotify",
        "application.process.binary": "spotify",
        "application.process.id": 9001
      }
    }
  },
  {
    "id": 60,
    "type": "PipeWire:Interface:Port",
    "info": {
      "props": {
        "node.id": 55,
        "port.name": "output_FL",
        "port.direction": "out",
        "audio.channel": "FL"
      }
    }
  },
  {
    "id": 61,
    "type": "PipeWire:Interface:Port",
    "info": {
      "props": {
        "node.id": 55,
        "port.name": "output_FR",
        "port.direction": "out",
        "audio.channel": "FR"
      }
    }
  },
  {
    "id": 62,
    "type": "PipeWire:Interface:Port",
    "info": {
      "props": {
        "node.id": 56,
        "port.name": "output_FL",
        "port.direction": "out",
        "audio.channel": "FL"
      }
    }
  },
  {
    "id": 99,
    "type": "PipeWire:Interface:Metadata",
    "props": { "metadata.name": "default" },
    "metadata": [
      {
        "key": "default.audio.sink",
        "value": { "name": "alsa_output.pci-0000_00_1f.3.analog-stereo" }
      },
      {
        "key": "default.audio.source",
        "value": { "name": "alsa_input.pci-0000_00_1f.3.analog-stereo" }
      }
    ]
  }
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_graph.py`:

```python
import json
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


def test_serial_falls_back_to_id_with_a_warning(caplog):
    # Degrading is better than crashing, but it must not happen silently:
    # a recycled id can point pw-record at the wrong stream mid-meeting.
    dump = json.dumps(
        [
            {
                "id": 77,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Audio/Sink",
                        "node.name": "sink-without-serial",
                    }
                },
            }
        ]
    )

    graph = parse_graph(dump)

    assert graph.node_by_name("sink-without-serial").serial == 77
    assert "no object.serial" in caplog.text


def test_empty_dump_yields_an_empty_graph():
    graph = parse_graph("[]")

    assert graph.nodes == ()
    assert graph.ports == ()
    assert graph.default_sink is None
    assert graph.default_source is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_graph.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.graph'`.

- [ ] **Step 4: Write the implementation**

Create `meetscribe/graph.py`:

```python
"""Turn `pw-dump` output into data.

Pure on purpose: this module takes a string and returns a PwGraph. Running
pw-dump is adapters.py's job, which is what lets every test here work off a
fixture with no PipeWire session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

# media.class values, per pipewire-props(7)
SINK = "Audio/Sink"
SOURCE = "Audio/Source"
PLAYBACK_STREAM = "Stream/Output/Audio"  # an application producing sound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PwNode:
    id: int
    serial: int
    name: str
    description: str
    media_class: str
    app_name: str | None = None
    app_binary: str | None = None
    pid: int | None = None

    @property
    def label(self) -> str:
        return self.app_name or self.description or self.name

    def matches(self, needle: str) -> bool:
        lowered = needle.lower()
        fields = (self.name, self.description, self.app_name, self.app_binary)
        return any(lowered in (value or "").lower() for value in fields)


@dataclass(frozen=True)
class PwPort:
    id: int
    node_id: int
    name: str
    direction: str  # "out" | "in"


@dataclass(frozen=True)
class PwGraph:
    nodes: tuple[PwNode, ...] = ()
    ports: tuple[PwPort, ...] = ()
    default_sink: str | None = None
    default_source: str | None = None

    def by_class(self, media_class: str) -> tuple[PwNode, ...]:
        return tuple(n for n in self.nodes if n.media_class == media_class)

    def node_by_name(self, name: str) -> PwNode | None:
        return next((n for n in self.nodes if n.name == name), None)

    def find(self, needle: str, media_class: str | None = None) -> PwNode | None:
        pool = self.by_class(media_class) if media_class else self.nodes
        exact = next((n for n in pool if n.name == needle), None)
        return exact or next((n for n in pool if n.matches(needle)), None)

    def ports_of(self, node_id: int, direction: str) -> tuple[PwPort, ...]:
        matching = (
            p for p in self.ports if p.node_id == node_id and p.direction == direction
        )
        return tuple(sorted(matching, key=lambda p: p.name))


def parse_graph(dump_text: str) -> PwGraph:
    nodes: list[PwNode] = []
    ports: list[PwPort] = []
    default_sink: str | None = None
    default_source: str | None = None

    for obj in json.loads(dump_text):
        obj_type = obj.get("type", "")
        props = (obj.get("info") or {}).get("props") or {}

        if obj_type.endswith("Interface:Node"):
            media_class = props.get("media.class")
            if not media_class:
                continue  # links, filters and other plumbing we do not care about
            serial = props.get("object.serial")
            if serial is None:
                serial = obj["id"]
                log.warning(
                    "node %r has no object.serial; falling back to id %s, which "
                    "PipeWire recycles - a long capture targeting it may end up "
                    "on the wrong stream",
                    props.get("node.name", ""),
                    serial,
                )
            nodes.append(
                PwNode(
                    id=obj["id"],
                    serial=serial,
                    name=props.get("node.name", ""),
                    description=props.get("node.description", ""),
                    media_class=media_class,
                    app_name=props.get("application.name"),
                    app_binary=props.get("application.process.binary"),
                    pid=props.get("application.process.id"),
                )
            )

        elif obj_type.endswith("Interface:Port"):
            ports.append(
                PwPort(
                    id=obj["id"],
                    node_id=props.get("node.id", -1),
                    name=props.get("port.name", ""),
                    direction=props.get("port.direction", ""),
                )
            )

        elif obj_type.endswith("Interface:Metadata"):
            if (obj.get("props") or {}).get("metadata.name") != "default":
                continue
            for entry in obj.get("metadata") or []:
                value = entry.get("value")
                name = value.get("name") if isinstance(value, dict) else value
                if entry.get("key") == "default.audio.sink":
                    default_sink = name
                elif entry.get("key") == "default.audio.source":
                    default_source = name

    return PwGraph(tuple(nodes), tuple(ports), default_sink, default_source)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_graph.py -v`

Expected: PASS, 11 passed.

- [ ] **Step 6: Commit**

```bash
git add meetscribe/graph.py tests/test_graph.py tests/fixtures/
git commit -m "feat: parse pw-dump output into a PwGraph"
```

---

## Task 5: Ports and the test fakes

The Protocols are trivial. The fakes are the real deliverable — every later task depends on them, so they are written once here.

**Files:**
- Create: `meetscribe/ports.py`
- Create: `tests/conftest.py`
- Create: `tests/test_ports.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ports.py`:

```python
from meetscribe.graph import PwGraph
from meetscribe.ports import LinkResult
from tests.conftest import FakeGraphSource


def test_link_result_distinguishes_duplicate_from_failure():
    # pw-link reporting "File exists" means the pair is already connected,
    # which is benign. A bool would erase that distinction.
    assert LinkResult.ALREADY_LINKED is not LinkResult.FAILED
    assert LinkResult.ALREADY_LINKED is not LinkResult.LINKED


def test_fakes_expose_the_protocol_methods(fake_launcher, fake_linker, fake_clock):
    # @runtime_checkable only verifies that the method NAMES exist. It checks
    # neither signatures nor return types, so this pins the shape of the fakes,
    # not their contracts.
    from meetscribe.ports import (
        Clock,
        GraphSource,
        Linker,
        ManagedProcess,
        ProcessLauncher,
    )

    assert isinstance(fake_launcher, ProcessLauncher)
    assert isinstance(fake_linker, Linker)
    assert isinstance(fake_clock, Clock)
    assert isinstance(fake_launcher.spawn(["pw-record"]), ManagedProcess)
    assert isinstance(FakeGraphSource(PwGraph()), GraphSource)


def test_fake_clock_advances_on_sleep(fake_clock):
    assert fake_clock.monotonic() == 0.0

    fake_clock.sleep(2.0)
    fake_clock.sleep(2.0)

    assert fake_clock.monotonic() == 4.0
    assert fake_clock.slept == [2.0, 2.0]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_ports.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.ports'`.

- [ ] **Step 3: Write the ports**

Create `meetscribe/ports.py`:

```python
"""The boundaries between this program and the outside world.

Every Protocol here has exactly one real implementation (adapters.py, or
google.py for SpeechSession) and one fake (tests/conftest.py). Nothing else in
the package touches a subprocess, a socket or the wall clock.
"""

from __future__ import annotations

from enum import Enum
from typing import BinaryIO, Iterator, Protocol, Sequence, runtime_checkable

from .graph import PwGraph
from .types import Segment


class LinkResult(Enum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    FAILED = "failed"


@runtime_checkable
class GraphSource(Protocol):
    def snapshot(self) -> PwGraph: ...


@runtime_checkable
class ManagedProcess(Protocol):
    @property
    def stdout(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def stderr_text(self) -> str: ...


@runtime_checkable
class ProcessLauncher(Protocol):
    def spawn(self, argv: Sequence[str]) -> ManagedProcess: ...


@runtime_checkable
class Linker(Protocol):
    def link(self, src_port: int, dst_port: int) -> LinkResult: ...


@runtime_checkable
class SpeechSession(Protocol):
    def stream(self, pcm: Iterator[bytes]) -> Iterator[Segment]: ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...
```

- [ ] **Step 4: Write the fakes**

Create `tests/conftest.py`:

```python
"""Fakes for every port, plus fixture loading helpers."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import pytest

from meetscribe.graph import PwGraph, parse_graph
from meetscribe.ports import LinkResult

FIXTURES = Path(__file__).parent / "fixtures"


def load_graph(name: str) -> PwGraph:
    return parse_graph((FIXTURES / name).read_text())


class ChunkedBytesIO(io.BytesIO):
    """A stream whose read() returns short, the way a real pipe does."""

    def __init__(self, data: bytes, max_read: int):
        super().__init__(data)
        self._max_read = max_read

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            return super().read()
        return super().read(min(size, self._max_read))


class FakeProcess:
    def __init__(self, stdout: io.BytesIO, stderr: str = ""):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    @property
    def stdout(self) -> io.BytesIO:
        return self._stdout

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        # A real SIGTERM exit is -15, not a clean 0. Recorder.failure() treats
        # 0 as "no failure", so reporting 0 here would hide the path Task 11's
        # shutdown guard actually depends on.
        self.returncode = -15

    def stderr_text(self) -> str:
        return self._stderr

    def die(self, returncode: int = 1, stderr: str = "boom") -> None:
        """Simulate pw-record exiting mid-session."""
        self.returncode = returncode
        self._stderr = stderr


class FakeLauncher:
    def __init__(self, script: bytes = b"", max_read: int | None = None):
        self.script = script
        self.max_read = max_read
        self.calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.calls.append(list(argv))
        stream: io.BytesIO
        if self.max_read is None:
            stream = io.BytesIO(self.script)
        else:
            stream = ChunkedBytesIO(self.script, self.max_read)
        process = FakeProcess(stream)
        self.processes.append(process)
        return process


class FakeGraphSource:
    """Returns each snapshot in turn, then repeats the last one forever."""

    def __init__(self, *snapshots: PwGraph):
        assert snapshots, "give FakeGraphSource at least one snapshot"
        self._queue = list(snapshots)
        self.calls = 0

    def snapshot(self) -> PwGraph:
        self.calls += 1
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]


class FakeLinker:
    def __init__(self, result: LinkResult = LinkResult.LINKED):
        self.result = result
        self.links: list[tuple[int, int]] = []

    def link(self, src_port: int, dst_port: int) -> LinkResult:
        self.links.append((src_port, dst_port))
        return self.result


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def fake_linker() -> FakeLinker:
    return FakeLinker()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def idle_graph() -> PwGraph:
    return load_graph("pw_dump_idle.json")


@pytest.fixture
def zoom_graph() -> PwGraph:
    return load_graph("pw_dump_zoom_active.json")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports.py -v`

Expected: PASS, 3 passed.

- [ ] **Step 6: Commit**

```bash
git add meetscribe/ports.py tests/conftest.py tests/test_ports.py
git commit -m "feat: define ports and test fakes for every OS boundary"
```

---

## Task 6: Silence gating

Streaming silence to a metered API is pure waste, but gating too hard is worse: the engine needs a little trailing silence to decide an utterance has ended and emit a final result. So we pass speech through plus a short tail.

The detector is injected as a plain callable, which keeps the tests free of real audio.

**Files:**
- Create: `meetscribe/vad.py`
- Create: `tests/test_vad.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_vad.py`:

```python
import random
import struct

from meetscribe.types import BLOCK_BYTES
from meetscribe.vad import SILENCE_TAIL_BLOCKS, SilenceGate, webrtc_detector

SPEECH = b"\x01" * BLOCK_BYTES
QUIET = b"\x00" * BLOCK_BYTES


def detector(pcm: bytes) -> bool:
    """Stand-in for webrtcvad: non-zero bytes count as speech."""
    return pcm != QUIET


def test_passes_everything_when_no_detector_is_available():
    gate = SilenceGate(detector=None)

    assert all(gate.allows(QUIET) for _ in range(100))


def test_passes_speech():
    gate = SilenceGate(detector=detector)

    assert gate.allows(SPEECH) is True


def test_passes_a_silence_tail_then_stops():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)

    passed = [gate.allows(QUIET) for _ in range(SILENCE_TAIL_BLOCKS + 3)]

    # The tail lets the engine finalise the utterance; after that we stop
    # paying to transmit dead air.
    assert passed[:SILENCE_TAIL_BLOCKS] == [True] * SILENCE_TAIL_BLOCKS
    assert passed[SILENCE_TAIL_BLOCKS:] == [False, False, False]


# Added after the live runs: past this tail nothing is sent, and Google ends a
# stream it receives nothing on ("409 Stream timed out after receiving no more
# client requests"). The keepalive that prevents that is NOT here - it is in
# EngineWorker.blocks(), because the audio can also stop upstream of the gate
# entirely. See google.py:KEEPALIVE_S.


def test_tail_resets_when_speech_resumes():
    gate = SilenceGate(detector=detector)
    gate.allows(SPEECH)
    for _ in range(SILENCE_TAIL_BLOCKS + 2):
        gate.allows(QUIET)

    assert gate.allows(SPEECH) is True
    assert gate.allows(QUIET) is True  # tail counter went back to zero


def test_frame_size_divides_a_block_evenly():
    from meetscribe.vad import FRAME_BYTES

    # webrtcvad only accepts 10, 20 or 30 ms frames, so a 100 ms block has to
    # split into whole frames or the last one is silently dropped.
    assert BLOCK_BYTES % FRAME_BYTES == 0


def noisy_block() -> bytes:
    """A deterministic block of loud noise, which a VAD should hear as speech."""
    rng = random.Random(0)
    samples = BLOCK_BYTES // 2
    return struct.pack(
        f"<{samples}h", *(rng.randint(-20000, 20000) for _ in range(samples))
    )


def test_real_detector_hears_speech_in_a_loud_block():
    # The stand-in detector used above never runs webrtc_detector's
    # frame-slicing loop. This does, and it has to be the positive case:
    # asserting only that silence is quiet would pass even if the loop
    # never ran, since any([]) is False.
    detect = webrtc_detector()
    assert detect is not None, "webrtcvad-wheels is a hard dependency"

    assert detect(noisy_block()) is True


def test_real_detector_reports_silence_as_quiet():
    # A FRESH detector on purpose: webrtcvad.Vad carries adaptive state
    # across calls, so reusing the instance from the test above could report
    # this silence as speech on hangover.
    detect = webrtc_detector()
    assert detect is not None

    assert detect(b"\x00" * BLOCK_BYTES) is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_vad.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.vad'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/vad.py`:

```python
"""Voice activity gating.

Silence costs money on a metered engine, but cutting it off entirely stops the
engine from finalising utterances. We pass speech plus a short tail.
"""

from __future__ import annotations

import logging
from typing import Callable

from .types import TARGET_RATE

log = logging.getLogger(__name__)

# webrtcvad accepts 10, 20 or 30 ms frames only.
FRAME_MS = 20
FRAME_BYTES = TARGET_RATE * 2 * FRAME_MS // 1000
SILENCE_TAIL_BLOCKS = 5

SpeechDetector = Callable[[bytes], bool]


def webrtc_detector(aggressiveness: int = 2) -> SpeechDetector | None:
    """Real detector, or None when webrtcvad is unavailable.

    `aggressiveness` runs 0-3, higher filtering more non-speech. 2 is a
    middle setting chosen for meeting audio, where fans and keyboards are
    common but clipping a quiet speaker costs more than a little extra
    streamed silence. It is a starting point, not a measured optimum.

    Note the returned closure is stateful: webrtcvad adapts to the noise
    floor across calls, so give each track its own detector rather than
    sharing one.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning(
            "webrtcvad not installed - silence will be streamed too, which "
            "costs more. Run: uv sync"
        )
        return None

    vad = webrtcvad.Vad(aggressiveness)

    def detect(pcm: bytes) -> bool:
        return any(
            vad.is_speech(pcm[i : i + FRAME_BYTES], TARGET_RATE)
            for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        )

    return detect


class SilenceGate:
    def __init__(
        self,
        detector: SpeechDetector | None,
        tail_blocks: int = SILENCE_TAIL_BLOCKS,
    ):
        self._detect = detector
        self._tail_blocks = tail_blocks
        self._silence_run = 0

    def allows(self, pcm: bytes) -> bool:
        if self._detect is None:
            return True
        if self._detect(pcm):
            self._silence_run = 0
            return True
        self._silence_run += 1
        return self._silence_run <= self._tail_blocks
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vad.py -v`

Expected: PASS, 7 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/vad.py tests/test_vad.py
git commit -m "feat: add silence gating with a finalisation tail"
```

---

## Task 7: Build the pw-record command line

Getting this string wrong is the single easiest way to waste an afternoon. `--properties` is parsed as JSON-ish, so an unquoted value containing a space splits into two properties and the flag is silently ignored.

**Files:**
- Create: `meetscribe/recorder.py`
- Create: `tests/test_recorder.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_recorder.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_recorder.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.recorder'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/recorder.py`:

```python
"""Construct and drive a pw-record subprocess.

pw-record resamples to our target format and writes raw PCM to stdout, which
is why nothing in this package does sample-rate conversion.
"""

from __future__ import annotations

from .types import TARGET_RATE

PW_RECORD = "pw-record"


def format_properties(props: dict[str, str]) -> str:
    """Render the --properties argument.

    PipeWire parses this as JSON-ish. Every value must stay quoted: an
    unquoted space inside a value splits the property and it is dropped
    without any error message.
    """
    body = " ".join(f'{key}="{value}"' for key, value in props.items())
    return "{ " + body + " }"


def build_argv(
    *,
    node_name: str,
    media_name: str,
    target: int | None = None,
    capture_sink: bool = False,
    autoconnect: bool = True,
    latency: str = "100ms",
) -> list[str]:
    props = {"node.name": node_name, "media.name": media_name}
    if capture_sink:
        props["stream.capture.sink"] = "true"
    if not autoconnect:
        props["node.autoconnect"] = "false"

    argv = [
        PW_RECORD,
        "--rate",
        str(TARGET_RATE),
        "--channels",
        "1",
        "--format",
        "s16",
        "--latency",
        latency,
        "--properties",
        format_properties(props),
        "--raw",
    ]
    if target is not None:
        argv += ["--target", str(target)]
    argv.append("-")
    return argv
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_recorder.py -v`

Expected: PASS, 6 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/recorder.py tests/test_recorder.py
git commit -m "feat: build pw-record argv with correctly quoted properties"
```

---

## Task 8: Frame the recorder's stdout

`read()` on a pipe returns whatever is available, which is frequently less than you asked for. Everything downstream assumes whole blocks, so the framing happens here, once.

**Files:**
- Modify: `meetscribe/recorder.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recorder.py`:

```python
import io

from meetscribe.recorder import Recorder, RecorderSpec, read_blocks
from meetscribe.types import BLOCK_BYTES
from tests.conftest import ChunkedBytesIO


def test_reads_whole_blocks():
    stream = io.BytesIO(b"\x01" * (BLOCK_BYTES * 3))

    blocks = list(read_blocks(stream))

    assert len(blocks) == 3
    assert all(len(b) == BLOCK_BYTES for b in blocks)


def test_reassembles_short_reads():
    # A real pipe hands back 700 bytes when you ask for 3200.
    stream = ChunkedBytesIO(b"\x01" * (BLOCK_BYTES * 2), max_read=700)

    blocks = list(read_blocks(stream))

    assert len(blocks) == 2
    assert all(len(b) == BLOCK_BYTES for b in blocks)


def test_drops_a_partial_trailing_block():
    stream = io.BytesIO(b"\x01" * (BLOCK_BYTES + 100))

    blocks = list(read_blocks(stream))

    # Downstream code is entitled to assume every chunk is exactly one block.
    assert len(blocks) == 1


def test_empty_stream_yields_nothing():
    assert list(read_blocks(io.BytesIO(b""))) == []


def test_recorder_spawns_with_its_own_argv(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic", target=1002), fake_launcher)

    recorder.start()

    assert len(fake_launcher.calls) == 1
    argv = fake_launcher.calls[0]
    assert argv[argv.index("--target") + 1] == "1002"
    assert recorder.node_name in argv[argv.index("--properties") + 1]


def test_recorder_node_names_are_unique():
    a = Recorder(RecorderSpec(track="mic"), None)
    b = Recorder(RecorderSpec(track="mic"), None)

    assert a.node_name != b.node_name
    assert a.node_name.startswith("meetscribe.mic.")


def test_recorder_yields_blocks_from_the_process(fake_launcher):
    fake_launcher.script = b"\x02" * (BLOCK_BYTES * 2)
    recorder = Recorder(RecorderSpec(track="system"), fake_launcher)
    recorder.start()

    assert len(list(recorder.blocks())) == 2


def test_recorder_reports_a_dead_process(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()

    assert recorder.failure() is None

    fake_launcher.processes[0].die(returncode=1, stderr="no such target")

    # A silently dead pw-record means the track goes quiet for the rest of the
    # meeting while we keep claiming to record.
    assert recorder.failure() == "no such target"


def test_stop_terminates_a_running_process(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()

    recorder.stop()

    assert fake_launcher.processes[0].terminated is True


def test_stop_is_a_no_op_once_the_process_has_exited(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)
    recorder.start()
    fake_launcher.processes[0].die(returncode=1, stderr="gone")

    recorder.stop()

    # Already dead. Signalling again is pointless, and against a real process
    # group it could reach a recycled pid.
    assert fake_launcher.processes[0].terminated is False


def test_stop_before_start_does_not_raise(fake_launcher):
    recorder = Recorder(RecorderSpec(track="mic"), fake_launcher)

    recorder.stop()

    assert fake_launcher.processes == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_recorder.py -v`

Expected: FAIL with `ImportError: cannot import name 'Recorder' from 'meetscribe.recorder'`.

- [ ] **Step 3: Write the implementation**

Append to `meetscribe/recorder.py`:

```python
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .ports import ManagedProcess, ProcessLauncher
from .types import BLOCK_BYTES


def read_blocks(stream: BinaryIO, block_bytes: int = BLOCK_BYTES) -> Iterator[bytes]:
    """Yield fixed-size blocks, reassembling short reads.

    A partial block at end of stream is dropped: consumers are entitled to
    assume every chunk is exactly one block.
    """
    while True:
        buf = stream.read(block_bytes)
        if not buf:
            return
        while len(buf) < block_bytes:
            more = stream.read(block_bytes - len(buf))
            if not more:
                return
            buf += more
        yield buf


@dataclass(frozen=True)
class RecorderSpec:
    track: str
    target: int | None = None
    capture_sink: bool = False
    autoconnect: bool = True
    latency: str = "100ms"


class Recorder:
    def __init__(self, spec: RecorderSpec, launcher: ProcessLauncher):
        self.spec = spec
        self.node_name = f"meetscribe.{spec.track}.{uuid.uuid4().hex[:8]}"
        self._launcher = launcher
        self._process: ManagedProcess | None = None

    def argv(self) -> list[str]:
        return build_argv(
            node_name=self.node_name,
            media_name=f"meetscribe {self.spec.track}",
            target=self.spec.target,
            capture_sink=self.spec.capture_sink,
            autoconnect=self.spec.autoconnect,
            latency=self.spec.latency,
        )

    def start(self) -> None:
        self._process = self._launcher.spawn(self.argv())

    def blocks(self) -> Iterator[bytes]:
        assert self._process is not None, "call start() first"
        yield from read_blocks(self._process.stdout)

    def failure(self) -> str | None:
        """Non-None once pw-record has exited unexpectedly."""
        if self._process is None:
            return None
        code = self._process.poll()
        if code in (None, 0):
            return None
        return self._process.stderr_text().strip() or f"pw-record exited {code}"

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
```

Move the `from __future__ import annotations` line and merge the imports so the module has a single import block at the top.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_recorder.py -v`

Expected: PASS, 17 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/recorder.py tests/test_recorder.py
git commit -m "feat: frame recorder stdout into fixed blocks and detect death"
```

---

## Task 9: The application tap

This is the module that justifies the whole PipeWire approach. It links a matching application's output ports into our capture node *in addition to* its existing link to your speakers, so the call keeps playing normally while we get a copy.

It has to keep re-scanning: Zoom creates its audio stream when the meeting starts, not when the app launches.

**Files:**
- Create: `meetscribe/tap.py`
- Create: `tests/test_tap.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tap.py`:

```python
import threading
from dataclasses import replace

from meetscribe.graph import PwGraph, PwNode, PwPort
from meetscribe.ports import LinkResult
from meetscribe.tap import GRAPH_ERROR_WARN_AFTER, POLL_INTERVAL_S, AppTap
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


def test_relinks_a_restarted_stream_whose_port_ids_were_recycled(zoom_graph):
    # PipeWire hands a dead stream's port ids to a new one within a poll
    # interval - verified against a live session. The old link died with the
    # old node, so the new stream must be linked even though the ids match.
    linker = FakeLinker()
    restarted = replace(
        zoom_graph,
        nodes=tuple(
            replace(n, serial=1900) if n.id == 55 else n for n in zoom_graph.nodes
        ),
    )
    tap = make_tap(
        with_capture_node(zoom_graph), with_capture_node(restarted), linker=linker
    )

    assert tap.poll_once() == 2
    assert tap.poll_once() == 2
    assert linker.links == [(60, 700), (61, 700), (60, 700), (61, 700)]


def test_a_failed_link_is_retried_next_poll(zoom_graph):
    # pw-link can lose a race with a stream still negotiating its format.
    # That must not poison the pair for the rest of the session.
    linker = FakeLinker(result=LinkResult.FAILED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    assert tap.poll_once() == 0
    assert tap.poll_once() == 0

    assert len(linker.links) == 4


def test_a_failed_link_warns_once_not_every_poll(zoom_graph, caplog):
    linker = FakeLinker(result=LinkResult.FAILED)
    tap = make_tap(with_capture_node(zoom_graph), linker=linker)

    tap.poll_once()
    tap.poll_once()
    tap.poll_once()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # one per port, not one per poll


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


def test_repeated_graph_failures_escalate_to_a_warning(caplog):
    class BrokenGraph:
        def snapshot(self):
            raise RuntimeError("pw-dump exploded")

    stop = threading.Event()
    clock = FakeClock()
    tap = AppTap(
        pattern="zoom",
        capture_node_name=CAPTURE_NODE,
        graph=BrokenGraph(),
        linker=FakeLinker(),
        clock=clock,
    )
    original_sleep = clock.sleep

    def sleep_and_maybe_stop(seconds):
        original_sleep(seconds)
        if len(clock.slept) >= GRAPH_ERROR_WARN_AFTER + 3:
            stop.set()

    clock.sleep = sleep_and_maybe_stop  # type: ignore[method-assign]
    tap.run(stop)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    # Quiet for a blip, one warning once it is clearly persistent, and not
    # one per poll after that.
    assert len(warnings) == 1
    assert "failing repeatedly" in warnings[0].getMessage()
    assert "pw-dump exploded" in warnings[0].getMessage()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_tap.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.tap'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/tap.py`:

```python
"""Link a matching application's audio into our capture node.

The link is additive. The application keeps its existing link to the
speakers, so the user still hears the call, and PipeWire delivers an
identical copy to us.

The watcher re-scans because applications create their audio streams late:
Zoom does it when the meeting starts, not when the app launches. Anything
that resolves nodes once at startup records silence. The same re-scan covers
reconnects when someone switches headphones mid-call.

Links are tracked by node serial and port name, never by port id. PipeWire
recycles port ids, and a restarted stream can be handed its dead
predecessor's ids inside one poll interval - keying on ids would make us
skip linking it and capture silence for the rest of the meeting.
"""

from __future__ import annotations

import logging
import threading

from .graph import PLAYBACK_STREAM
from .ports import Clock, GraphSource, Linker, LinkResult

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
GRAPH_ERROR_WARN_AFTER = 3


class AppTap:
    def __init__(
        self,
        pattern: str,
        capture_node_name: str,
        graph: GraphSource,
        linker: Linker,
        clock: Clock,
        interval: float = POLL_INTERVAL_S,
    ):
        self._pattern = pattern
        self._capture_node_name = capture_node_name
        self._graph = graph
        self._linker = linker
        self._clock = clock
        self._interval = interval
        self._linked: set[tuple[int, str, str]] = set()
        self._warned: set[tuple[int, str, str]] = set()
        self.tapped_labels: set[str] = set()

    def poll_once(self) -> int:
        """Link any new matching ports. Returns how many links were created."""
        snapshot = self._graph.snapshot()

        node = snapshot.node_by_name(self._capture_node_name)
        if node is None:
            return 0  # pw-record has not registered with the graph yet
        inputs = snapshot.ports_of(node.id, "in")
        if not inputs:
            return 0

        created = 0
        for source in snapshot.by_class(PLAYBACK_STREAM):
            if not source.matches(self._pattern):
                continue
            outputs = snapshot.ports_of(source.id, "out")
            if not outputs:
                continue

            if source.label not in self.tapped_labels:
                log.info("tapping %s (pid=%s)", source.label, source.pid)
                self.tapped_labels.add(source.label)

            for index, out_port in enumerate(outputs):
                # Fan every channel into our mono input; PipeWire sums them.
                in_port = inputs[min(index, len(inputs) - 1)]
                # Keyed on the node's serial and the port NAMES, never on port
                # ids: PipeWire recycles ids, and a restarted stream can be
                # handed its dead predecessor's ids within one poll interval.
                pair = (source.serial, out_port.name, in_port.name)
                if pair in self._linked:
                    continue

                result = self._linker.link(out_port.id, in_port.id)
                if result is LinkResult.FAILED:
                    log.debug(
                        "link %s:%s -> %s failed",
                        source.label,
                        out_port.name,
                        in_port.name,
                    )
                    if pair not in self._warned:
                        self._warned.add(pair)
                        log.warning(
                            "could not link %s (%s) into the capture node - "
                            "audio from that application may be missing",
                            source.label,
                            out_port.name,
                        )
                    continue  # deliberately not recorded, so it is retried

                self._linked.add(pair)
                if result is LinkResult.LINKED:
                    created += 1
        return created

    def run(self, stop: threading.Event) -> None:
        consecutive_errors = 0
        while not stop.is_set():
            try:
                self.poll_once()
                consecutive_errors = 0
            except Exception as exc:  # a transient graph read must not kill us
                consecutive_errors += 1
                if consecutive_errors == GRAPH_ERROR_WARN_AFTER:
                    # One blip is unremarkable. Failing repeatedly means we are
                    # blind to new streams for the rest of the meeting, which
                    # must not be debug-only. Warn once, not every poll.
                    # The cause is whatever %s carries - it may be the graph
                    # read, but a missing pw-link lands here too.
                    log.warning(
                        "tap watcher failing repeatedly (%s) - no longer "
                        "picking up new streams matching %r",
                        exc,
                        self._pattern,
                    )
                else:
                    log.debug("tap watcher: %s", exc)
            self._clock.sleep(self._interval)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tap.py -v`

Expected: PASS, 12 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/tap.py tests/test_tap.py
git commit -m "feat: add AppTap watcher for additive per-application capture"
```

---

## Task 10: The real adapters

The only module that shells out. Keep it thin — the decision logic it does own (tool lookup, `pw-link` result classification, version parsing) is extracted as pure functions so it can still be tested.

**Files:**
- Create: `meetscribe/adapters.py`
- Create: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_adapters.py`:

```python
import subprocess
import tempfile

import pytest

from meetscribe.adapters import (
    STDERR_TAIL_BYTES,
    MissingToolError,
    PopenProcess,
    PwLinkLinker,
    SystemClock,
    classify_link_output,
    parse_pw_version,
    require_tool,
)
from meetscribe.ports import LinkResult


def test_link_success():
    assert classify_link_output(0, "") is LinkResult.LINKED


def test_file_exists_means_already_linked():
    # pw-link says this when the pair is already connected. Benign.
    assert classify_link_output(1, "failed to link ports: File exists") is (
        LinkResult.ALREADY_LINKED
    )


def test_other_errors_are_failures():
    assert classify_link_output(1, "No such port") is LinkResult.FAILED


def test_parses_the_pipewire_version():
    assert parse_pw_version("pw-cli\nCompiled with libpipewire 1.0.5\n") == (1, 0, 5)


def test_unparseable_version_is_zero():
    assert parse_pw_version("something unexpected") == (0, 0, 0)


def test_require_tool_returns_the_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    assert require_tool("pw-dump") == "/usr/bin/pw-dump"


def test_require_tool_explains_how_to_install(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(MissingToolError) as excinfo:
        require_tool("pw-record")

    message = str(excinfo.value)
    assert "pw-record" in message
    assert "pacman" in message  # install hints for the user's distro


def test_system_clock_satisfies_the_port():
    from meetscribe.ports import Clock

    assert isinstance(SystemClock(), Clock)


def test_stderr_text_returns_the_tail_of_a_long_log():
    # A temp file rather than a pipe, so pw-record can log all meeting without
    # filling a 64 KB buffer and deadlocking. We keep the end of the log,
    # which is where the failure is.
    with tempfile.TemporaryFile() as handle:
        handle.write(b"x" * STDERR_TAIL_BYTES)
        handle.write(b"the actual error\n")

        text = PopenProcess(process=None, stderr_file=handle).stderr_text()

    assert "the actual error" in text
    assert len(text) <= STDERR_TAIL_BYTES


def test_stderr_text_is_empty_without_a_file():
    assert PopenProcess(process=None).stderr_text() == ""


def test_a_link_timeout_is_a_failure(monkeypatch):
    # link() runs inside AppTap's poll loop, so a hang would stall the
    # watcher for the rest of the meeting.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pw-link", timeout=5)

    monkeypatch.setattr("subprocess.run", explode)

    assert PwLinkLinker().link(60, 700) is LinkResult.FAILED
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_adapters.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.adapters'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/adapters.py`:

```python
"""Real implementations of every port except SpeechSession.

This is the only module in the package that starts a subprocess. Keeping that
in one place is what lets everything else be tested against fakes.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import IO, BinaryIO, Sequence

from .graph import PwGraph, parse_graph
from .ports import LinkResult

INSTALL_HINT = (
    "Install PipeWire's CLI utilities:\n"
    "  Debian/Ubuntu: sudo apt install pipewire-bin pipewire-audio\n"
    "  Fedora:        sudo dnf install pipewire-utils\n"
    "  Arch:          sudo pacman -S pipewire pipewire-audio"
)

MIN_PW_VERSION = (0, 3, 60)
STDERR_TAIL_BYTES = 8192
LINK_TIMEOUT_S = 5


class MissingToolError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MissingToolError(f"{name} not found. {INSTALL_HINT}")
    return path


def parse_pw_version(text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return (0, 0, 0)
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def classify_link_output(returncode: int, stderr: str) -> LinkResult:
    if returncode == 0:
        return LinkResult.LINKED
    # "File exists" just means we already linked this pair.
    if "exists" in stderr.lower():
        return LinkResult.ALREADY_LINKED
    return LinkResult.FAILED


class PwDumpGraphSource:
    def snapshot(self) -> PwGraph:
        result = subprocess.run(
            [require_tool("pw-dump")],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return parse_graph(result.stdout)


class PopenProcess:
    def __init__(
        self, process: subprocess.Popen, stderr_file: IO[bytes] | None = None
    ):
        self._process = process
        self._stderr_file = stderr_file

    @property
    def stdout(self) -> BinaryIO:
        assert self._process.stdout is not None
        return self._process.stdout

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def stderr_text(self) -> str:
        """The tail of whatever the process wrote to stderr."""
        if self._stderr_file is None:
            return ""
        end = self._stderr_file.seek(0, os.SEEK_END)
        self._stderr_file.seek(max(0, end - STDERR_TAIL_BYTES))
        return self._stderr_file.read().decode(errors="replace")


class SubprocessLauncher:
    def spawn(self, argv: Sequence[str]) -> PopenProcess:
        resolved = [require_tool(argv[0]), *argv[1:]]
        # stderr goes to a temp file, never a pipe. Nothing reads a pipe until
        # the process has already exited, so a chatty pw-record - an inherited
        # PIPEWIRE_DEBUG is enough - fills the 64 KB buffer and blocks forever
        # on write. That stalls stdout too, since it blocks in the same call
        # stack, so capture goes silent with poll() still returning None and
        # even the dead-track check never fires.
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            resolved,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0,
            start_new_session=True,
        )
        return PopenProcess(process, stderr_file)


class PwLinkLinker:
    def link(self, src_port: int, dst_port: int) -> LinkResult:
        try:
            result = subprocess.run(
                [require_tool("pw-link"), str(src_port), str(dst_port)],
                capture_output=True,
                text=True,
                timeout=LINK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Called inside AppTap's poll loop; a hang here would stall the
            # watcher for the rest of the meeting.
            return LinkResult.FAILED
        return classify_link_output(result.returncode, result.stderr or "")


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def installed_pw_version() -> tuple[int, int, int]:
    try:
        output = subprocess.run(
            [require_tool("pw-cli"), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (MissingToolError, subprocess.SubprocessError, OSError):
        # OSError covers a tool deleted between which() and exec. This exists
        # only to drive a soft warning, so it must never throw harder.
        return (0, 0, 0)
    return parse_pw_version(output)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_adapters.py -v`

Expected: PASS, 11 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/adapters.py tests/test_adapters.py
git commit -m "feat: add real PipeWire adapters behind the ports"
```

---

## Task 11: Capture composition

Deciding *what* to record from a graph snapshot is pure logic and gets tested properly. Owning threads and queues is not, and stays thin.

**Files:**
- Create: `meetscribe/capture.py`
- Create: `tests/test_capture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_capture.py`:

```python
import io
import threading

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


def test_pump_timestamps_chunks_from_the_clock(idle_graph):
    class TickingClock(FakeClock):
        def monotonic(self) -> float:
            self.now += 0.1
            return self.now

    launcher = FakeLauncher(script=b"\x01" * (BLOCK_BYTES * 3))
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=launcher,
        linker=None,
        clock=TickingClock(),
    )
    recorder = capture.recorders[MIC]
    recorder.start()
    capture.pump(MIC, recorder)

    stamps = [capture.queues[MIC].get(timeout=0).t_start for _ in range(3)]

    # The field the sibling test is named for but never checks. Timestamps
    # must advance, and must be relative to the capture's own origin.
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 3
    assert stamps[0] >= 0.0


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


def test_start_then_shutdown_leaves_no_thread_running(idle_graph):
    launcher = FakeLauncher(script=b"\x01" * (BLOCK_BYTES * 2))
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=launcher,
        linker=None,
        clock=FakeClock(),
    )

    capture.start()
    assert len(capture._threads) == 1  # one pump, no tap without --app

    capture.shutdown()

    assert capture.stop.is_set()
    assert all(not thread.is_alive() for thread in capture._threads)


def test_shutdown_is_safe_to_call_twice(idle_graph):
    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(script=b""),
        linker=None,
        clock=FakeClock(),
    )
    capture.start()

    capture.shutdown()
    capture.shutdown()  # a second Ctrl-C must not raise


def test_shutdown_joins_a_pump_parked_on_read(idle_graph):
    # A real pump spends almost all of a meeting blocked in read(), waiting
    # for the next block. Terminating pw-record closes its stdout, which is
    # what releases it. Without a join, shutdown() would return while that
    # thread was still unwinding - and a test whose pump exits on its own
    # cannot tell the difference.
    released = threading.Event()

    class ParkedStream(io.BytesIO):
        def read(self, size=-1):  # type: ignore[override]
            released.wait(timeout=5)
            return b""

    class ParkingLauncher(FakeLauncher):
        def spawn(self, argv):
            process = super().spawn(argv)
            process._stdout = ParkedStream()
            inner = process.terminate

            def terminate():
                inner()
                released.set()

            process.terminate = terminate  # type: ignore[method-assign]
            return process

    capture = PipeWireCapture(
        config=CaptureConfig(system_enabled=False),
        graph=FakeGraphSource(idle_graph),
        launcher=ParkingLauncher(),
        linker=None,
        clock=FakeClock(),
    )

    capture.start()
    assert capture._threads[0].is_alive(), "pump should be parked on read()"

    capture.shutdown()

    assert all(not thread.is_alive() for thread in capture._threads)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_capture.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.capture'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/capture.py`:

```python
"""Decide what to capture, then own the recorders, queues and threads."""

from __future__ import annotations

import logging
import queue
import threading
import time

from dataclasses import dataclass

from .graph import SINK, SOURCE, PwGraph
from .ports import Clock, GraphSource, Linker, ProcessLauncher
from .recorder import Recorder, RecorderSpec
from .tap import AppTap
from .types import MIC, SYSTEM, AudioChunk

log = logging.getLogger(__name__)

QUEUE_BLOCKS = 400  # about 40 seconds of audio at 100 ms per block
DROP_LOG_EVERY = 100
NODE_APPEAR_TIMEOUT_S = 5.0
SHUTDOWN_TIMEOUT_S = 2.0


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureConfig:
    mic: str | None = None
    system: str | None = None
    app: str | None = None
    mic_enabled: bool = True
    system_enabled: bool = True
    latency: str = "100ms"


def plan_recorders(graph: PwGraph, config: CaptureConfig) -> list[RecorderSpec]:
    """Pure: work out what to record from one graph snapshot."""
    specs: list[RecorderSpec] = []

    if config.mic_enabled:
        node = (
            graph.find(config.mic, SOURCE)
            if config.mic
            else graph.node_by_name(graph.default_source or "")
        )
        if node is None:
            raise CaptureError(
                f"No microphone matching {config.mic!r}. Try: meetscribe devices"
            )
        specs.append(
            RecorderSpec(track=MIC, target=node.serial, latency=config.latency)
        )

    if config.system_enabled:
        if config.app:
            # No target: AppTap makes the links itself, and autoconnect must be
            # off so WirePlumber does not attach us to the default microphone.
            specs.append(
                RecorderSpec(
                    track=SYSTEM,
                    target=None,
                    autoconnect=False,
                    latency=config.latency,
                )
            )
        else:
            node = (
                graph.find(config.system, SINK)
                if config.system
                else graph.node_by_name(graph.default_sink or "")
            )
            if node is None:
                raise CaptureError(
                    f"No output device matching {config.system!r}. "
                    "Try: meetscribe devices"
                )
            specs.append(
                RecorderSpec(
                    track=SYSTEM,
                    target=node.serial,
                    capture_sink=True,
                    latency=config.latency,
                )
            )

    if not specs:
        raise CaptureError("Nothing to capture: both tracks are disabled.")
    return specs


class DroppingQueue:
    """Bounded queue that drops and counts rather than blocking the producer.

    Blocking here would stall the stdout pump and eventually pw-record itself.
    Audio is lost either way when the engine cannot keep up; this way the
    pipeline survives and says so.
    """

    def __init__(self, maxsize: int = QUEUE_BLOCKS):
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, item) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped % DROP_LOG_EVERY == 1:
                log.warning(
                    "audio queue full, dropped %d blocks so far "
                    "(is the engine keeping up?)",
                    self.dropped,
                )
            return False

    def get(self, timeout: float | None = None):
        return self._queue.get(timeout=timeout)


class PipeWireCapture:
    def __init__(
        self,
        config: CaptureConfig,
        graph: GraphSource,
        launcher: ProcessLauncher,
        linker: Linker | None,
        clock: Clock,
    ):
        self._config = config
        self._graph = graph
        self._launcher = launcher
        self._linker = linker
        self._clock = clock

        self.stop = threading.Event()
        self.recorders: dict[str, Recorder] = {}
        self.queues: dict[str, DroppingQueue] = {}
        self.dead_tracks: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._t0 = clock.monotonic()

        for spec in plan_recorders(graph.snapshot(), config):
            self.recorders[spec.track] = Recorder(spec, launcher)
            self.queues[spec.track] = DroppingQueue()

    def start(self) -> None:
        for recorder in self.recorders.values():
            recorder.start()

        for track, recorder in self.recorders.items():
            thread = threading.Thread(
                target=self.pump, args=(track, recorder), daemon=True, name=f"pump-{track}"
            )
            thread.start()
            self._threads.append(thread)

        if self._config.app and SYSTEM in self.recorders:
            assert self._linker is not None, "app mode needs a Linker"
            self.await_capture_node(self.recorders[SYSTEM])
            tap = AppTap(
                pattern=self._config.app,
                capture_node_name=self.recorders[SYSTEM].node_name,
                graph=self._graph,
                linker=self._linker,
                clock=self._clock,
            )
            thread = threading.Thread(
                target=tap.run, args=(self.stop,), daemon=True, name="app-tap"
            )
            thread.start()
            self._threads.append(thread)
            log.info("watching for streams matching %r", self._config.app)

    def await_capture_node(self, recorder: Recorder) -> None:
        """Block until pw-record registers with the graph.

        If it never does, pw-record is broken and linking would silently do
        nothing, so fail loudly with a way to check that.
        """
        deadline = self._clock.monotonic() + NODE_APPEAR_TIMEOUT_S
        while self._clock.monotonic() < deadline:
            if self._graph.snapshot().node_by_name(recorder.node_name) is not None:
                return
            self._clock.sleep(0.2)
        raise CaptureError(
            "Capture node never appeared - is pw-record working? "
            "Test with: pw-record --target=0 /tmp/t.wav"
        )

    def pump(self, track: str, recorder: Recorder) -> None:
        """Move framed blocks from one recorder into its queue."""
        for block in recorder.blocks():
            chunk = AudioChunk(
                track=track,
                pcm=block,
                t_start=self._clock.monotonic() - self._t0,
            )
            self.queues[track].put(chunk)

        failure = recorder.failure()
        if failure and not self.stop.is_set():
            log.error("capture for track %r stopped: %s", track, failure)
            self.dead_tracks.add(track)

    def all_tracks_dead(self) -> bool:
        return bool(self.recorders) and self.dead_tracks == set(self.recorders)

    def shutdown(self) -> None:
        self.stop.set()
        for recorder in self.recorders.values():
            recorder.stop()
        # One shared budget rather than a fresh timeout per thread: joining
        # three threads at 2 s each would stall Ctrl-C for six seconds. Real
        # time, not the injected clock - these are real threads.
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for track, q in self.queues.items():
            if q.dropped:
                log.warning("track %r dropped %d blocks in total", track, q.dropped)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_capture.py -v`

Expected: PASS, 19 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/capture.py tests/test_capture.py
git commit -m "feat: compose capture tracks with dropping queues and dead-track detection"
```

---

## Task 12: Google result mapping and error classification

Two pure pieces, separated from anything that opens a connection: turning a recognition result into a `Segment`, and deciding whether an exception is worth retrying.

**Files:**
- Create: `meetscribe/google.py`
- Create: `tests/test_google.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_google.py`:

```python
from datetime import timedelta
from types import SimpleNamespace

from google.api_core import exceptions as gexc

from meetscribe.google import (
    BACKOFF_CAP_S,
    BACKOFF_START_S,
    duration_seconds,
    is_fatal,
    next_backoff,
    segment_from_result,
)
from meetscribe.rotation import StreamClock


def word(text, start, end):
    return SimpleNamespace(
        word=text,
        start_offset=timedelta(seconds=start),
        end_offset=timedelta(seconds=end),
    )


def result(transcript, *, is_final=True, end=1.0, words=(), confidence=0.9):
    alternative = SimpleNamespace(
        transcript=transcript, confidence=confidence, words=list(words)
    )
    return SimpleNamespace(
        alternatives=[alternative],
        is_final=is_final,
        result_end_offset=timedelta(seconds=end),
    )


def test_duration_of_none_is_zero():
    assert duration_seconds(None) == 0.0


def test_duration_reads_total_seconds():
    assert duration_seconds(timedelta(seconds=2, milliseconds=500)) == 2.5


def test_maps_a_result_onto_the_session_timeline():
    clock = StreamClock(offset=300.0)
    seg = segment_from_result(
        result("hello there", end=4.0, words=[word("hello", 1.0, 2.0), word("there", 2.0, 4.0)]),
        clock,
        track="system",
    )

    assert seg.text == "hello there"
    assert seg.track == "system"
    assert seg.is_final is True
    # Stream-relative times get the rotation offset applied.
    assert seg.t_start == 301.0
    assert seg.t_end == 304.0
    assert [w.word for w in seg.words] == ["hello", "there"]
    # Every boundary, not just the first: reading end_offset as start_offset
    # would otherwise leave this test green.
    assert (seg.words[0].start, seg.words[0].end) == (301.0, 302.0)
    assert (seg.words[1].start, seg.words[1].end) == (302.0, 304.0)


def test_interim_results_have_no_words():
    clock = StreamClock()
    seg = segment_from_result(result("partial", is_final=False, end=2.0), clock, "mic")

    assert seg.is_final is False
    assert seg.words == ()
    # With no word timings an interim collapses to a point at its end offset.
    assert seg.t_start == seg.t_end == 2.0


def test_empty_transcripts_are_dropped():
    clock = StreamClock()

    assert segment_from_result(result("   "), clock, "mic") is None
    assert segment_from_result(SimpleNamespace(alternatives=[]), clock, "mic") is None


def test_text_is_stripped():
    seg = segment_from_result(result("  spaced  "), StreamClock(), "mic")

    assert seg.text == "spaced"


def test_credential_errors_are_fatal():
    # Retrying these on a 2-second loop spins forever printing the same thing.
    assert is_fatal(gexc.Unauthenticated("bad credentials")) is True
    assert is_fatal(gexc.PermissionDenied("no access")) is True
    assert is_fatal(gexc.InvalidArgument("bad region")) is True
    assert is_fatal(gexc.NotFound("no such recognizer")) is True


def test_transport_errors_are_retryable():
    assert is_fatal(gexc.ServiceUnavailable("try later")) is False
    assert is_fatal(gexc.DeadlineExceeded("slow")) is False
    assert is_fatal(ConnectionResetError()) is False


def test_backoff_doubles_up_to_a_cap():
    delays = [BACKOFF_START_S]
    for _ in range(6):
        delays.append(next_backoff(delays[-1]))

    assert delays[:4] == [2.0, 4.0, 8.0, 16.0]
    assert delays[-1] == BACKOFF_CAP_S
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_google.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.google'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/google.py`:

```python
"""Google Cloud Speech-to-Text v2 (chirp_3) adapter."""

from __future__ import annotations

import logging

from google.api_core import exceptions as gexc

from .rotation import StreamClock
from .types import Segment, Word

log = logging.getLogger(__name__)

BACKOFF_START_S = 2.0
BACKOFF_CAP_S = 30.0
ESCALATE_AFTER_FAILURES = 5

# Retrying any of these is pointless: the configuration or the credentials are
# wrong and will stay wrong.
FATAL_ERRORS = (
    gexc.Unauthenticated,
    gexc.PermissionDenied,
    gexc.InvalidArgument,
    gexc.NotFound,
)


def duration_seconds(value) -> float:
    """protobuf Duration or timedelta -> float seconds. Tolerates None."""
    if value is None:
        return 0.0
    total_seconds = getattr(value, "total_seconds", None)
    return float(total_seconds()) if callable(total_seconds) else 0.0


def segment_from_result(result, clock: StreamClock, track: str) -> Segment | None:
    """Map one recognition result onto the session timeline.

    Returns None for results with nothing usable in them.
    """
    alternatives = getattr(result, "alternatives", None) or []
    if not alternatives:
        return None

    alternative = alternatives[0]
    text = (alternative.transcript or "").strip()
    if not text:
        return None

    # Direct attribute access, not getattr with a default: on a real protobuf
    # these fields are always present, so a default could only ever mask an
    # upstream rename - turning a loud AttributeError into 0.0 timestamps
    # written straight to the durable JSONL.
    end = clock.absolute(duration_seconds(result.result_end_offset))
    words = tuple(
        Word(
            word=w.word,
            start=clock.absolute(duration_seconds(w.start_offset)),
            end=clock.absolute(duration_seconds(w.end_offset)),
        )
        for w in (alternative.words or ())
    )

    return Segment(
        track=track,
        text=text,
        is_final=bool(result.is_final),
        t_start=words[0].start if words else end,
        t_end=end,
        confidence=alternative.confidence or None,
        words=words,
    )


def is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, FATAL_ERRORS)


def next_backoff(previous: float) -> float:
    return min(previous * 2, BACKOFF_CAP_S)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_google.py -v`

Expected: PASS, 9 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/google.py tests/test_google.py
git commit -m "feat: map Chirp 3 results to segments and classify errors"
```

---

## Task 13: The engine worker

One worker per track. It owns the rotation loop, the retry policy, and the silence gate. The speech session is injected as a factory that takes a `StreamClock`, so the adapter applies offsets where the result mapping already lives and the worker never touches a protobuf.

**Files:**
- Modify: `meetscribe/google.py`
- Modify: `tests/test_google.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_google.py`:

```python
import queue
import threading

from google.api_core import exceptions as gexc

from meetscribe.google import EngineWorker
from meetscribe.types import BLOCK_BYTES, AudioChunk, Segment
from meetscribe.vad import SilenceGate
from tests.conftest import FakeClock

PCM = b"\x01" * BLOCK_BYTES


def seg(text, track="mic"):
    return Segment(track=track, text=text, is_final=True, t_start=0.0, t_end=1.0)


class ScriptedSession:
    """Fails on connect, or consumes blocks and yields canned segments."""

    def __init__(
        self,
        clock_state,
        *,
        segments=(),
        error=None,
        clock=None,
        tick=0.0,
        stop=None,
        stop_after=None,
    ):
        self.clock_state = clock_state
        self.segments = list(segments)
        self.error = error
        self.clock = clock
        self.tick = tick
        self.stop = stop
        self.stop_after = stop_after
        self.consumed: list[bytes] = []

    def stream(self, pcm):
        # A real transport error surfaces when the stream opens, before any
        # audio has flowed. Raising here keeps the tests from having to drain
        # the whole queue first.
        if self.error is not None:
            raise self.error
        for block in pcm:
            self.consumed.append(block)
            if self.clock is not None and self.tick:
                self.clock.advance(self.tick)
            if self.stop_after and len(self.consumed) >= self.stop_after:
                self.stop.set()
        yield from self.segments


def filled_queue(count: int, start: float = 0.0) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    for i in range(count):
        q.put(AudioChunk(track="mic", pcm=PCM, t_start=start + i))
    return q


def test_emits_segments_from_the_session():
    stop = threading.Event()
    sessions = []

    def factory(clock_state):
        session = ScriptedSession(clock_state, segments=[seg("hello"), seg("world")])
        sessions.append(session)
        stop.set()  # one pass is enough; blocks() then yields nothing
        return session

    out: queue.Queue = queue.Queue()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(filled_queue(2), out, stop)

    assert [out.get_nowait().text for _ in range(2)] == ["hello", "world"]


def test_silence_gate_filters_blocks_before_they_are_sent():
    stop = threading.Event()
    sessions = []
    quiet = b"\x00" * BLOCK_BYTES

    def factory(clock_state):
        session = ScriptedSession(clock_state, stop=stop, stop_after=6)
        sessions.append(session)
        return session

    audio: queue.Queue = queue.Queue()
    audio.put(AudioChunk(track="mic", pcm=PCM, t_start=0.0))
    for i in range(10):
        audio.put(AudioChunk(track="mic", pcm=quiet, t_start=float(i + 1)))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=lambda pcm: pcm != quiet),
        clock=FakeClock(),
    )
    worker.run(audio, queue.Queue(), stop)

    # One speech block plus the five-block finalisation tail, nothing more.
    assert len(sessions[0].consumed) == 6


def test_rotates_and_carries_the_offset_forward():
    stop = threading.Event()
    clock = FakeClock()
    seen_offsets: list[float] = []

    def factory(clock_state):
        seen_offsets.append(clock_state.offset)
        if len(seen_offsets) == 2:
            stop.set()
        # Each consumed block advances the clock by 0.5 s, so two blocks hit
        # the 1 s rotation limit.
        return ScriptedSession(clock_state, clock=clock, tick=0.5)

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
        max_stream_s=1.0,
    )
    worker.run(filled_queue(6, start=100.0), queue.Queue(), stop)

    assert seen_offsets[0] == 0.0
    # The exact value, not merely positive: 101.0 is the t_start of the last
    # chunk the first stream consumed. Rotating to anything else would pass a
    # "> 0.0" check while silently corrupting the timeline.
    assert seen_offsets[1] == 101.0


def test_fatal_errors_stop_the_run_immediately():
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        return ScriptedSession(clock_state, error=gexc.Unauthenticated("bad creds"))

    clock = FakeClock()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    assert len(attempts) == 1  # no retry loop
    assert stop.is_set()
    assert clock.slept == []  # and no backoff sleep


def test_transport_errors_retry_with_growing_backoff():
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        if len(attempts) >= 4:  # three failures, then stop before the fourth sleep
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    clock = FakeClock()
    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=clock,
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    assert clock.slept == [2.0, 4.0, 8.0]


def test_repeated_failures_escalate_to_error_level(caplog):
    stop = threading.Event()
    attempts = []

    def factory(clock_state):
        attempts.append(1)
        if len(attempts) >= 7:  # escalation starts at the fifth failure
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(filled_queue(1), queue.Queue(), stop)

    levels = [
        record.levelname
        for record in caplog.records
        if "speech stream error" in record.getMessage()
    ]
    # Four warnings, then ERROR for every failure after - a dead network must
    # not scroll past at warning level forever.
    assert levels == ["WARNING"] * 4 + ["ERROR"] * 2


def test_reports_audio_lost_while_offline(caplog):
    stop = threading.Event()
    attempts = []
    audio: queue.Queue = queue.Queue()
    audio.dropped = 0  # type: ignore[attr-defined]

    def factory(clock_state):
        attempts.append(1)
        audio.dropped += 30  # the pump kept dropping while we were offline
        if len(attempts) >= 2:
            stop.set()
        return ScriptedSession(clock_state, error=gexc.ServiceUnavailable("nope"))

    worker = EngineWorker(
        track="mic",
        session_factory=factory,
        gate=SilenceGate(detector=None),
        clock=FakeClock(),
    )
    worker.run(audio, queue.Queue(), stop)

    # An unmarked gap in a transcript reads as silence. Say how much went.
    assert any("3s of audio dropped" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_google.py -v`

Expected: FAIL with `ImportError: cannot import name 'EngineWorker' from 'meetscribe.google'`.

- [ ] **Step 3: Write the implementation**

Append to `meetscribe/google.py`:

```python
import queue as queue_module
import threading
from typing import Callable, Iterator

from .ports import Clock, SpeechSession
from .rotation import MAX_STREAM_SECONDS
from .types import BLOCK_MS  # merges into the existing .types import
from .vad import SilenceGate

SessionFactory = Callable[[StreamClock], SpeechSession]


class EngineWorker:
    """Drives one track's audio through rotating recognition streams."""

    @staticmethod
    def _dropped(audio_q) -> int:
        """Blocks the queue has discarded, when it counts them.

        A plain queue.Queue does not, so this reports 0 rather than failing.
        """
        return getattr(audio_q, "dropped", 0)

    def __init__(
        self,
        track: str,
        session_factory: SessionFactory,
        gate: SilenceGate,
        clock: Clock,
        max_stream_s: float = MAX_STREAM_SECONDS,
    ):
        self._track = track
        self._factory = session_factory
        self._gate = gate
        self._clock = clock
        self._max_stream_s = max_stream_s

    def run(
        self,
        audio_q,
        out_q: queue_module.Queue,
        stop: threading.Event,
    ) -> None:
        stream_clock = StreamClock(max_stream_s=self._max_stream_s)
        backoff = BACKOFF_START_S
        consecutive_failures = 0
        dropped_at_success = self._dropped(audio_q)

        while not stop.is_set():
            last_chunk_t = stream_clock.offset
            started = self._clock.monotonic()
            # One timeline per stream: the engine numbers its results from the
            # start of the audio we send it, and the gate means that is not
            # elapsed time.
            timeline = AudioTimeline(stream_clock.offset)

            def blocks() -> Iterator[bytes]:
                nonlocal last_chunk_t
                while not stop.is_set():
                    if stream_clock.should_rotate(self._clock.monotonic() - started):
                        log.debug("rotating %s stream", self._track)
                        return
                    try:
                        chunk = audio_q.get(timeout=0.25)
                    except queue_module.Empty:
                        continue
                    last_chunk_t = chunk.t_start
                    if self._gate.allows(chunk.pcm):
                        timeline.sent(chunk.t_start)
                        yield chunk.pcm

            try:
                # No stop check inside this loop. blocks() already returns
                # when stop is set, which ends the stream on its own, and
                # breaking out here would discard finals the engine emitted
                # on the way out - exactly the ones cli.py drains out_q for
                # after Ctrl-C.
                for segment in self._factory(timeline).stream(blocks()):
                    out_q.put(segment)
                consecutive_failures = 0
                backoff = BACKOFF_START_S
                dropped_at_success = self._dropped(audio_q)
            except Exception as exc:
                if stop.is_set():
                    break
                if is_fatal(exc):
                    log.error(
                        "speech configuration error (%s), not retryable: %s",
                        self._track,
                        exc,
                    )
                    stop.set()
                    return
                consecutive_failures += 1
                level = (
                    logging.ERROR
                    if consecutive_failures >= ESCALATE_AFTER_FAILURES
                    else logging.WARNING
                )
                # Say how much audio went while we were offline. Without
                # this an outage leaves an unmarked hole in the transcript
                # that reads exactly like nobody speaking.
                lost_blocks = self._dropped(audio_q) - dropped_at_success
                lost = (
                    f"; {lost_blocks * BLOCK_MS / 1000:.0f}s of audio dropped "
                    "while offline"
                    if lost_blocks
                    else ""
                )
                log.log(
                    level,
                    "speech stream error (%s), retrying in %.0fs: %s%s",
                    self._track,
                    backoff,
                    exc,
                    lost,
                )
                self._clock.sleep(backoff)
                backoff = next_backoff(backoff)

            stream_clock = stream_clock.rotated(last_chunk_t)
```

Merge the new imports into the module's existing import block.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_google.py -v`

Expected: PASS, 16 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/google.py tests/test_google.py
git commit -m "feat: add engine worker with stream rotation and retry policy"
```

---

## Task 14: The real Chirp 3 session

The one piece that opens a connection. It is thin by design: build the config, feed the generator, map each result. Its correctness is confirmed by the manual smoke run in Task 16, not by unit tests.

**Files:**
- Modify: `meetscribe/google.py`
- Modify: `tests/test_google.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_google.py`:

```python
from meetscribe.google import (
    STREAM_TIMEOUT_S,
    GoogleConfig,
    GoogleSpeechSession,
    recognizer_path,
    speech_endpoint,
)


def test_regional_endpoint():
    assert speech_endpoint("eu") == "eu-speech.googleapis.com"
    assert speech_endpoint("us") == "us-speech.googleapis.com"


def test_global_endpoint_has_no_prefix():
    assert speech_endpoint("global") == "speech.googleapis.com"


def test_recognizer_path_uses_the_implicit_recognizer():
    path = recognizer_path("my-project", "eu")

    assert path == "projects/my-project/locations/eu/recognizers/_"


def test_config_defaults_match_the_spec():
    config = GoogleConfig(project_id="p")

    assert config.region == "eu"
    assert config.model == "chirp_3"
    assert config.language_codes == ("en-US",)
    assert config.interim is True


class RecordingClient:
    """Drains the request generator so we can inspect what was sent."""

    def __init__(self):
        self.received = None

    def streaming_recognize(self, requests, timeout=None):
        self.received = list(requests)
        self.timeout = timeout
        return []


def test_stream_sends_config_first_then_one_request_per_block():
    client = RecordingClient()
    session = GoogleSpeechSession(
        GoogleConfig(
            project_id="p", phrases=("Kubernetes",), language_codes=("uk-UA", "en-US")
        ),
        client,
        StreamClock(),
        "mic",
    )

    list(session.stream(iter([b"chunk1", b"chunk2"])))

    config = client.received[0].streaming_config.config
    assert client.received[0].recognizer == "projects/p/locations/eu/recognizers/_"
    assert list(config.language_codes) == ["uk-UA", "en-US"]
    assert config.features.enable_word_time_offsets is False
    assert (
        config.adaptation.phrase_sets[0].inline_phrase_set.phrases[0].value
        == "Kubernetes"
    )
    # Audio follows the config, one request per block, in order.
    assert [r.audio for r in client.received[1:]] == [b"chunk1", b"chunk2"]


def test_stream_bounds_the_call_with_a_timeout():
    client = RecordingClient()
    session = GoogleSpeechSession(
        GoogleConfig(project_id="p"), client, StreamClock(), "mic"
    )

    list(session.stream(iter([b"x"])))

    # Without this a black-holed connection stalls the track silently.
    assert client.timeout == STREAM_TIMEOUT_S
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_google.py -k endpoint -v`

Expected: FAIL with `ImportError: cannot import name 'GoogleConfig' from 'meetscribe.google'`.

- [ ] **Step 3: Write the implementation**

Append to `meetscribe/google.py`:

```python
import os
from dataclasses import dataclass

from .types import TARGET_RATE

# Healthy streams are rotated at MAX_STREAM_SECONDS. A stream still open well
# past that is stalled - gRPC sets no deadline and no keepalive, so a
# black-holed connection would otherwise leave this track silently
# transcribing nothing for the rest of the meeting. DeadlineExceeded is not
# fatal, so EngineWorker reconnects.
STREAM_TIMEOUT_S = MAX_STREAM_SECONDS + 30


def speech_endpoint(region: str) -> str:
    if region == "global":
        return "speech.googleapis.com"
    return f"{region}-speech.googleapis.com"


def recognizer_path(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}/recognizers/_"


@dataclass(frozen=True)
class GoogleConfig:
    project_id: str
    region: str = "eu"
    model: str = "chirp_3"
    language_codes: tuple[str, ...] = ("en-US",)
    phrases: tuple[str, ...] = ()
    interim: bool = True


class GoogleSpeechSession:
    """One StreamingRecognize call. Discarded and rebuilt on every rotation."""

    def __init__(self, config: GoogleConfig, client, stream_clock: StreamClock, track: str):
        self._config = config
        self._client = client
        self._clock = stream_clock
        self._track = track

    def _config_request(self):
        from google.cloud.speech_v2.types import cloud_speech as cs

        adaptation = None
        if self._config.phrases:
            adaptation = cs.SpeechAdaptation(
                phrase_sets=[
                    cs.SpeechAdaptation.AdaptationPhraseSet(
                        inline_phrase_set=cs.PhraseSet(
                            phrases=[
                                # 0-20, where high values start degrading
                                # general accuracy. 15 is aggressive enough
                                # for names and jargon without that trade-off.
                                cs.PhraseSet.Phrase(value=p, boost=15.0)
                                for p in self._config.phrases
                            ]
                        )
                    )
                ]
            )

        recognition_config = cs.RecognitionConfig(
            explicit_decoding_config=cs.ExplicitDecodingConfig(
                encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=TARGET_RATE,
                audio_channel_count=1,
            ),
            language_codes=list(self._config.language_codes),
            model=self._config.model,
            # No enable_word_time_offsets: Chirp 3 rejects it outright in
            # streaming mode, which is a fatal InvalidArgument that ends the
            # run before a single word is transcribed.
            features=cs.RecognitionFeatures(
                enable_automatic_punctuation=True,
            ),
            **({"adaptation": adaptation} if adaptation else {}),
        )

        streaming_config = cs.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=cs.StreamingRecognitionFeatures(
                interim_results=self._config.interim,
            ),
        )
        return cs.StreamingRecognizeRequest(
            recognizer=recognizer_path(self._config.project_id, self._config.region),
            streaming_config=streaming_config,
        )

    def stream(self, pcm: Iterator[bytes]) -> Iterator[Segment]:
        from google.cloud.speech_v2.types import cloud_speech as cs

        def requests():
            yield self._config_request()
            for block in pcm:
                yield cs.StreamingRecognizeRequest(audio=block)

        for response in self._client.streaming_recognize(
            requests=requests(), timeout=STREAM_TIMEOUT_S
        ):
            for result in response.results:
                segment = segment_from_result(result, self._clock, self._track)
                if segment is not None:
                    yield segment


def build_session_factory(config: GoogleConfig, track: str) -> SessionFactory:
    """Create the factory the EngineWorker calls on every rotation."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient

    client = SpeechClient(
        client_options=ClientOptions(api_endpoint=speech_endpoint(config.region))
    )

    def factory(stream_clock: StreamClock) -> SpeechSession:
        return GoogleSpeechSession(config, client, stream_clock, track)

    return factory


def project_from_environment() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "No GCP project. Pass --project or set GOOGLE_CLOUD_PROJECT, and "
            "point GOOGLE_APPLICATION_CREDENTIALS at a service account key."
        )
    return project
```

Merge the new imports into the module's existing import block.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_google.py -v`

Expected: PASS, 22 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/google.py tests/test_google.py
git commit -m "feat: add the Chirp 3 streaming session and factory"
```

---

## Task 15: Transcript output

Three sinks: a live console line, an append-only JSONL, and a Markdown render at the end. The JSONL is flushed per final segment so an unclean exit still leaves you with everything up to that moment — the Markdown is a convenience, the JSONL is the record.

**Files:**
- Create: `meetscribe/transcript.py`
- Create: `tests/test_transcript.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcript.py`:

```python
import json
import os
import shutil

from meetscribe.transcript import (
    TranscriptWriter,
    _interim_width,
    hhmmss,
    render_markdown,
)
from meetscribe.types import Segment


def final(track, text, t):
    return Segment(track=track, text=text, is_final=True, t_start=t, t_end=t + 1)


def test_hhmmss():
    assert hhmmss(0) == "00:00:00"
    assert hhmmss(61.9) == "00:01:01"
    assert hhmmss(3661) == "01:01:01"


def test_markdown_groups_consecutive_lines_under_one_heading():
    records = [
        {"t": 0.0, "track": "mic", "text": "hello"},
        {"t": 1.0, "track": "mic", "text": "still me"},
        {"t": 2.0, "track": "system", "text": "hi back"},
    ]

    markdown = render_markdown("session-1", records)

    assert markdown.count("**You**") == 1
    assert markdown.count("**Them**") == 1
    assert "hello\nstill me" in markdown


def test_markdown_sorts_by_time():
    records = [
        {"t": 5.0, "track": "mic", "text": "second"},
        {"t": 1.0, "track": "system", "text": "first"},
    ]

    markdown = render_markdown("s", records)

    assert markdown.index("first") < markdown.index("second")


def test_markdown_starts_a_new_heading_when_the_speaker_changes_back():
    records = [
        {"t": 0.0, "track": "mic", "text": "a"},
        {"t": 1.0, "track": "system", "text": "b"},
        {"t": 2.0, "track": "mic", "text": "c"},
    ]

    assert render_markdown("s", records).count("**You**") == 2


def test_jsonl_is_written_per_final_segment(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "one", 0.0))
    writer.write(final("system", "two", 1.0))

    # Deliberately no close(): an unclean exit must still leave the record.
    lines = (tmp_path / "sess.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["text"] for line in lines] == ["one", "two"]


def test_interim_segments_never_reach_the_jsonl(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(Segment(track="mic", text="partial", is_final=False, t_start=0.0, t_end=1.0))

    assert (tmp_path / "sess.jsonl").read_text() == ""


def test_close_writes_markdown_and_returns_its_path(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")
    writer.write(final("mic", "hello", 0.0))

    path = writer.close()

    assert path == tmp_path / "sess.md"
    assert "# Transcript sess" in path.read_text()
    assert "hello" in path.read_text()


def test_jsonl_records_carry_timing_and_confidence(tmp_path):
    writer = TranscriptWriter(tmp_path, session="sess")
    writer.write(
        Segment(
            track="system", text="x", is_final=True, t_start=1.0, t_end=2.0, confidence=0.87
        )
    )

    record = json.loads((tmp_path / "sess.jsonl").read_text().strip())
    assert record["t"] == 1.0
    assert record["t_end"] == 2.0
    assert record["track"] == "system"
    assert record["confidence"] == 0.87
    assert "wall_clock" in record


def test_creates_the_output_directory(tmp_path):
    target = tmp_path / "nested" / "transcripts"

    TranscriptWriter(target, session="sess")

    assert target.is_dir()


def test_final_lines_print_a_stamp_and_a_speaker_label(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "hello", 61.0))
    writer.write(final("system", "hi back", 62.0))

    out = capsys.readouterr().out
    assert "00:01:01 You: hello" in out
    assert "00:01:02 Them: hi back" in out


def test_console_output_is_plain_when_stdout_is_not_a_tty(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(final("mic", "hello", 0.0))

    # No cursor to rewrite and no point in colour under a pipe.
    assert "\x1b[" not in capsys.readouterr().out


def test_interim_lines_are_silent_when_stdout_is_not_a_tty(tmp_path, capsys):
    writer = TranscriptWriter(tmp_path, session="sess")

    writer.write(
        Segment(track="mic", text="partial", is_final=False, t_start=0.0, t_end=1.0)
    )

    assert capsys.readouterr().out == ""


def test_interim_width_follows_the_terminal(monkeypatch):
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((40, 24))
    )

    # A fixed width wider than the terminal wraps, and the \r erase then
    # clears only one of the two rows.
    assert _interim_width() == 39
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_transcript.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.transcript'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/transcript.py`:

```python
"""Transcript sinks: live console, durable JSONL, readable Markdown."""

from __future__ import annotations

import json
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .types import MIC, SYSTEM, Segment

LABELS = {MIC: "You", SYSTEM: "Them"}
COLORS = {MIC: "\033[36m", SYSTEM: "\033[33m"}
DIM = "\033[2m"
RESET = "\033[0m"


def _interim_width() -> int:
    """Columns the interim line may occupy.

    Derived from the real terminal, not a fixed maximum: a line wider than
    the terminal wraps, and the \\r erase only clears the row the cursor is
    on - leaving the wrapped remainder on screen for the rest of the meeting.
    One column is left spare so writing the last cell cannot wrap.
    """
    return max(1, shutil.get_terminal_size((80, 24)).columns - 1)


def hhmmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def render_markdown(session: str, records: list[dict]) -> str:
    lines = [f"# Transcript {session}", ""]
    last_label: str | None = None
    for record in sorted(records, key=lambda r: r["t"]):
        label = LABELS.get(record["track"], record["track"])
        if label != last_label:
            lines.append("")
            lines.append(f"**{label}** _{hhmmss(record['t'])}_")
            last_label = label
        lines.append(record["text"])
    return "\n".join(lines) + "\n"


class TranscriptWriter:
    def __init__(
        self,
        outdir: Path,
        session: str | None = None,
        show_interim: bool = True,
        color: bool = True,
    ):
        outdir.mkdir(parents=True, exist_ok=True)
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        self.show_interim = show_interim and sys.stdout.isatty()
        self.color = color and sys.stdout.isatty()
        self._lock = threading.Lock()
        self._interim_width = 0
        self._records: list[dict] = []
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")

    def _clear_interim(self) -> None:
        if self._interim_width:
            sys.stdout.write("\r" + " " * self._interim_width + "\r")
            self._interim_width = 0

    def write(self, segment: Segment) -> None:
        with self._lock:
            label = LABELS.get(segment.track, segment.track)

            if not segment.is_final:
                if self.show_interim:
                    self._clear_interim()
                    line = f"{hhmmss(segment.t_start)} {label}: {segment.text}"
                    line = line[: _interim_width()]
                    sys.stdout.write(f"{DIM}{line}{RESET}" if self.color else line)
                    sys.stdout.flush()
                    self._interim_width = len(line)
                return

            self._clear_interim()
            stamp = hhmmss(segment.t_start)
            if self.color:
                colour = COLORS.get(segment.track, "")
                print(f"{DIM}{stamp}{RESET} {colour}{label}:{RESET} {segment.text}")
            else:
                print(f"{stamp} {label}: {segment.text}")

            record = {
                "t": segment.t_start,
                "t_end": segment.t_end,
                "track": segment.track,
                "text": segment.text,
                "confidence": segment.confidence,
                "wall_clock": datetime.now(timezone.utc).isoformat(),
            }
            self._records.append(record)
            self._jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._jsonl.flush()

    def close(self) -> Path:
        with self._lock:
            self._clear_interim()
            self._jsonl.close()
            self.md_path.write_text(
                render_markdown(self.session, self._records), encoding="utf-8"
            )
        return self.md_path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transcript.py -v`

Expected: PASS, 13 passed.

- [ ] **Step 5: Commit**

```bash
git add meetscribe/transcript.py tests/test_transcript.py
git commit -m "feat: add transcript console, JSONL and Markdown sinks"
```

---

## Task 16: CLI and wiring

`main()` accepts injected ports so the whole program can be exercised without PipeWire. `describe_graph` returns a string rather than printing, for the same reason.

**Files:**
- Create: `meetscribe/cli.py`
- Create: `meetscribe/__main__.py`
- Create: `tests/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
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
```

Replace the contents of `tests/test_smoke.py`:

```python
def test_package_imports():
    import meetscribe

    assert meetscribe is not None


def test_console_entry_point_is_importable():
    from meetscribe.cli import main

    assert callable(main)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'meetscribe.cli'`.

- [ ] **Step 3: Write the implementation**

Create `meetscribe/cli.py`:

```python
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
    session_factory=None,
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
        return _run(args, graph, launcher, linker, clock, session_factory)
    except KeyboardInterrupt:
        # Ctrl-C before the handler is installed, e.g. during auth.
        return 130
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
    except Exception as exc:
        # Everything else the outside world throws: Google auth and gRPC, a
        # dead pw-dump, a full disk. None of those subclass RuntimeError, so
        # an allowlist misses exactly the failures a first run hits. -v
        # re-raises so a real traceback is still one flag away.
        if getattr(args, "verbose", False):
            raise
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _run(args, graph, launcher, linker, clock, session_factory=None) -> int:
    make_session = session_factory or build_session_factory
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

    workers = [
        threading.Thread(
            target=EngineWorker(
                track=track,
                session_factory=make_session(google_config, track),
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

    # After the workers, so a failure building the Google client does not
    # leave a stray transcripts/ directory and an empty .jsonl behind.
    writer = TranscriptWriter(args.out, show_interim=not args.no_interim)

    def handle_sigint(*_):
        stop.set()
        capture.stop.set()

    signal.signal(signal.SIGINT, handle_sigint)

    capture.start()
    for worker in workers:
        worker.start()

    where = f"app={args.app}" if args.app else "system audio"
    # Chrome goes to stderr so `meetscribe run > transcript.txt` gets only
    # the transcript, and the user still sees this on the terminal.
    print(
        f"\nRecording {where} via Chirp 3. This session is being recorded. "
        "Ctrl-C to stop.\n",
        file=sys.stderr,
    )

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
        print(f"\nSaved:\n  {writer.jsonl_path}\n  {path}", file=sys.stderr)

    return 0
```

Create `meetscribe/__main__.py`:

```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v`

Expected: PASS. Every test written so far passes together.

- [ ] **Step 5: Verify both entry points work**

Run: `uv run meetscribe --help` and `uv run python -m meetscribe --help`

Expected: both print the same usage text listing the `devices` and `run` subcommands.

- [ ] **Step 6: Commit**

```bash
git add meetscribe/cli.py meetscribe/__main__.py tests/test_cli.py tests/test_smoke.py
git commit -m "feat: add CLI, wiring and lifecycle management"
```

---

## Task 17: Documentation and manual verification

Fixture tests cannot prove that `pw-link` routes real audio or that Chirp 3 returns sensible Ukrainian. That needs a real call, so the checklist is part of the deliverable rather than an afterthought.

This task also updates `CLAUDE.md`, which currently states the application does not exist — true when it was written, false once Task 16 lands.

**Files:**
- Create: `README.md`
- Create: `docs/manual-smoke.md`
- Create: `tests/fixtures/pw_dump_real.json`
- Modify: `CLAUDE.md:12-37`

- [ ] **Step 1: Write the manual smoke checklist**

Create `docs/manual-smoke.md`:

```markdown
# Manual smoke check

Run this after any change to capture, linking or the engine. It covers what
the test suite deliberately cannot: that audio actually flows.

Start a real call before you begin — an application only appears in the
PipeWire graph once it is playing audio.

1. `uv run meetscribe devices`
   - your sink and microphone are listed, each marked `[default]` where expected
   - the call application appears under APPLICATIONS with a `--app` hint
   - monitors are not listed as microphones

2. `uv run meetscribe run --app <name> --lang uk-UA --lang en-US`
   - the log says `watching for streams matching '<name>'`
   - text appears for **Them** within a few seconds of someone speaking
   - text appears for **You** when you speak
   - the call audio still plays through your speakers, unchanged

3. Talk past the four-minute mark
   - transcription keeps going (the stream rotation worked)
   - timestamps continue increasing across the rotation, with no jump back to zero

4. Ctrl-C
   - `Saved:` prints both paths
   - the `.md` groups consecutive lines under one **You** / **Them** heading
   - the `.jsonl` has one line per final segment

5. Failure paths worth checking once
   - `--app nonexistent` keeps polling rather than exiting
   - `--mic nonexistent` exits immediately suggesting `meetscribe devices`
   - an invalid `--project` exits immediately instead of retrying forever
```

- [ ] **Step 2: Capture a real pw-dump fixture**

Run, with a call in progress:

```bash
pw-dump > /tmp/pw-dump-raw.json
```

Scrub it before committing. Raw output carries your username, hostname, device serial numbers and process ids:

```bash
grep -iE "$(whoami)|$(hostname)" /tmp/pw-dump-raw.json | head
```

Replace any real username with `user`, any hostname with `host`, and any
hardware serial with a placeholder, then save as
`tests/fixtures/pw_dump_real.json`.

- [ ] **Step 3: Add a schema regression test**

Append to `tests/test_graph.py`:

```python
def test_parses_a_real_pw_dump():
    """Guards against pw-dump's schema differing from our hand-written fixtures."""
    graph = parse_graph((FIXTURES / "pw_dump_real.json").read_text())

    assert graph.by_class(SINK), "expected at least one sink"
    assert graph.by_class(SOURCE), "expected at least one source"
    assert graph.default_sink is not None
    assert all(n.serial for n in graph.nodes)
    assert all(p.direction in ("in", "out") for p in graph.ports)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_graph.py::test_parses_a_real_pw_dump -v`

Expected: PASS. A failure here means the hand-written fixtures encode an assumption the real format does not share — fix `graph.py`, not the test.

- [ ] **Step 5: Write the README**

Create `README.md`:

```markdown
# meetscribe

Transcribes any call on your machine — Zoom, Slack huddles, Meet, Teams,
Discord, a browser tab — by capturing audio from PipeWire instead of
integrating with each platform's API.

Your microphone and the call are captured as two separate tracks, which gives
"You" versus "Them" separation without a diarization model.

## Requirements

Linux with PipeWire >= 0.3.60, and a GCP project with Speech-to-Text enabled.

```bash
# Arch
sudo pacman -S pipewire pipewire-audio wireplumber
# Debian/Ubuntu
sudo apt install pipewire-bin pipewire-audio wireplumber

uv sync
```

## Use

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/keys/stt.json
export GOOGLE_CLOUD_PROJECT=my-project

# Start your call FIRST, then see what is playing:
uv run meetscribe devices

uv run meetscribe run --app zoom --lang uk-UA --lang en-US
uv run meetscribe run --no-mic                    # them only
uv run meetscribe run --phrase Kubernetes         # boost jargon
```

Ctrl-C writes `transcripts/<session>.md`. The `.jsonl` beside it is
append-only, so an unclean exit still keeps everything up to that moment.

## Notes

- **Headphones.** Otherwise your microphone re-transcribes the call and the
  other party appears on both tracks. If you cannot, load PipeWire's canceller
  with `pactl load-module module-echo-cancel` and point `--mic` at the
  resulting `echo-cancel-source`.
- **Regions.** `us`, `eu`, `asia-northeast1` and `asia-southeast1` are GA;
  `europe-west2` and `europe-west3` are preview. Default is `eu`.
- **Consent.** Recording participants without telling them is a legal
  question in Ukraine and the EU, not a UX one. Announce it.

Design notes are in `docs/superpowers/specs/`. The research spike that
preceded this implementation is kept in `docs/initial_research/`.
```

- [ ] **Step 6: Update CLAUDE.md**

Replace the "Current repository state — read this first" section (lines 12-37) with:

```markdown
## Repository layout

`meetscribe/` is the application. `docs/initial_research/` is **reference
material, not code** — a research spike kept for the PipeWire details it
records. Do not refactor it, extend it, or import from it; it has its own
manifest and does not run.

- `pyproject.toml` and `uv.lock` at the root belong to the real package
- `docs/superpowers/specs/` holds design documents, `docs/superpowers/plans/`
  the implementation plans
- `docs/manual-smoke.md` covers what the test suite cannot: that audio
  actually flows
```

Leave the rest of `CLAUDE.md` unchanged — the invariants, error handling and
runtime traps it documents all still apply.

- [ ] **Step 7: Run the full suite one last time**

Run: `uv run pytest -v`

Expected: PASS, every test green.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/manual-smoke.md tests/fixtures/pw_dump_real.json tests/test_graph.py CLAUDE.md
git commit -m "docs: add README, manual smoke checklist and real graph fixture"
```

---

## Done

At this point `uv run meetscribe run --app zoom` transcribes a live call, the
suite runs green without audio hardware or network access, and every PipeWire
trap the research spike paid for is pinned by a test.
