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
