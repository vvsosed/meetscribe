# meetscribe

Live transcription of any call on your machine — Zoom, Slack huddles, Meet,
Teams, Discord, a browser tab — captured straight from **PipeWire** instead of
integrating with each platform's API.

Your microphone and the call are captured as two separate tracks, which gives
"You" vs "Them" separation for free — no diarization model needed.
Transcription is Google Cloud Speech-to-Text v2 (`chirp_3`), streaming.

## Why PipeWire

A PipeWire output port can feed several input ports at once. That means
meetscribe can tap an application's audio *in addition to* its existing link
to your speakers: the call keeps playing normally, and meetscribe gets an
identical copy of the same stream.

On PulseAudio the alternatives are both worse: capture the whole sink monitor
and get Spotify and notification chimes mixed into your transcript, or move
the app's stream to a null sink and stop hearing the call yourself.

## Requirements

- Linux with PipeWire >= 0.3.60 (needed for `target.object` and
  `stream.capture.sink`).
- A GCP project with the Speech-to-Text API enabled, and a service account key
  with access to it.

Install PipeWire's CLI utilities if they aren't already present:

```bash
# Arch
sudo pacman -S pipewire pipewire-audio wireplumber

# Debian/Ubuntu
sudo apt install pipewire-bin pipewire-audio wireplumber
```

Then, from the repository root:

```bash
uv sync
```

`uv` is the only supported way to install and run this project — never `pip
install` or activate `.venv` by hand; `uv run` does both, from the lockfile.

## Use

Set your GCP credentials:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/keys/stt.json
export GOOGLE_CLOUD_PROJECT=my-project
```

**Start your call first, then run `meetscribe devices`.** An application does
not appear anywhere in the PipeWire graph until it actually starts an audio
stream — Zoom, for instance, only creates one once the meeting itself starts,
not when the app launches. Running `devices` before that shows no application
at all, which is the single most common way to get confused on a first run.

```bash
uv run meetscribe devices
```

```
=== OUTPUT DEVICES (sinks) ===
  serial=62     ... Analog Stereo [default]
      node.name = alsa_output.pci-....analog-stereo

=== INPUT DEVICES (sources / microphones) ===
  serial=60     USB Microphone Mono [default]
      node.name = alsa_input.usb-....mono-fallback

=== APPLICATIONS CURRENTLY PLAYING AUDIO ===
  serial=2328   zoom.real  binary=zoom  pid=3553
      --app 'zoom'
```

Then start transcribing. Some realistic invocations:

```bash
# Tap only Zoom's audio, transcribing Ukrainian and English
uv run meetscribe run --app zoom --lang uk-UA --lang en-US

# Only the call, not your own mic (e.g. you're just listening)
uv run meetscribe run --app slack --no-mic

# Boost recognition of names and jargon that would otherwise get mangled
uv run meetscribe run --app zoom --phrase "Volodymyr" --phrase "meetscribe"
```

`--app` matches on the application's name/binary substring shown by `devices`.
Without `--app` (and without `--system`), meetscribe captures your default
output device's monitor instead — the whole-sink equivalent, with everything
that implies (see "Why PipeWire" above).

## Output

Ctrl-C stops the session and writes `transcripts/<session>.md` — a readable
transcript grouped into **You** / **Them** blocks in chronological order.

A `.jsonl` file next to it (one JSON object per finalised segment) is opened
in append mode and flushed after every final segment, so an unclean exit —
a crash, a killed terminal, `kill -9` — still leaves everything transcribed up
to that moment on disk. Only the `.md` is written at the very end, from
whatever finals accumulated.

## Notes worth knowing

- **Wear headphones.** Otherwise your microphone re-transcribes the call
  audio coming out of your speakers, and the other party ends up appearing on
  both the You and Them tracks. If headphones aren't an option, run:

  ```bash
  pactl load-module module-echo-cancel
  ```

  and point `--mic` at the resulting `echo-cancel-source`.

- **Chirp 3 regions.** `us`, `eu`, `asia-northeast1` and `asia-southeast1` are
  generally available; `europe-west2` and `europe-west3` are preview. The
  default (`--region`) is `eu`.

- **Consent.** Recording call participants without telling them is a legal
  question in Ukraine and the EU, not a UX one. That's why meetscribe
  announces on startup that the session is being recorded — it is not just a
  courtesy message.

## Known limitations

These are real, current limitations found during review — not aspirational
TODOs.

- **A network outage loses audio, silently.** During an outage the speech
  engine backs off and retries, but the audio queue behind it is bounded and
  overflows after about 40 seconds of being unable to send. The number of
  dropped blocks is logged, but the transcript itself carries no marker for
  the gap — a dropped stretch just reads as if nobody was talking.
- **A stalled connection can take 4.5 minutes to notice.** A healthy stream is
  rotated proactively every 4 minutes; a stream that has stopped responding
  entirely is only caught by a 270-second (4.5 minute) deadline on the whole
  call, rather than detected within seconds. gRPC keepalive pings would catch
  this much sooner.
- **Ctrl-C can take 10-12 seconds in the worst case** — if `pw-record` needs
  escalating from SIGTERM to SIGKILL and the speech workers are mid-backoff
  sleep when they're asked to stop. The common case is well under a second.
- **`pw-link` isn't checked at startup.** `pw-dump` and `pw-record` are, but
  `pw-link` is only invoked lazily once `--app` mode tries to make its first
  link. On a machine that's missing it, `--app` mode logs a warning after
  about six seconds and then runs for the rest of the session with an empty
  **Them** track.
- **Two sessions started in the same second share a `.jsonl` file.** The
  session name comes from a one-second-resolution timestamp and the file is
  opened in append mode, so two runs launched within the same wall-clock
  second write into the same file.
- **Per-word timestamps don't reach the output.** Google returns them and
  meetscribe computes them, but only the segment-level start/end time is
  written to the `.jsonl`/`.md` output today.

## More detail

- `docs/superpowers/specs/` — the design.
- `docs/superpowers/plans/` — the implementation plan.
- `docs/manual-smoke.md` — a checklist of what the automated test suite
  cannot verify (it runs with no audio hardware, network or credentials).
- `docs/initial_research/` — the research spike that preceded this package.
  Reference material only, not part of the codebase: it has its own
  `pyproject.toml` and `uv.lock` and is not wired up to run.
