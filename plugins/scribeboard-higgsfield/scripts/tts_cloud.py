r"""tts_cloud.py - cloud text-to-speech for narrate.py: ElevenLabs and OpenAI, one sentence per request.

Library only (stdlib urllib, no SDK):
  speak(engine, text, out_stem, seed=None, previous_text=None, next_text=None) -> Path   one take, any audio format
  clone_voice(sample, name, remove_background_noise=False) -> dict                    ElevenLabs Instant Voice Cloning
  api_key(engine) -> str | None, key_env(engine) -> str, problems(engine) -> [str]

API keys come only from the environment (ELEVENLABS_API_KEY, OPENAI_API_KEY), never from a settings file. Voice,
model, format and server come from settings (scripts/settings.py) tts.elevenlabs.* / tts.openai.*.

ElevenLabs  POST {base}/v1/text-to-speech/{voice_id}?output_format=F, header xi-api-key, JSON {text, model_id, seed,
            previous_text, next_text}; POST {base}/v1/voices/add (multipart: name, files) for a clone.
            https://elevenlabs.io/docs/api-reference/text-to-speech/convert
            https://elevenlabs.io/docs/api-reference/voices/ivc/create
OpenAI      POST {base}/audio/speech, header Authorization: Bearer KEY, JSON {model, voice, input, response_format,
            instructions}. https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create
Requests run one at a time; HTTP 429 (rate or concurrency limit) and 5xx are retried with backoff (Retry-After when
the server sends it), other errors fail with the server's message.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

import settings as cfg

ENGINES = ("elevenlabs", "openai")
KEY_ENV = {"elevenlabs": "ELEVENLABS_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_BASE = {"elevenlabs": "https://api.elevenlabs.io", "openai": "https://api.openai.com/v1"}
OPENAI_FORMAT = "flac"                      # lossless, a complete file (the API's wav header may carry no length)
ELEVEN_PREFIXES = ("mp3_", "wav_", "pcm_")  # codec_samplerate[_bitrate]; pcm_* is raw s16le, wrapped as WAV here
TIMEOUT_S = 120
TRIES = 5                                   # per request, for 429 / 5xx / connection errors
BACKOFF_MAX_S = 60


class CloudError(Exception):
    pass


def key_env(engine: str) -> str:
    return KEY_ENV[engine]


def api_key(engine: str) -> str | None:
    return os.environ.get(KEY_ENV[engine]) or None


def _base(engine: str) -> str:
    return (cfg.get(f"tts.{engine}.base_url") or DEFAULT_BASE[engine]).rstrip("/")


def problems(engine: str) -> list[str]:
    """What keeps `engine` from running, as messages (empty = ready). Checks only local configuration."""
    out = []
    if not api_key(engine):
        out.append(f"{KEY_ENV[engine]} is not set in the environment (API keys never go in settings files)")
    if engine == "elevenlabs":
        if not cfg.get("tts.elevenlabs.voice_id"):
            out.append("tts.elevenlabs.voice_id is not set (settings.local.json): a voice from your ElevenLabs "
                       "Voices, or `narrate.py clone-voice --sample FILE` for a clone of your own voice")
        fmt = cfg.get("tts.elevenlabs.output_format")
        if fmt and not fmt.startswith(ELEVEN_PREFIXES):
            out.append(f"tts.elevenlabs.output_format {fmt!r} is not mp3_*, wav_* or pcm_*")
    elif not cfg.get("tts.openai.voice"):
        out.append("tts.openai.voice is not set (settings.local.json)")
    return out


def _post(url: str, headers: dict, body: bytes, what: str) -> bytes:
    for attempt in range(1, TRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code != 429 and e.code < 500 or attempt == TRIES:
                raise CloudError(f"{what}: HTTP {e.code}: {detail}")
            retry_after = e.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2.0 ** attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == TRIES:
                raise CloudError(f"{what}: {getattr(e, 'reason', e)}")
            wait = 2.0 ** attempt
        time.sleep(min(wait, BACKOFF_MAX_S))
    raise AssertionError("unreachable")


def _write(out: Path, data: bytes) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


def _named(out_stem: Path, ext: str) -> Path:
    """out_stem + '.' + ext (not with_suffix: a stem like '1.2_raw' already holds a dot)."""
    return out_stem.with_name(f"{out_stem.name}.{ext}")


def _need_key(engine: str) -> str:
    key = api_key(engine)
    if not key:
        raise CloudError(f"{KEY_ENV[engine]} is not set in the environment")
    return key


def elevenlabs_speech(text: str, out_stem: Path, seed: int | None = None, previous_text: str | None = None,
                      next_text: str | None = None) -> Path:
    voice = cfg.get("tts.elevenlabs.voice_id")
    if not voice:
        raise CloudError("tts.elevenlabs.voice_id is not set (settings.local.json)")
    fmt = cfg.get("tts.elevenlabs.output_format") or "mp3_44100_128"
    if not fmt.startswith(ELEVEN_PREFIXES):
        raise CloudError(f"tts.elevenlabs.output_format {fmt!r} is not mp3_*, wav_* or pcm_*")
    body = {"text": text}
    model = cfg.get("tts.elevenlabs.model_id")
    if model:
        body["model_id"] = model
    if seed is not None:
        body["seed"] = seed
    if previous_text:
        body["previous_text"] = previous_text
    if next_text:
        body["next_text"] = next_text
    url = (f"{_base('elevenlabs')}/v1/text-to-speech/{urllib.parse.quote(voice, safe='')}"
           f"?{urllib.parse.urlencode({'output_format': fmt})}")
    data = _post(url, {"xi-api-key": _need_key("elevenlabs"), "Content-Type": "application/json",
                       "Accept": "audio/*"},
                 json.dumps(body).encode("utf-8"), "ElevenLabs text-to-speech")
    if fmt.startswith("pcm_"):   # raw 16-bit little-endian mono at the named rate
        out = _named(out_stem, "wav")
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(fmt.split("_")[1]))
            w.writeframes(data)
        return out
    return _write(_named(out_stem, fmt.split("_")[0]), data)


def openai_speech(text: str, out_stem: Path) -> Path:
    voice = cfg.get("tts.openai.voice")
    if not voice:
        raise CloudError("tts.openai.voice is not set (settings.local.json)")
    body = {"model": cfg.get("tts.openai.model") or "gpt-4o-mini-tts", "voice": voice, "input": text,
            "response_format": OPENAI_FORMAT}
    if cfg.get("tts.openai.instructions"):
        body["instructions"] = cfg.get("tts.openai.instructions")
    data = _post(f"{_base('openai')}/audio/speech",
                 {"Authorization": f"Bearer {_need_key('openai')}", "Content-Type": "application/json"},
                 json.dumps(body).encode("utf-8"), "OpenAI audio/speech")
    return _write(_named(out_stem, OPENAI_FORMAT), data)


def speak(engine: str, text: str, out_stem: Path, seed: int | None = None, previous_text: str | None = None,
          next_text: str | None = None) -> Path:
    """One take of `text` into out_stem.<ext>; returns the file. The seed and neighbouring sentences go to
    ElevenLabs only (OpenAI's speech endpoint takes neither)."""
    if engine == "elevenlabs":
        return elevenlabs_speech(text, out_stem, seed, previous_text, next_text)
    if engine == "openai":
        return openai_speech(text, out_stem)
    raise CloudError(f"unknown cloud TTS engine {engine!r}")


def clone_voice(sample: Path, name: str, remove_background_noise: bool = False) -> dict:
    """ElevenLabs Instant Voice Cloning: upload `sample` (1-2 minutes of clean speech works well) as a new voice.
    Returns the API's {"voice_id", "requires_verification"}."""
    boundary = uuid.uuid4().hex
    parts = [("name", None, name.encode("utf-8"))]
    if remove_background_noise:
        parts.append(("remove_background_noise", None, b"true"))
    parts.append(("files", sample.name, sample.read_bytes()))
    body = b""
    for field, filename, content in parts:
        disp = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{boundary}\r\nContent-Disposition: {disp}\r\n".encode("utf-8")
        if filename:
            body += b"Content-Type: application/octet-stream\r\n"
        body += b"\r\n" + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    data = _post(f"{_base('elevenlabs')}/v1/voices/add",
                 {"xi-api-key": _need_key("elevenlabs"), "Content-Type": f"multipart/form-data; boundary={boundary}"},
                 body, "ElevenLabs voices/add")
    try:
        out = json.loads(data)
    except ValueError:
        raise CloudError(f"ElevenLabs voices/add: unexpected reply {data[:200]!r}")
    if not out.get("voice_id"):
        raise CloudError(f"ElevenLabs voices/add: no voice_id in {out}")
    return out
