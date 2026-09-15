"""Transcript sinks: live console + durable JSONL + readable Markdown."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

LABELS = {"mic": "You", "system": "Them"}
COLORS = {"mic": "\033[36m", "system": "\033[33m"}
DIM, RESET = "\033[2m", "\033[0m"


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class TranscriptWriter:
    """
    Writes each final segment to disk immediately (JSONL is append-only, so
    an unclean exit still leaves you with everything up to that moment) and
    renders a rolling interim line to the terminal.
    """

    def __init__(self, outdir: Path, session: str | None = None,
                 show_interim: bool = True, color: bool = True):
        outdir.mkdir(parents=True, exist_ok=True)
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        self.show_interim = show_interim and sys.stdout.isatty()
        self.color = color and sys.stdout.isatty()
        self._lock = threading.Lock()
        self._interim_len = 0
        self._segments: list[dict] = []
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------
    def _clear_interim(self) -> None:
        if self._interim_len:
            sys.stdout.write("\r" + " " * self._interim_len + "\r")
            self._interim_len = 0

    def _paint(self, track: str, text: str) -> str:
        if not self.color:
            return text
        return f"{COLORS.get(track, '')}{text}{RESET}"

    # ------------------------------------------------------------------
    def write(self, seg) -> None:
        with self._lock:
            label = LABELS.get(seg.track, seg.track)
            if seg.speaker:
                label = f"{label}/{seg.speaker}"

            if not seg.is_final:
                if self.show_interim:
                    self._clear_interim()
                    line = f"{hhmmss(seg.t_start)} {label}: {seg.text}"
                    line = line[:160]
                    sys.stdout.write(DIM + line + RESET if self.color else line)
                    sys.stdout.flush()
                    self._interim_len = len(line)
                return

            self._clear_interim()
            stamp = hhmmss(seg.t_start)
            print(f"{DIM if self.color else ''}{stamp}{RESET if self.color else ''} "
                  f"{self._paint(track=seg.track, text=label + ':')} {seg.text}")

            record = {
                "t": seg.t_start,
                "t_end": seg.t_end,
                "track": seg.track,
                "speaker": seg.speaker,
                "text": seg.text,
                "confidence": seg.confidence,
                "wall_clock": datetime.now(timezone.utc).isoformat(),
            }
            self._segments.append(record)
            self._jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._jsonl.flush()

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._clear_interim()
            self._jsonl.close()
            self._segments.sort(key=lambda r: r["t"])

            lines = [f"# Transcript {self.session}", ""]
            last_label = None
            for r in self._segments:
                label = LABELS.get(r["track"], r["track"])
                if r["speaker"]:
                    label = f"{label}/{r['speaker']}"
                if label != last_label:
                    lines.append("")
                    lines.append(f"**{label}** _{hhmmss(r['t'])}_")
                    last_label = label
                lines.append(r["text"])
            self.md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"\nSaved:\n  {self.jsonl_path}\n  {self.md_path}")
        return self.md_path
