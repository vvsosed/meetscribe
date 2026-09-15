"""Link a matching application's audio into our capture node.

The link is additive. The application keeps its existing link to the
speakers, so the user still hears the call, and PipeWire delivers an
identical copy to us.

The watcher re-scans because applications create their audio streams late:
Zoom does it when the meeting starts, not when the app launches. Anything
that resolves nodes once at startup records silence. The same re-scan covers
reconnects when someone switches headphones mid-call.
"""

from __future__ import annotations

import logging
import threading

from .graph import PLAYBACK_STREAM
from .ports import Clock, GraphSource, Linker, LinkResult

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0


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
        self._linked: set[tuple[int, int]] = set()
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
                pair = (out_port.id, in_port.id)
                if pair in self._linked:
                    continue
                self._linked.add(pair)
                if self._linker.link(out_port.id, in_port.id) is LinkResult.LINKED:
                    created += 1
        return created

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # a transient graph read must not kill us
                log.debug("tap watcher: %s", exc)
            self._clock.sleep(self._interval)
