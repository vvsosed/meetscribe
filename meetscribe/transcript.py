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
