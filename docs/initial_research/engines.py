"""
Speech-to-text back ends.

Every engine consumes AudioChunk objects from a queue and emits Segment
objects. Swapping engines is a config change, not a code change.

  google   Cloud Speech-to-Text v2 (chirp_3) - streaming gRPC
  deepgram Nova-3 - streaming WebSocket
  local    faster-whisper - offline, chunked, no network
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono


@dataclass
class Segment:
    track: str
    text: str
    is_final: bool
    t_start: float
    t_end: float
    confidence: float | None = None
    speaker: str | None = None
    words: list[dict] = field(default_factory=list)


class Engine(Protocol):
    def run(self, track: str, audio_q: queue.Queue,
            out_q: queue.Queue, stop: threading.Event) -> None: ...


# ==========================================================================
# helper: adapt a blocking queue into a generator, with silence gating
# ==========================================================================

class ChunkReader:
    """Pulls AudioChunks, optionally drops silence, tracks stream age."""

    def __init__(self, audio_q: queue.Queue, stop: threading.Event,
                 vad_aggressiveness: int | None = 2):
        self.q = audio_q
        self.stop = stop
        self.vad = None
        self.last_t = 0.0
        self._silence_run = 0
        if vad_aggressiveness is not None:
            try:
                import webrtcvad
                self.vad = webrtcvad.Vad(vad_aggressiveness)
            except ImportError:
                log.warning("webrtcvad not installed - streaming silence too "
                            "(costs more). pip install webrtcvad")

    def _is_speech(self, pcm: bytes) -> bool:
        if self.vad is None:
            return True
        # webrtcvad wants 10/20/30 ms frames; our blocks are 100 ms.
        frame = SAMPLE_RATE * 20 // 1000 * 2  # 640 bytes
        hits = sum(
            1 for i in range(0, len(pcm) - frame + 1, frame)
            if self.vad.is_speech(pcm[i:i + frame], SAMPLE_RATE)
        )
        return hits > 0

    def blocks(self, max_seconds: float | None = None) -> Iterator[bytes]:
        """Yield PCM blocks, ending cleanly after max_seconds of stream age."""
        started = time.monotonic()
        while not self.stop.is_set():
            if max_seconds and time.monotonic() - started > max_seconds:
                log.debug("rotating stream after %.0fs", max_seconds)
                return
            try:
                chunk = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            self.last_t = chunk.t_start
            if self._is_speech(chunk.pcm):
                self._silence_run = 0
                yield chunk.pcm
            else:
                # Keep a little silence flowing so the engine can finalise,
                # then go quiet to avoid paying for dead air.
                self._silence_run += 1
                if self._silence_run <= 5:
                    yield chunk.pcm


# ==========================================================================
# Google Cloud Speech-to-Text v2
# ==========================================================================

class GoogleV2Engine:
    """
    Streaming recognition against Speech-to-Text v2.

    The important constraint: a single StreamingRecognize call may stay open
    for at most 5 minutes. Meetings are longer than that, so the run loop
    tears the stream down at ~4 minutes and opens a fresh one. Offsets are
    accumulated so timestamps stay continuous across rotations.
    """

    MAX_STREAM_SECONDS = 240  # 4 min, safely under Google's 5 min ceiling

    def __init__(self, project_id: str, region: str = "us",
                 language_codes: tuple[str, ...] = ("en-US",),
                 model: str = "chirp_3",
                 interim: bool = True,
                 phrases: list[str] | None = None,
                 vad_aggressiveness: int | None = 2):
        from google.api_core.client_options import ClientOptions
        from google.cloud.speech_v2 import SpeechClient

        self.project_id = project_id
        self.region = region
        self.language_codes = list(language_codes)
        self.model = model
        self.interim = interim
        self.phrases = phrases or []
        self.vad_aggressiveness = vad_aggressiveness

        endpoint = ("speech.googleapis.com" if region == "global"
                    else f"{region}-speech.googleapis.com")
        self.client = SpeechClient(
            client_options=ClientOptions(api_endpoint=endpoint))
        self.recognizer = (
            f"projects/{project_id}/locations/{region}/recognizers/_")

    def _config_request(self):
        from google.cloud.speech_v2.types import cloud_speech as cs

        features = cs.RecognitionFeatures(
            enable_automatic_punctuation=True,
            enable_word_time_offsets=True,
        )

        adaptation = None
        if self.phrases:
            adaptation = cs.SpeechAdaptation(
                phrase_sets=[cs.SpeechAdaptation.AdaptationPhraseSet(
                    inline_phrase_set=cs.PhraseSet(phrases=[
                        cs.PhraseSet.Phrase(value=p, boost=15.0)
                        for p in self.phrases
                    ]))])

        config = cs.RecognitionConfig(
            explicit_decoding_config=cs.ExplicitDecodingConfig(
                encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=SAMPLE_RATE,
                audio_channel_count=1,
            ),
            language_codes=self.language_codes,
            model=self.model,
            features=features,
            **({"adaptation": adaptation} if adaptation else {}),
        )

        streaming_config = cs.StreamingRecognitionConfig(
            config=config,
            streaming_features=cs.StreamingRecognitionFeatures(
                interim_results=self.interim,
                # Let Google close the stream on a long pause so we get
                # finals promptly instead of waiting for rotation.
                voice_activity_timeout=cs.StreamingRecognitionFeatures
                .VoiceActivityTimeout(
                    speech_start_timeout={"seconds": 0},
                    speech_end_timeout={"seconds": 0},
                ),
            ),
        )
        return cs.StreamingRecognizeRequest(
            recognizer=self.recognizer, streaming_config=streaming_config)

    def run(self, track: str, audio_q: queue.Queue, out_q: queue.Queue,
            stop: threading.Event) -> None:
        from google.cloud.speech_v2.types import cloud_speech as cs

        reader = ChunkReader(audio_q, stop, self.vad_aggressiveness)
        offset = 0.0

        while not stop.is_set():
            def requests():
                yield self._config_request()
                for pcm in reader.blocks(max_seconds=self.MAX_STREAM_SECONDS):
                    yield cs.StreamingRecognizeRequest(audio=pcm)

            try:
                responses = self.client.streaming_recognize(requests=requests())
                for response in responses:
                    if stop.is_set():
                        break
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        alt = result.alternatives[0]
                        start = offset + _dur(result.result_end_offset) - \
                            _estimate_len(alt)
                        out_q.put(Segment(
                            track=track,
                            text=alt.transcript.strip(),
                            is_final=result.is_final,
                            t_start=max(start, offset),
                            t_end=offset + _dur(result.result_end_offset),
                            confidence=alt.confidence or None,
                            words=[{
                                "word": w.word,
                                "start": offset + _dur(w.start_offset),
                                "end": offset + _dur(w.end_offset),
                            } for w in alt.words],
                        ))
            except Exception as exc:
                if stop.is_set():
                    break
                log.warning("google stream error (%s), reconnecting in 2s: %s",
                            track, exc)
                time.sleep(2)

            offset = reader.last_t


def _dur(d) -> float:
    """protobuf Duration -> float seconds (tolerates None)."""
    if d is None:
        return 0.0
    return getattr(d, "total_seconds", lambda: 0.0)()


def _estimate_len(alt) -> float:
    if alt.words:
        return _dur(alt.words[-1].end_offset) - _dur(alt.words[0].start_offset)
    return 0.0


# ==========================================================================
# Deepgram (streaming WebSocket)
# ==========================================================================

class DeepgramEngine:
    """
    Nova-3 over WebSocket. No 5-minute cap, lower latency than Google,
    and diarization works in streaming mode (Google's does not).
    """

    URL = ("wss://api.deepgram.com/v1/listen"
           "?encoding=linear16&sample_rate=16000&channels=1"
           "&model=nova-3&smart_format=true&interim_results=true"
           "&punctuate=true&diarize=true")

    def __init__(self, api_key: str | None = None, language: str = "multi",
                 vad_aggressiveness: int | None = 2):
        self.api_key = api_key or os.environ["DEEPGRAM_API_KEY"]
        self.language = language
        self.vad_aggressiveness = vad_aggressiveness

    def run(self, track: str, audio_q: queue.Queue, out_q: queue.Queue,
            stop: threading.Event) -> None:
        from websockets.sync.client import connect

        url = f"{self.URL}&language={self.language}"
        reader = ChunkReader(audio_q, stop, self.vad_aggressiveness)

        while not stop.is_set():
            try:
                with connect(url, additional_headers={
                        "Authorization": f"Token {self.api_key}"}) as ws:
                    log.info("deepgram connected (%s)", track)

                    def pump():
                        for pcm in reader.blocks():
                            ws.send(pcm)
                        ws.send(json.dumps({"type": "CloseStream"}))

                    t = threading.Thread(target=pump, daemon=True,
                                         name=f"dg-send-{track}")
                    t.start()

                    for raw in ws:
                        if stop.is_set():
                            break
                        msg = json.loads(raw)
                        if msg.get("type") != "Results":
                            continue
                        alt = msg["channel"]["alternatives"][0]
                        if not alt["transcript"]:
                            continue
                        words = alt.get("words", [])
                        spk = (f"S{words[0]['speaker']}"
                               if words and "speaker" in words[0] else None)
                        out_q.put(Segment(
                            track=track,
                            text=alt["transcript"],
                            is_final=msg.get("is_final", False),
                            t_start=msg.get("start", 0.0),
                            t_end=msg.get("start", 0.0) + msg.get("duration", 0.0),
                            confidence=alt.get("confidence"),
                            speaker=spk,
                            words=words,
                        ))
            except Exception as exc:
                if stop.is_set():
                    break
                log.warning("deepgram error (%s), retrying in 2s: %s", track, exc)
                time.sleep(2)


# ==========================================================================
# Local faster-whisper
# ==========================================================================

class LocalWhisperEngine:
    """
    Offline fallback. Whisper is not a streaming model, so this buffers
    speech until a pause and transcribes the utterance. Latency is one
    utterance, not sub-second, but nothing leaves the machine.
    """

    def __init__(self, model_size: str = "small", device: str = "auto",
                 language: str | None = None, compute_type: str = "int8",
                 max_utterance_s: float = 25.0,
                 silence_to_flush_s: float = 0.8):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_size, device=device,
                                  compute_type=compute_type)
        self.language = language
        self.max_utterance_s = max_utterance_s
        self.silence_to_flush_s = silence_to_flush_s

    def run(self, track: str, audio_q: queue.Queue, out_q: queue.Queue,
            stop: threading.Event) -> None:
        import numpy as np

        reader = ChunkReader(audio_q, stop, vad_aggressiveness=None)
        vad = None
        try:
            import webrtcvad
            vad = webrtcvad.Vad(2)
        except ImportError:
            pass

        buf = bytearray()
        silence = 0.0
        utt_start = 0.0

        def flush():
            nonlocal buf, silence
            if len(buf) < BYTES_PER_SEC // 2:  # < 0.5 s, ignore
                buf = bytearray()
                return
            audio = (np.frombuffer(bytes(buf), dtype="<i2")
                     .astype("float32") / 32768.0)
            segs, _ = self.model.transcribe(
                audio, language=self.language, vad_filter=True,
                beam_size=1, condition_on_previous_text=False)
            for s in segs:
                out_q.put(Segment(track=track, text=s.text.strip(),
                                  is_final=True,
                                  t_start=utt_start + s.start,
                                  t_end=utt_start + s.end))
            buf = bytearray()
            silence = 0.0

        while not stop.is_set():
            try:
                chunk = audio_q.get(timeout=0.25)
            except queue.Empty:
                continue

            speech = True
            if vad is not None:
                frame = SAMPLE_RATE * 20 // 1000 * 2
                speech = any(
                    vad.is_speech(chunk.pcm[i:i + frame], SAMPLE_RATE)
                    for i in range(0, len(chunk.pcm) - frame + 1, frame))

            if speech:
                if not buf:
                    utt_start = chunk.t_start
                buf.extend(chunk.pcm)
                silence = 0.0
            elif buf:
                buf.extend(chunk.pcm)
                silence += 0.1

            if buf and (silence >= self.silence_to_flush_s
                        or len(buf) / BYTES_PER_SEC >= self.max_utterance_s):
                flush()

        flush()


# ==========================================================================

def build_engine(name: str, cfg: dict) -> Engine:
    if name == "google":
        return GoogleV2Engine(
            project_id=cfg.get("project_id") or os.environ["GOOGLE_CLOUD_PROJECT"],
            region=cfg.get("region", "us"),
            language_codes=tuple(cfg.get("language_codes", ["en-US"])),
            model=cfg.get("model", "chirp_3"),
            interim=cfg.get("interim", True),
            phrases=cfg.get("phrases"),
        )
    if name == "deepgram":
        return DeepgramEngine(api_key=cfg.get("api_key"),
                              language=cfg.get("language", "multi"))
    if name == "local":
        return LocalWhisperEngine(model_size=cfg.get("model", "small"),
                                  device=cfg.get("device", "auto"),
                                  language=cfg.get("language"))
    raise ValueError(f"Unknown engine: {name!r}")
