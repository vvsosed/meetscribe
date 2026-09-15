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
