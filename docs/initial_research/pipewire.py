"""
PipeWire-native audio capture for Linux.

Three things this module does that the PulseAudio path cannot:

1. Introspects the real graph (`pw-dump`) instead of the Pulse compat view,
   so you see individual application streams, not just sinks.
2. Taps a *single application* by linking its output ports to our capture
   node. PipeWire lets one output port feed many inputs, so Zoom keeps
   playing to your speakers while we get an identical copy. No virtual
   cable, no moving streams, no Spotify in your transcript.
3. Re-links automatically when a stream appears. Zoom does not create its
   audio stream until the meeting starts, so a recorder that resolves nodes
   once at startup records silence.

Audio is resampled by PipeWire itself (`--rate/--channels/--format` on
pw-record), so nothing here touches sample-rate conversion.

Requires: pipewire >= 0.3.60, pipewire-utils (pw-dump, pw-record, pw-link).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TARGET_RATE = 16_000
BLOCK_MS = 100
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000  # 3200 bytes of s16 mono

# media.class values we care about, per pipewire-props(7)
SINK = "Audio/Sink"
SOURCE = "Audio/Source"
PLAYBACK_STREAM = "Stream/Output/Audio"  # an app producing sound
CAPTURE_STREAM = "Stream/Input/Audio"  # an app consuming sound


@dataclass
class AudioChunk:
    track: str
    pcm: bytes
    t_start: float


# ==========================================================================
# graph introspection
# ==========================================================================

@dataclass
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
        n = needle.lower()
        return any(n in (v or "").lower() for v in
                   (self.name, self.description, self.app_name, self.app_binary))


@dataclass
class PwPort:
    id: int
    node_id: int
    name: str  # "output_FL", "monitor_FR", "input_MONO"
    direction: str  # "out" | "in"
    channel: str | None = None


@dataclass
class PwGraph:
    nodes: list[PwNode] = field(default_factory=list)
    ports: list[PwPort] = field(default_factory=list)
    default_sink: str | None = None
    default_source: str | None = None

    # -- lookups ------------------------------------------------------
    def by_class(self, media_class: str) -> list[PwNode]:
        return [n for n in self.nodes if n.media_class == media_class]

    def node_by_name(self, name: str) -> PwNode | None:
        return next((n for n in self.nodes if n.name == name), None)

    def find(self, needle: str, media_class: str | None = None) -> PwNode | None:
        pool = self.by_class(media_class) if media_class else self.nodes
        exact = next((n for n in pool if n.name == needle), None)
        return exact or next((n for n in pool if n.matches(needle)), None)

    def ports_of(self, node_id: int, direction: str) -> list[PwPort]:
        return sorted(
            (p for p in self.ports
             if p.node_id == node_id and p.direction == direction),
            key=lambda p: p.name)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(
            f"{tool} not found. Install PipeWire's CLI utilities:\n"
            "  Debian/Ubuntu: sudo apt install pipewire-bin pipewire-audio\n"
            "  Fedora:        sudo dnf install pipewire-utils\n"
            "  Arch:          sudo pacman -S pipewire pipewire-audio")
    return path


def dump_graph() -> PwGraph:
    """Parse `pw-dump` into something usable."""
    _require("pw-dump")
    raw = subprocess.run(["pw-dump"], capture_output=True, text=True,
                         timeout=10, check=True).stdout
    objects = json.loads(raw)

    graph = PwGraph()
    for obj in objects:
        otype = obj.get("type", "")
        info = obj.get("info") or {}
        props = info.get("props") or {}

        if otype.endswith("Interface:Node"):
            mc = props.get("media.class")
            if not mc:
                continue
            graph.nodes.append(PwNode(
                id=obj["id"],
                serial=props.get("object.serial", obj["id"]),
                name=props.get("node.name", ""),
                description=props.get("node.description", ""),
                media_class=mc,
                app_name=props.get("application.name"),
                app_binary=props.get("application.process.binary"),
                pid=props.get("application.process.id"),
            ))

        elif otype.endswith("Interface:Port"):
            graph.ports.append(PwPort(
                id=obj["id"],
                node_id=props.get("node.id", -1),
                name=props.get("port.name", ""),
                direction=props.get("port.direction", ""),
                channel=props.get("audio.channel"),
            ))

        elif otype.endswith("Interface:Metadata"):
            if (obj.get("props") or {}).get("metadata.name") != "default":
                continue
            for entry in obj.get("metadata") or []:
                value = entry.get("value")
                name = value.get("name") if isinstance(value, dict) else value
                if entry.get("key") == "default.audio.sink":
                    graph.default_sink = name
                elif entry.get("key") == "default.audio.source":
                    graph.default_source = name

    return graph


def describe_graph() -> None:
    """Human-readable dump. Run this while your call is live."""
    g = dump_graph()

    print("\n=== OUTPUT DEVICES (sinks) ===")
    for n in g.by_class(SINK):
        star = " [default]" if n.name == g.default_sink else ""
        print(f"  serial={n.serial:<6} {n.label}{star}\n"
              f"      node.name = {n.name}")

    print("\n=== INPUT DEVICES (sources / microphones) ===")
    for n in g.by_class(SOURCE):
        if n.name.endswith(".monitor"):
            continue
        star = " [default]" if n.name == g.default_source else ""
        print(f"  serial={n.serial:<6} {n.label}{star}\n"
              f"      node.name = {n.name}")

    print("\n=== APPLICATIONS CURRENTLY PLAYING AUDIO ===")
    streams = g.by_class(PLAYBACK_STREAM)
    if not streams:
        print("  (none - start your Zoom/Slack call, then run this again)")
    for n in streams:
        print(f"  serial={n.serial:<6} {n.app_name or '?'}"
              f"  binary={n.app_binary or '?'}  pid={n.pid}")
        print(f"      --app '{n.app_binary or n.app_name}'")
    print()


# ==========================================================================
# pw-record wrapper
# ==========================================================================

def _pw_version() -> tuple[int, ...]:
    out = subprocess.run(["pw-cli", "--version"], capture_output=True,
                         text=True).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


class PwRecorder:
    """
    One `pw-record` subprocess writing raw s16 mono 16 kHz to stdout.

    PipeWire does the resampling and downmixing, which is why this module
    has no numpy in it.
    """

    def __init__(self, track: str, out_q: queue.Queue, t0: float,
                 target: str | None = None,
                 capture_sink: bool = False,
                 autoconnect: bool = True,
                 latency: str = "100ms"):
        self.track = track
        self.out_q = out_q
        self.t0 = t0
        self.node_name = f"meetscribe.{track}.{uuid.uuid4().hex[:8]}"
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

        props = {"node.name": self.node_name,
                 "media.name": f"meetscribe {track}"}
        if capture_sink:
            # Attach to the target sink's monitor ports rather than
            # expecting it to be a source. This is the Linux equivalent
            # of WASAPI loopback.
            props["stream.capture.sink"] = "true"
        if not autoconnect:
            # Keep WirePlumber from helpfully linking us to the default
            # microphone; we will make our own links with pw-link.
            props["node.autoconnect"] = "false"

        self.cmd = [
            _require("pw-record"),
            "--rate", str(TARGET_RATE),
            "--channels", "1",
            "--format", "s16",
            "--latency", latency,
            # Values get quoted: PipeWire parses this as JSON-ish, so an
            # unquoted space inside a value silently splits the property.
            "--properties", "{ " + " ".join(
                f'{k}="{v}"' for k, v in props.items()) + " }",
            "--raw",
        ]
        if target:
            self.cmd[1:1] = ["--target", str(target)]
        self.cmd.append("-")

    # ------------------------------------------------------------------
    def start(self) -> None:
        log.debug("exec: %s", " ".join(self.cmd))
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True)
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name=f"pw-read-{self.track}")
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        stream = self.proc.stdout
        try:
            while True:
                buf = stream.read(BLOCK_BYTES)
                if not buf:
                    break
                # read() on a pipe can return short; pad to a whole block
                while len(buf) < BLOCK_BYTES:
                    more = stream.read(BLOCK_BYTES - len(buf))
                    if not more:
                        break
                    buf += more
                self.out_q.put(AudioChunk(self.track, buf,
                                          time.monotonic() - self.t0))
        except Exception as exc:
            log.error("reader %s stopped: %s", self.track, exc)
        finally:
            if self.proc and self.proc.poll() not in (None, 0):
                err = (self.proc.stderr.read() or b"").decode(errors="replace")
                if err.strip():
                    log.error("pw-record (%s): %s", self.track, err.strip())

    def wait_for_node(self, timeout: float = 5.0) -> PwNode | None:
        """Block until our capture node shows up in the graph."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            node = dump_graph().node_by_name(self.node_name)
            if node:
                return node
            time.sleep(0.2)
        return None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ==========================================================================
# per-application tap
# ==========================================================================

def pw_link(src_port: int, dst_port: int) -> bool:
    r = subprocess.run([_require("pw-link"), str(src_port), str(dst_port)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True
    # "File exists" just means we already linked this pair.
    if "exists" in (r.stderr or "").lower():
        return False
    log.warning("pw-link %s -> %s failed: %s", src_port, dst_port,
                (r.stderr or "").strip())
    return False


class AppTap:
    """
    Continuously links every playback stream matching `pattern` into one
    capture node.

    The link is additive: the application's existing link to the speakers
    is untouched, so the user hears the call normally. When Zoom creates a
    new stream (meeting starts, screen share, device switch) the watcher
    picks it up within `interval` seconds.
    """

    def __init__(self, pattern: str, recorder: PwRecorder,
                 stop: threading.Event, interval: float = 2.0):
        self.pattern = pattern
        self.recorder = recorder
        self.stop = stop
        self.interval = interval
        self._linked: set[tuple[int, int]] = set()
        self._seen_any = False

    def _our_inputs(self, graph: PwGraph) -> list[PwPort]:
        node = graph.node_by_name(self.recorder.node_name)
        return graph.ports_of(node.id, "in") if node else []

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                graph = dump_graph()
                sinks = self._our_inputs(graph)
                if not sinks:
                    time.sleep(self.interval)
                    continue

                for node in graph.by_class(PLAYBACK_STREAM):
                    if not node.matches(self.pattern):
                        continue
                    outs = graph.ports_of(node.id, "out")
                    if not outs:
                        continue
                    if not self._seen_any:
                        log.info("tapping %s (pid=%s)", node.label, node.pid)
                        self._seen_any = True
                    # Fan every app channel into our mono input; PipeWire
                    # sums multiple links into one port.
                    for i, src in enumerate(outs):
                        dst = sinks[min(i, len(sinks) - 1)]
                        key = (src.id, dst.id)
                        if key in self._linked:
                            continue
                        if pw_link(src.id, dst.id):
                            log.debug("linked %s:%s -> %s",
                                      node.label, src.name, dst.name)
                        self._linked.add(key)
            except Exception as exc:
                log.debug("tap watcher: %s", exc)
            self.stop.wait(self.interval)


# ==========================================================================
# top level
# ==========================================================================

class PipeWireCapture:
    """
    Sets up the capture tracks and hands back one queue per track.

    system_mode:
      "sink" - everything you hear (default sink monitor)
      "app"  - only streams matching --app, via additive pw-link
    """

    def __init__(self, mic: str | None = None,
                 system: str | None = None,
                 app: str | None = None,
                 mic_enabled: bool = True,
                 system_enabled: bool = True,
                 latency: str = "100ms"):
        _require("pw-dump")
        if _pw_version() < (0, 3, 60):
            log.warning("PipeWire < 0.3.60 detected; target.object and "
                        "stream.capture.sink may misbehave.")

        self.stop = threading.Event()
        self.queues: dict[str, queue.Queue] = {}
        self._recorders: list[PwRecorder] = []
        self._threads: list[threading.Thread] = []
        self._t0 = time.monotonic()

        graph = dump_graph()

        if mic_enabled:
            node = graph.find(mic, SOURCE) if mic else \
                graph.node_by_name(graph.default_source or "")
            if node is None:
                raise RuntimeError(
                    f"No microphone matching {mic!r}. Try: meetscribe devices")
            log.info("mic: %s (serial=%s)", node.label, node.serial)
            self._add("mic", PwRecorder("mic", self._q("mic"), self._t0,
                                        target=node.serial, latency=latency))

        if system_enabled and app:
            rec = PwRecorder("system", self._q("system"), self._t0,
                             target=None, autoconnect=False, latency=latency)
            self._add("system", rec)
            self._app_pattern = app
        elif system_enabled:
            node = graph.find(system, SINK) if system else \
                graph.node_by_name(graph.default_sink or "")
            if node is None:
                raise RuntimeError(
                    f"No output device matching {system!r}. "
                    "Try: meetscribe devices")
            log.info("system: monitor of %s (serial=%s)", node.label, node.serial)
            self._add("system", PwRecorder(
                "system", self._q("system"), self._t0,
                target=node.serial, capture_sink=True, latency=latency))
            self._app_pattern = None
        else:
            self._app_pattern = None

        if not self._recorders:
            raise RuntimeError("Nothing to capture: both tracks disabled.")

    def _q(self, track: str) -> queue.Queue:
        return self.queues.setdefault(track, queue.Queue(maxsize=400))

    def _add(self, track: str, rec: PwRecorder) -> None:
        self._q(track)
        self._recorders.append(rec)

    # ------------------------------------------------------------------
    def start(self) -> None:
        for rec in self._recorders:
            rec.start()

        if self._app_pattern:
            rec = next(r for r in self._recorders if r.track == "system")
            if rec.wait_for_node() is None:
                raise RuntimeError(
                    "Capture node never appeared - is pw-record working? "
                    "Test with: pw-record --target=0 /tmp/t.wav")
            tap = AppTap(self._app_pattern, rec, self.stop)
            t = threading.Thread(target=tap.run, daemon=True, name="app-tap")
            t.start()
            self._threads.append(t)
            log.info("watching for streams matching %r", self._app_pattern)

    def shutdown(self) -> None:
        self.stop.set()
        for rec in self._recorders:
            rec.stop()
        for t in self._threads:
            t.join(timeout=2.0)
