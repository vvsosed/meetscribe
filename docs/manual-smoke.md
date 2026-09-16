# Manual smoke test

The automated suite (145 tests) runs with no audio hardware, no network and no
credentials — everything that touches PipeWire, Google, or the wall clock is
faked. That proves the logic is right; it cannot prove audio actually flows.
This checklist covers what the suite can't. Run it once against a real call
before trusting a change that touches capture, rotation or timestamps.

You'll need: a running PipeWire session, a way to generate a call with two
sides (a real meeting, or two browser tabs in the same call), headphones (see
the README's "Notes worth knowing"), and working GCP credentials.

## 1. `devices` sees the running call

Start a call first — join a meeting, or open the call in a tab — and only
then run:

```bash
uv run meetscribe devices
```

Check:

- Your speakers/headphones are listed under `OUTPUT DEVICES (sinks)`, with
  `[default]` next to whichever one is actually your default.
- Your microphone is listed under `INPUT DEVICES (sources / microphones)`,
  with `[default]` if it's the system default.
- Nothing ending in `.monitor` appears under the microphone list — monitors
  are sinks' shadow sources and must not be offered as mic candidates.
- The call application (Zoom, the browser tab, etc.) appears under
  `APPLICATIONS CURRENTLY PLAYING AUDIO`, with a ready-to-use `--app '...'`
  hint underneath its line.

If the application is missing, confirm it is actually making sound at that
moment — this list is empty until a stream exists, not until the app is
running.

## 2. A live run actually transcribes both sides

```bash
uv run meetscribe run --app <name> --lang uk-UA --lang en-US
```

Check:

- The log line `watching for streams matching '<name>'` appears at startup.
- When the other side of the call speaks, text tagged **Them** appears within
  a few seconds.
- When you speak, text tagged **You** appears.
- The call keeps playing through your speakers/headphones completely
  unchanged throughout — meetscribe's tap is additive, it must not mute or
  redirect anything.

## 3. Stream rotation across the 4-minute mark

Keep the same session running and talk (or have the other side talk) past
the four-minute mark.

Check:

- Transcription keeps working uninterrupted across that boundary — this is
  the point where the 4-minute proactive stream rotation tears down the old
  `StreamingRecognize` call and opens a new one.
- Timestamps in the output keep increasing monotonically across the
  rotation; they must not reset to zero or jump backwards.

## 4. Timestamp accuracy against real silence (the one automation is weakest on)

This is the single check most worth doing by hand, because it is the one the
fake-clock tests are least able to stand in for.

Procedure: start a run, sit completely quiet for about three minutes, then
say one short sentence, then Ctrl-C.

Check: in the resulting `.md`, that sentence should be stamped near
`00:03:00` — not near `00:00:0x`.

Check, in the same run: the log stays quiet through those three minutes. A
`409 Stream timed out after receiving no more client requests`, or any
reconnect at all while nothing is being said, means the keepalive in
`EngineWorker.blocks()` (`meetscribe/google.py`) has regressed.

Then do the harder half, which is a *different* failure with the same error:
with `--app`, **stop the audio at the source** — end the video, or close the
tab — and leave the run going for a minute. That unlinks the capture node, and
PipeWire emits nothing at all rather than silence, so no block reaches the gate
and a gate-level keepalive cannot save it. This is how that bug shipped twice:
the first fix only covered the quiet-but-still-flowing case above, and the
smoke test only exercised that one.

Why this matters: the silence gate drops quiet audio blocks before they are
ever sent to Google, so the engine's own result offsets only count the audio
it actually received, not wall-clock elapsed time. `AudioTimeline` (in
`meetscribe/rotation.py`) is what maps those audio-relative offsets back onto
real capture time. The unit tests exercise `AudioTimeline` against a fake
clock and fake gate; they cannot prove the mapping holds up against a real
VAD gate dropping a real three minutes of room tone into a real streaming
session. If this drifts, it means the offset math is wrong against a real
engine, and — because the two tracks (You/Them) drop different amounts of
silence — the two tracks would also drift apart from each other, corrupting
the interleaving in the saved Markdown.

## 5. Shutdown and output on Ctrl-C

Ctrl-C the session from step 2 or 3 and check:

- `Saved:` is printed with both the `.jsonl` and `.md` paths.
- The `.md` groups consecutive same-speaker lines under one **You** / **Them**
  heading rather than repeating the heading per line.
- The `.jsonl` has exactly one line per finalised segment (open it and count
  — no partial/interim entries, no duplicates).
- Reading the `.md` top to bottom, the You/Them blocks interleave in the
  order the conversation actually happened, not grouped by track.

## 6. Failure paths (worth exercising once per change to this area)

```bash
uv run meetscribe run --app nonexistent-app-xyz
```
Check: it keeps polling (re-scanning the graph every couple of seconds)
rather than exiting — the app might still start its stream late.

```bash
uv run meetscribe run --mic nonexistent-mic-xyz
```
Check: it exits immediately with a message suggesting `meetscribe devices`,
rather than starting a session with no mic input.

```bash
uv run meetscribe run --project not-a-real-project
```
Check: it exits immediately (a fatal auth/config error), rather than retrying
forever the way a transient network error would.
