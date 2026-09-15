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
