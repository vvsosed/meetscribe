# meetscribe — initial research

> Write-up and example files from the initial research spike, kept as reference.
> This is **not** the application: the code here is illustrative, incomplete and
> not wired up to run. The real implementation lives in its own package once it
> is written.

Console app that transcribes any call on your machine — Zoom, Slack huddles,
Meet, Teams, Discord, a browser tab — by capturing audio from PipeWire instead
of integrating with each platform's API.

```
 Zoom stream ──┬──► your speakers        (untouched, you still hear the call)
               └──► meetscribe capture   (additive pw-link — a copy, not a move)

 microphone ─────► meetscribe capture

        both tracks: s16 / 16 kHz / mono, resampled by PipeWire
                          │
                   VAD silence gate
                          │
             google │ deepgram │ local whisper
                          │
             console  +  .jsonl  +  .md
```

Capturing the mic and the call separately gives you speaker separation between
"you" and "them" for free — no diarization model needed.

## Why PipeWire specifically

A PipeWire output port can feed **several** input ports at once. That means you
can tap Zoom's audio stream directly while it keeps playing to your speakers.
PulseAudio can't do this — there you capture the whole sink monitor and get
Spotify, Telegram pings and YouTube in your transcript, or you move the stream
to a null sink and stop hearing it.

```bash
uv run python -m meetscribe run --app zoom      # only Zoom's audio
uv run python -m meetscribe run                 # everything you hear (sink monitor)
```

## Requirements

```bash
# Debian/Ubuntu
sudo apt install pipewire-bin pipewire-audio wireplumber
# Fedora
sudo dnf install pipewire-utils
# Arch
sudo pacman -S pipewire pipewire-audio wireplumber

# uv, if you don't have it yet
curl -LsSf https://astral.sh/uv/install.sh | sh

cd docs/initial_research    # pyproject.toml and uv.lock live here
uv sync --extra google      # or: --extra deepgram / --extra local
```

`uv sync` creates `.venv/` here and installs from `uv.lock`, so everyone gets the
same versions. There is one extra per engine and the engine SDKs are imported lazily,
so installing only the one you use keeps grpc or ctranslate2 out of your
environment entirely. Prefix commands with `uv run` and you never activate a
venv by hand.

PipeWire ≥ 0.3.60 (for `target.object` and `stream.capture.sink`). Check with
`pw-cli --version`. Verify the tools work before anything else:

```bash
pw-dump | head            # graph as JSON
pw-record --target=0 /tmp/t.wav   # Ctrl-C, then play it back
```

## Use

```bash
# start your call FIRST, then look at what's playing
uv run python -m meetscribe devices

#   === APPLICATIONS CURRENTLY PLAYING AUDIO ===
#     serial=1204   ZOOM VoiceEngine  binary=zoom  pid=44321
#         --app 'zoom'
#     serial=1250   Spotify  binary=spotify  pid=9001

export GOOGLE_APPLICATION_CREDENTIALS=~/keys/stt.json
export GOOGLE_CLOUD_PROJECT=my-project

uv run python -m meetscribe run --app zoom --region eu --lang uk-UA --lang en-US
uv run python -m meetscribe run --app slack --engine deepgram
uv run python -m meetscribe run --engine local --model medium      # offline
uv run python -m meetscribe run --no-mic                           # them only
uv run python -m meetscribe run --phrase Kubernetes --phrase Poltava
```

Ctrl-C writes `transcripts/<session>.md`. The `.jsonl` alongside it is
append-only, so an unclean exit still keeps everything up to that moment.

## How the pieces map to PipeWire

| what | how |
|---|---|
| find devices and apps | `pw-dump` → JSON, filtered by `media.class` |
| whole system audio | `pw-record --target=<sink serial>` + `stream.capture.sink=true` |
| one application | capture node with `node.autoconnect=false`, then `pw-link` the app's output ports to it |
| resampling to 16 kHz mono | `--rate 16000 --channels 1 --format s16` — PipeWire does it |
| identify nodes | `object.serial`, **never** `object.id` — ids get reused |

`AppTap` re-scans the graph every 2 s. This matters: Zoom doesn't create its
audio stream until the meeting actually starts, so a recorder that resolves
nodes once at launch records silence. The same watcher handles reconnects when
someone switches headphones mid-call.

## Alternative: in-process via GStreamer

If you'd rather not shell out to `pw-record`, `pipewiresrc` does the same job
inside Python with PyGObject (`sudo apt install python3-gi gstreamer1.0-pipewire`):

```python
import gi; gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)

pipeline = Gst.parse_launch(
    f"pipewiresrc target-object={serial} "
    '  stream-properties="p,stream.capture.sink=true" '
    "! audioconvert ! audioresample "
    "! audio/x-raw,format=S16LE,rate=16000,channels=1 "
    "! appsink name=out emit-signals=true max-buffers=10 drop=true")

def on_sample(sink):
    buf = sink.emit("pull-sample").get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if ok:
        handle(bytes(info.data))       # s16 mono 16 kHz
        buf.unmap(info)
    return Gst.FlowReturn.OK

pipeline.get_by_name("out").connect("new-sample", on_sample)
pipeline.set_state(Gst.State.PLAYING)
```

Trade-off: no subprocess and real callbacks, but you inherit GLib's main loop
and a much heavier dependency. `pw-record` keeps failures obvious and
debuggable from a shell. The `python3-pipewire` bindings are not mature enough
to recommend — you end up writing the `pw_main_loop` glue yourself.

## Things that bite

**Google's 5-minute stream cap.** A single `StreamingRecognize` call may stay
open for at most 5 minutes. `GoogleV2Engine` tears the stream down at 4 minutes
and opens a fresh one, carrying a time offset forward. Without it,
transcription silently stops mid-meeting.

**Chirp 3 won't diarize in streaming mode** — only in `Recognize` /
`BatchRecognize`. For live per-person labels use Deepgram. If you can wait,
keep the raw audio and run `BatchRecognize` afterwards for better accuracy
*and* diarization.

**Chirp 3 regions.** `us`, `eu`, `asia-northeast1`, `asia-southeast1` are GA;
`europe-west2/3` are still preview. From Ukraine, use `--region eu`.

**Echo on speakers.** Your mic picks up the call and transcribes "them" twice.
Headphones fix it. Otherwise load PipeWire's canceller:

```bash
pactl load-module module-echo-cancel               # quick test
# permanent: ~/.config/pipewire/pipewire.conf.d/99-echo-cancel.conf
#   context.modules = [ { name = libpipewire-module-echo-cancel } ]
```
then point `--mic` at the resulting `echo-cancel-source` node.

**Wayland is irrelevant here.** Audio capture needs no portal or screen-share
permission — that's video only.

**Consent.** Ukraine and the EU treat recording a conversation without telling
the participants as a legal question, not a UX one. Announce it.
