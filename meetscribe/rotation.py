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
        """Clock for the next stream. max() guards against rewinding."""
        return replace(self, offset=max(self.offset, last_chunk_t))
