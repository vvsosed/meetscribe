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
        self._stderr_file.flush()
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
        result = subprocess.run(
            [require_tool("pw-link"), str(src_port), str(dst_port)],
            capture_output=True,
            text=True,
        )
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
    except (MissingToolError, subprocess.SubprocessError):
        return (0, 0, 0)
    return parse_pw_version(output)
