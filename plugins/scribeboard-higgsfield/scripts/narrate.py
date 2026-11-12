#!/usr/bin/env python
r"""narrate.py - Scribeboard narration: one take per segment (sentence), checked by a transcript, joined into one
narration track with timecodes. Who speaks is settings narration.engine:
  qwen-clone  local Qwen3-TTS voice clone (tts.qwen_*), up to 3 seeded takes, inside a GPU-queue lease
  elevenlabs  ElevenLabs TTS, voice tts.elevenlabs.voice_id (key: env ELEVENLABS_API_KEY), up to 3 seeded takes
  openai      OpenAI TTS, preset voice tts.openai.voice (key: env OPENAI_API_KEY), up to 3 takes
  recorded    the user reads narration/reading.md into ONE file, narration/recording.<wav|mp3|m4a|flac>; faster-whisper
              word timestamps cut it into sentences, each checked like a take. A sentence that fails is re-recorded
              alone as narration/retake_<id>.<ext>, which then wins over the recording for that sentence.
  higgsfield  Higgsfield generate_audio in the narrator voice chosen on the run's setup page (RUN/setup.json,
              scripts/runsetup.py; never a settings value) through the agent's MCP connection
              (scripts/hf.py): `tts-plan` prints one request per sentence that needs a take, the agent submits them
              (quote first), `tts-ingest` records the job ids and later downloads and checks each take like any
              other engine's (up to 3 takes per sentence); then `run` builds the track

  doctor   preflight for the selected engine: its settings and their files, keys in the environment, ffmpeg,
           whisper model, presence EQ; qwen-clone also the voice synth + reference and the GPU queue
  run      make every new/changed segment (qwen-clone: ONE GPU-queue lease, or directly when settings gpu_queue
           is null), then always rebuild narration/narration.wav + .mp3 and alignment.json
  status   per-segment state (ready / changed / missing), whether alignment.json is current, and the next step
  reading  (recorded) write narration/reading.md, the numbered sentences to read aloud
  tts-plan   --run RUN                  (higgsfield) the generate_audio requests for every sentence needing a take
  tts-ingest --run RUN --results FILE   (higgsfield) FILE = [{"id", "job_id", "quoted"}] right after submitting,
                                        then [{"id", "url"}] (or "failed": reason / "unknown": note) once finished
  clone-voice --sample FILE [--name NAME]   ElevenLabs Instant Voice Cloning: prints the new voice id for
           settings tts.elevenlabs.voice_id

Internal subcommands (not for direct use):
  _worker  runs as the lease payload (or directly): synth -> trim -> transcribe -> check, up to 3 takes
  _tts     runs in the qwen-tts venv: batch synth through qwen_tts_synth.main() (settings tts.qwen_synth)
           with the model loaded once
  _asr     runs in the whisperx venv: batch transcription with faster-whisper (WhisperX's ASR engine)
  _words   runs in the whisperx venv: word timestamps of a whole recording (recorded)

Machine paths come from settings.json / settings.local.json (scripts/settings.py): narration.engine, tts.*,
asr.whisper_python, gpu_queue, powershell, python, pronunciations. Cloud requests: scripts/tts_cloud.py.
Contract: .orchestrator/contracts/run-folder.md, section "Narration".
Files in RUN:
  pronunciations.json          (optional) how to say specific words, see load_pronunciations; settings
                               `pronunciations` may name a user-wide file of the same format
  narration/segment_<id>.wav   trimmed take, 48 kHz mono PCM16, raw (no EQ/loudnorm)
  narration/narration.wav      48 kHz mono, presence EQ (when set) + one loudnorm pass
  narration/narration.mp3      192 kbps, for the gate player
  narration/state.json         narrate.py's own cache: text_sha256, spoken_sha256, transcript, wer, pace, takes per
                               segment
  narration/reading.md         (recorded) the reading script; recording.<ext> and retake_<id>.<ext> are the user's
  alignment.json               per contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import hf                # scripts/hf.py: the Higgsfield handoff (narration.engine 'higgsfield')
import runsetup          # scripts/runsetup.py: the run's setup page answers (the narrator voice)
import settings as cfg  # scripts/settings.py: python, narration.engine, tts.*, asr.*, gpu_queue, powershell
import tts_cloud         # scripts/tts_cloud.py: ElevenLabs / OpenAI requests (stdlib only)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

# ------------------------------------------------------------------ constants

PY = cfg.python()
ENGINES = ("qwen-clone", "elevenlabs", "openai", "recorded", "higgsfield")
ENGINE_KEYS = {   # the settings each engine reads, in doctor order
    "qwen-clone": ("python", "tts.qwen_python", "tts.qwen_synth", "tts.reference_wav", "tts.reference_text",
                   "tts.presence_eq", "asr.whisper_python", "gpu_queue", "gpu_queue_log", "powershell"),
    "elevenlabs": ("python", "tts.elevenlabs.voice_id", "tts.elevenlabs.model_id", "tts.elevenlabs.output_format",
                   "tts.elevenlabs.base_url", "tts.presence_eq", "asr.whisper_python"),
    "openai": ("python", "tts.openai.model", "tts.openai.voice", "tts.openai.instructions", "tts.openai.base_url",
               "tts.presence_eq", "asr.whisper_python"),
    "recorded": ("python", "tts.presence_eq", "asr.whisper_python"),
    "higgsfield": ("python", "tts.presence_eq", "asr.whisper_python"),
}
LEGACY_VOICE = "qwen-clone"          # state records without a "voice" were made by the Qwen clone
REC_EXTS = (".wav", ".mp3", ".m4a", ".flac")
REC_HEAD_PAD_S = 0.30                # recorded: keep at most this much before a sentence's first word timestamp ...
REC_TAIL_PAD_S = 0.50                # ... and after its last (whisper word ends run early); the trim does the rest
REC_QUIET_DB = 3.0                   # a cut goes in the middle of the longest stretch within 3 dB of the gap's minimum

TTS_MODEL = "1.7B"
SEEDS = (42, 1337, 90210)            # take 1, 2, 3 (a failed take is re-synthesized with the next seed)
MAX_TAKES = len(SEEDS)
MAX_WER = 0.15                       # accept when wer <= MAX_WER ...
MAX_CER_NOSPACE = 0.05               # ... OR the space-free character error rate <= this
PACE_MIN, PACE_MAX = 1.20, 6.00      # words per second of trimmed speech
ASR_MODEL = "medium"                 # faster-whisper, English, CPU int8 (short clips; keeps VRAM for the TTS)

SR = 48000
PAUSE_SEGMENTS_S = 0.35
PAUSE_SCENES_S = 0.9
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=7"

# Silence trim: 10 ms RMS frames; a frame is speech when above max(peak - 45 dB, -60 dBFS).
TRIM_FRAME_S = 0.010
TRIM_REL_DB = 45.0
TRIM_FLOOR_DB = -60.0
TRIM_PAD_HEAD_S = 0.040              # keep soft onsets
TRIM_PAD_TAIL_S = 0.080              # keep trailing consonants/breath decay
# Generated takes (clean_take): Seed Audio can put a lone click or breath before the first word (or after the last),
# set apart by digital silence, and can stop mid-decay. A sound this short, this far from the words and this much
# quieter than the take's peak is dropped; both ends are faded so an abrupt stop does not click.
BURST_MAX_S = 0.30
BURST_GAP_S = 0.12
BURST_QUIETER_DB = 8.0
FADE_IN_S = 0.005
FADE_OUT_S = 0.020
# A complete Seed Audio take ends in digital silence (its last 50 ms measured -83 dBFS or lower); a take cut off
# mid-sound ends louder (-15 to -34 dBFS measured). Such a take is rejected and the sentence is recorded again.
CUTOFF_TAIL_S = 0.050
CUTOFF_DB = -50.0
# After a cut-off take the sentence is asked for again with a throwaway word after it ("<sentence> ... Okay."): Seed
# Audio leaves a pause before the word and the cut falls on the word, so the sentence itself ends cleanly. The take
# is cut at its last pause (drop_pad); the speech check then has to hear the whole sentence and nothing more. Tested
# 2026-11-10 on a sentence cut off three times running: the padded take ended the sentence in -92 dBFS silence.
PAD_SUFFIX = " ... Okay."
PAD_GAP_S = 0.15                     # the pause before the throwaway word is at least this long
PAD_MAX_S = 1.5                      # ...and what follows it (the word) is at most this long
PAD_MAX_CER = 0.03                   # a padded take's transcript must match closely: an extra word cannot pass
                                     # (seen 2026-11-10: "...and the URL. ... Okay." came back "...the URL slash")

SEGMENT_ID = re.compile(r"^(\d+)\.(\d+)$")


class Fail(Exception):
    pass


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Fail(f"missing {path}")
    except json.JSONDecodeError as e:
        raise Fail(f"bad JSON in {path}: {e}")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seg_wav_rel(seg_id: str) -> str:
    return f"narration/segment_{seg_id}.wav"


def engine() -> str:
    """settings narration.engine, checked against ENGINES."""
    e = cfg.get("narration.engine")
    if e not in ENGINES:
        raise Fail(f"{cfg.where('narration.engine')} is {e!r}; expected one of {', '.join(ENGINES)}")
    return e


def voice_key(eng: str, run: Path) -> str:
    """Who speaks a take: a segment made by another voice counts as changed (never mix two voices in one track).
    qwen-clone adds a content hash of each reference file that is set; with none set (the synth's default reference)
    it stays plain "qwen-clone", as in records written before voices were tracked."""
    if eng == "elevenlabs":
        return f"elevenlabs:{cfg.get('tts.elevenlabs.voice_id')}:{cfg.get('tts.elevenlabs.model_id')}"
    if eng == "openai":
        style = cfg.get("tts.openai.instructions")
        return (f"openai:{cfg.get('tts.openai.model')}:{cfg.get('tts.openai.voice')}"
                + (f":{sha256(style)[:16]}" if style else ""))
    if eng == "qwen-clone":
        return ":".join([eng] + [f"{k.split('.')[-1]}={file_sha(tool(k))[:16]}"
                                 for k in ("tts.reference_wav", "tts.reference_text") if cfg.path(k)])
    if eng == "higgsfield":
        try:
            return hf.voice_key(run)          # the narrator chosen on the run's setup page
        except hf.HFError as e:
            raise Fail(str(e)) from e
    return eng


# ------------------------------------------------------------------ script + state

def load_script(path: Path, lex: dict) -> list[dict]:
    """Segments in script order: [{id, scene, text, sha, spoken, spoken_sha}]. spoken = the text with the run's
    pronunciations applied (say_text): what a voice is given and what the transcript check compares against.
    Checks only what narration needs. Before script.json exists (the scribe-higgsfield order: the narration is recorded
    before Gate 1), the run's narration.json stands in: one scene per paragraph, its sentences as the segments, with
    the same ids and texts pass 2 copies into script.json, so every take stays valid."""
    if path.name == "script.json" and not path.is_file() and (path.parent / "narration.json").is_file():
        nar = read_json(path.parent / "narration.json")
        data = {"scenes": [{"id": p.get("id"), "segments": [{"id": g.get("id"), "text": g.get("text")}
                                                            for g in p.get("sentences") or []]}
                           for p in nar.get("paragraphs") or []]}
    else:
        data = read_json(path)
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise Fail(f"{path}: 'scenes' must be a non-empty list")
    segs, seen, prev_scene = [], set(), 0
    for sc in scenes:
        sid = sc.get("id")
        if not isinstance(sid, int) or sid <= prev_scene:
            raise Fail(f"{path}: scene ids must be increasing integers (got {sid!r} after {prev_scene})")
        prev_scene = sid
        ss = sc.get("segments")
        if not isinstance(ss, list) or not ss:
            raise Fail(f"{path}: scene {sid} has no segments")
        for s in ss:
            gid, text = s.get("id"), s.get("text")
            m = SEGMENT_ID.match(gid) if isinstance(gid, str) else None
            if not m or int(m.group(1)) != sid:
                raise Fail(f"{path}: segment id {gid!r} in scene {sid} is not '<scene>.<n>'")
            if gid in seen:
                raise Fail(f"{path}: duplicate segment id {gid}")
            if not isinstance(text, str) or not text.strip():
                raise Fail(f"{path}: segment {gid} has no text")
            seen.add(gid)
            spoken = say_text(text, lex)
            segs.append({"id": gid, "scene": sid, "text": text, "sha": sha256(text),
                         "spoken": spoken, "spoken_sha": sha256(spoken)})
    return segs


def state_path(run: Path) -> Path:
    return run / "narration" / "state.json"


def load_state(run: Path) -> dict:
    p = state_path(run)
    if not p.exists():
        return {"version": 1, "segments": {}}
    st = read_json(p)
    if st.get("version") != 1 or not isinstance(st.get("segments"), dict):
        raise Fail(f"{p}: unknown state format")
    return st


def seg_state(run: Path, seg: dict, st: dict, voice: str) -> str:
    """ready / changed / missing. A take is changed when its text, its spoken form (a pronunciation was added or
    changed) or its voice differs; records from before spoken forms existed count their text as their spoken form."""
    rec = st["segments"].get(seg["id"])
    if rec is None or not (run / seg_wav_rel(seg["id"])).is_file():
        return "missing"
    if (rec.get("text_sha256") != seg["sha"] or rec.get("spoken_sha256", rec.get("text_sha256")) != seg["spoken_sha"]
            or rec.get("voice", LEGACY_VOICE) != voice):
        return "changed"
    if rec.get("retake"):          # a later check rejected this take (narrate.py retrim): it needs a new one
        return "changed"
    return "ready"


# ------------------------------------------------------------------ pronunciations

PRONUNCIATIONS = "pronunciations.json"


def _read_pronunciations(path: Path) -> dict[str, dict]:
    """One pronunciation file: {"Written form": "how to say it"} or {"Written form": {"say": "how to say it",
    "heard": ["how a transcript may spell it", ...]}}; keys starting with "_" are comments.
    Returns {lowercase written form: {"written", "say", "heard"}}."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise Fail(f"{path}: must be a JSON object of \"Written form\": \"how to say it\"")
    out = {}
    for written, e in data.items():
        if written.startswith("_"):
            continue
        if isinstance(e, str):
            e = {"say": e}
        heard = e.get("heard", []) if isinstance(e, dict) else None
        if (not written.strip() or not isinstance(e, dict) or set(e) - {"say", "heard"}
                or not isinstance(e.get("say"), str) or not e["say"].strip() or not isinstance(heard, list)
                or not all(isinstance(h, str) and h.strip() for h in heard)):
            raise Fail(f"{path}: entry {written!r} must be \"how to say it\" or "
                       "{\"say\": \"how to say it\", \"heard\": [\"how a transcript may spell it\"]}")
        out[written.strip().lower()] = {"written": written.strip(), "say": e["say"].strip(),
                                        "heard": [h.strip() for h in heard]}
    return out


def load_pronunciations(run: Path) -> dict[str, dict]:
    """How to say specific words in this run: the user-wide file named by settings `pronunciations` (optional), then
    RUN/pronunciations.json (optional), whose entries win. The voice is given each written form's "say"; the
    transcript check counts the written form, its "say" and every "heard" spelling as that word said right."""
    lex = {}
    if cfg.path("pronunciations") is not None:
        lex.update(_read_pronunciations(tool("pronunciations")))
    if (run / PRONUNCIATIONS).is_file():
        lex.update(_read_pronunciations(run / PRONUNCIATIONS))
    return lex


def _replace_words(text: str, table: dict[str, str]) -> str:
    """Every whole-word, case-insensitive occurrence of a table key (lowercase) replaced by its value, longest key
    first, in one pass (a replacement is never replaced again)."""
    if not table:
        return text
    rx = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, sorted(table, key=len, reverse=True))) + r")(?!\w)",
                    re.IGNORECASE)

    def repl(m: re.Match) -> str:
        hit = m.group(1)
        if hit.lower() in table:
            return table[hit.lower()]
        # a few letters (İ, ſ) match case-insensitively but lowercase to another string
        return next(v for k, v in table.items() if re.fullmatch(re.escape(k), hit, re.IGNORECASE))

    return rx.sub(repl, text)


def say_text(text: str, lex: dict) -> str:
    """The text as the voice says it: every written form replaced by its "say"."""
    return _replace_words(text, {k: e["say"] for k, e in lex.items()})


def heard_text(transcript: str, lex: dict) -> str:
    """A transcript in the same spoken form: each written form and each "heard" spelling replaced by its "say"
    (a written form wins over another entry's "heard")."""
    table = {h.lower(): e["say"] for e in lex.values() for h in e["heard"]}
    table.update({k: e["say"] for k, e in lex.items()})
    return _replace_words(transcript, table)


# ------------------------------------------------------------------ audio helpers (system python)

def run_cmd(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"{what} failed (exit {p.returncode}): {(p.stdout + p.stderr)[-800:]}")
    return p


def decode_48k(path: Path):
    """Any audio file -> float32 mono numpy array at 48 kHz."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"ffmpeg could not decode {path}: {p.stderr.decode('utf-8', 'replace')[-400:]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def write_pcm16(path: Path, x) -> None:
    import numpy as np
    from scipy.io import wavfile
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    tmp = path.with_name(path.stem + ".tmp.wav")
    wavfile.write(str(tmp), SR, pcm)
    os.replace(tmp, path)


def read_pcm16(path: Path):
    import numpy as np
    from scipy.io import wavfile
    sr, x = wavfile.read(str(path))
    if sr != SR or x.dtype != np.int16 or x.ndim != 1:
        raise Fail(f"{path}: expected 48 kHz mono PCM16, got sr={sr} dtype={x.dtype} shape={x.shape}")
    return x.astype(np.float32) / 32767.0


def trim_bounds(x) -> tuple[int, int] | None:
    """Sample range [a, b) of x without leading/trailing silence (energy threshold), or None if silent."""
    import numpy as np
    n = int(round(TRIM_FRAME_S * SR))
    frames = len(x) // n
    if frames == 0:
        return None
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n).astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-10))
    thr = max(float(db.max()) - TRIM_REL_DB, TRIM_FLOOR_DB)
    on = np.nonzero(db > thr)[0]
    if len(on) == 0 or float(db.max()) <= TRIM_FLOOR_DB:
        return None
    a = max(0, on[0] * n - int(round(TRIM_PAD_HEAD_S * SR)))
    b = min(len(x), (on[-1] + 1) * n + int(round(TRIM_PAD_TAIL_S * SR)))
    return int(a), int(b)


def trim_silence(x):
    """Cut leading/trailing silence with an energy threshold. Returns the trimmed array or None if silent."""
    ab = trim_bounds(x)
    return None if ab is None else x[ab[0]:ab[1]]


def cut_off_db(x) -> float | None:
    """The level (dBFS) of a take's last CUTOFF_TAIL_S when it is CUTOFF_DB or louder: the take stops mid-sound.
    None when it ends in silence."""
    import numpy as np
    k = int(round(CUTOFF_TAIL_S * SR))
    if len(x) < k:
        return None
    level = 20.0 * np.log10(max(float(np.sqrt(np.mean(np.asarray(x[-k:], dtype=np.float64) ** 2))), 1e-10))
    return level if level >= CUTOFF_DB else None


def cut_off_reason(take: int, level: float) -> str:
    return (f"take {take}: cut off at the end (the audio stops mid-sound: its last {CUTOFF_TAIL_S * 1000:.0f} ms "
            f"are at {level:.0f} dBFS, a complete take ends below {CUTOFF_DB:.0f})")


def drop_pad(x):
    """A padded take (PAD_SUFFIX) up to the middle of its last pause of PAD_GAP_S or more, which separates the
    sentence from the throwaway word. Returns (audio, None) or (None, why)."""
    import numpy as np
    n = int(round(TRIM_FRAME_S * SR))
    frames = len(x) // n
    if frames < 2:
        return None, "too short"
    rms = np.sqrt(np.mean(np.asarray(x[: frames * n], dtype=np.float64).reshape(frames, n) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-10))
    on = db > max(float(db.max()) - TRIM_REL_DB, TRIM_FLOOR_DB)
    sound = np.nonzero(on)[0]
    if len(sound) == 0:
        return None, "silent"
    last = int(sound[-1])
    gap = max(1, int(round(PAD_GAP_S / TRIM_FRAME_S)))
    i = last
    while i >= 0:                    # walk back from the end: the last silent stretch of `gap` frames or more
        if on[i]:
            i -= 1
            continue
        e = i
        while i >= 0 and not on[i]:
            i -= 1
        s = i + 1
        if e - s + 1 >= gap:
            if s == 0:
                break                # the silence before the first word: no pause between sentence and pad
            if (last + 1 - (e + 1)) * TRIM_FRAME_S > PAD_MAX_S:
                return None, (f"the sound after the last pause lasts {(last + 1 - (e + 1)) * TRIM_FRAME_S:.2f} s, "
                              f"longer than the throwaway word ({PAD_MAX_S} s)")
            return np.asarray(x[: ((s + e + 1) // 2) * n]), None
    return None, "no pause before the throwaway word"


def clean_take(x):
    """A generated take ready to check: silence trimmed (trim_bounds), a lone click or breath before the first word
    or after the last dropped (BURST_*), both ends faded (FADE_*). Returns (audio, what was dropped) or (None, [])
    if silent. The ASR check that follows still has to hear every word, so a dropped word cannot pass unseen."""
    import numpy as np
    ab = trim_bounds(x)
    if ab is None:
        return None, []
    y = x[ab[0]:ab[1]]
    n = int(round(TRIM_FRAME_S * SR))
    frames = len(y) // n
    dropped: list[str] = []
    if frames >= 2:
        rms = np.sqrt(np.mean(y[: frames * n].reshape(frames, n).astype(np.float64) ** 2, axis=1))
        db = 20.0 * np.log10(np.maximum(rms, 1e-10))
        peak = float(db.max())
        on = db > max(peak - TRIM_REL_DB, TRIM_FLOOR_DB)
        gap = max(1, int(round(BURST_GAP_S / TRIM_FRAME_S)))
        runs, i = [], 0                      # runs of sound frames [s, e), split at silences >= BURST_GAP_S
        while i < frames:
            if not on[i]:
                i += 1
                continue
            s = i
            quiet = 0
            while i < frames and quiet < gap:
                quiet = 0 if on[i] else quiet + 1
                i += 1
            runs.append([s, i - quiet])
        def burst(r) -> bool:
            return ((r[1] - r[0]) * TRIM_FRAME_S <= BURST_MAX_S
                    and float(db[r[0]:r[1]].max()) <= peak - BURST_QUIETER_DB)
        while len(runs) > 1 and burst(runs[0]):
            dropped.append(f"head {runs[0][0] * TRIM_FRAME_S:.2f}-{runs[0][1] * TRIM_FRAME_S:.2f} s")
            runs.pop(0)
        while len(runs) > 1 and burst(runs[-1]):
            dropped.append(f"tail {runs[-1][0] * TRIM_FRAME_S:.2f}-{runs[-1][1] * TRIM_FRAME_S:.2f} s")
            runs.pop()
        if dropped:
            a = max(0, runs[0][0] * n - int(round(TRIM_PAD_HEAD_S * SR)))
            b = min(len(y), runs[-1][1] * n + int(round(TRIM_PAD_TAIL_S * SR)))
            y = y[a:b]
    y = np.array(y, dtype=np.float32)
    fi, fo = min(len(y), int(round(FADE_IN_S * SR))), min(len(y), int(round(FADE_OUT_S * SR)))
    if fi:
        y[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)
    if fo:
        y[len(y) - fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)
    return y, dropped


def tool(key: str) -> Path:
    """A path setting narration needs (tts.*, asr.*): Fail when it is not set or does not exist."""
    p = cfg.path(key)
    if p is None:
        raise Fail(f"{key} is not set in settings (settings.local.json)")
    if not p.is_file():
        raise Fail(f"{p} ({cfg.where(key)}) does not exist")
    return p


def presence_eq() -> str | None:
    """PRESENCE_EQ exactly as the presence-eq.sh in settings tts.presence_eq defines it (single source);
    None when tts.presence_eq is not set (the track then gets loudnorm only)."""
    if cfg.path("tts.presence_eq") is None:
        return None
    sh = tool("tts.presence_eq")
    found = re.findall(r'^PRESENCE_EQ="([^"$`\\]+)"\s*$', sh.read_text(encoding="utf-8"), re.M)
    if len(found) != 1:
        raise Fail(f"{sh}: expected exactly one literal PRESENCE_EQ=\"...\" assignment, found {len(found)}")
    return found[0]


# ------------------------------------------------------------------ transcript check

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
            "nine": "ninth", "twelve": "twelfth"}
NUM_VARIANTS_MAX = 32                 # cap on year/cardinal reading combinations per sentence


def _under_100(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])


def cardinal(n: int) -> list[str]:
    """Standard English cardinal, no 'and': 1500 -> one thousand five hundred."""
    if n < 100:
        return _under_100(n)
    for size, name in _SCALES:
        if n >= size:
            rest = n % size
            return cardinal(n // size) + [name] + (cardinal(rest) if rest else [])
    rest = n % 100
    return [_ONES[n // 100], "hundred"] + (_under_100(rest) if rest else [])


def year_reading(n: int) -> list[str] | None:
    """Spoken year for 1100-2099: 2022 -> twenty twenty two, 1905 -> nineteen oh five,
    1900 -> nineteen hundred, 2000-2009 -> two thousand (x). None outside that range."""
    if not 1100 <= n <= 2099:
        return None
    if 2000 <= n <= 2009:
        return cardinal(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return _under_100(hi) + ["hundred"]
    return _under_100(hi) + (["oh", _ONES[lo]] if lo < 10 else _under_100(lo))


def ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    last = _ORDINAL.get(last, last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return words[:-1] + [last]


def _number_readings(tok: str) -> list[list[str]]:
    """All accepted spoken readings of a digit token (first = preferred)."""
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", tok)
    if m:
        return [ordinal(cardinal(int(m.group(1))))]
    m = re.fullmatch(r"(\d+\.\d+)s", tok)                    # 2.0s -> two point zero s (as before)
    if m:
        return [r + ["s"] for r in _number_readings(m.group(1))]
    m = re.fullmatch(r"(\d*0)s", tok)                        # 90s -> nineties, 1990s -> nineteen nineties
    if m:
        base = _number_readings(m.group(1))
        plurals = [r[:-1] + [r[-1][:-1] + "ies" if r[-1].endswith("y") else r[-1] + "s"] for r in base]
        plurals += [p[1:] for p in plurals if len(p) == 2 and p[0] == "one"]   # 100s -> hundreds
        return plurals + [r + ["s"] for r in base]                          # old "ninety s" kept last
    m = re.fullmatch(r"(\d+)\.(\d+)", tok)
    if m:
        out = [cardinal(int(m.group(1))) + ["point"] + [_ONES[int(d)] for d in m.group(2)]]
        if m.group(1).strip("0") == "":                       # 0.5 -> point five as well as zero point five
            out.append(out[0][1:])
        h, mm = int(m.group(1)), m.group(2)
        if 1 <= h <= 12 and len(mm) == 2 and int(mm) < 60:   # ASR writes "two forty" (a clock time) as 2.40
            mins = int(mm)
            out.append(cardinal(h) + ([] if mins == 0 else ["oh", _ONES[mins]] if mins < 10 else _under_100(mins)))
        return out
    n = int(tok)
    readings = [cardinal(n)]
    year = year_reading(n)
    if year is not None and year != cardinal(n):
        readings.insert(0, year)
    if 1100 <= n <= 9999 and n % 100 == 0 and n % 1000 and year is None:   # 7900 -> seventy nine hundred
        readings.append(_under_100(n // 100) + ["hundred"])
    return readings


_CONTRACTION_WORDS = {"can't": "can not", "won't": "will not", "shan't": "shall not", "ain't": "aint",
                      "cannot": "can not", "let's": "let us"}
_CONTRACTION_ENDS = {"ve": "have", "re": "are", "ll": "will", "m": "am", "d": "would"}
_IS_WORDS = "it that there here what who he she where how when why"


def expand_contractions(t: str) -> str:
    """One spoken form for contractions on both sides of the check (would've = would have, don't = do not), so a
    voice or a transcript that expands or contracts a word costs nothing. t is lowercase with straight apostrophes.
    An ambiguous ending gets one fixed reading ('d -> would, "it's" -> it is); any other 's (a possessive) stays."""
    t = re.sub(r"\b(" + "|".join(map(re.escape, _CONTRACTION_WORDS)) + r")\b",
               lambda m: _CONTRACTION_WORDS[m.group(1)], t)
    t = re.sub(r"\b(\w+)n't\b", r"\1 not", t)
    t = re.sub(r"\b(\w+)'(ve|re|ll|m|d)\b", lambda m: f"{m.group(1)} {_CONTRACTION_ENDS[m.group(2)]}", t)
    return re.sub(r"\b(" + _IS_WORDS.replace(" ", "|") + r")'s\b", r"\1 is", t)


def norm_variants(text: str, contractions: bool = True) -> list[list[str]]:
    """Normalized word lists for the check: lowercase, punctuation stripped, hyphens/dashes/slashes
    split words, contractions expanded (would've -> would have; contractions=False only drops their apostrophe),
    other apostrophes dropped, digits spelled out. A number that reads both as a year and as a cardinal (1100-2099)
    yields both readings; the first variant uses the year reading everywhere. Decades (90s, 1990s) read as plurals
    (nineties, nineteen nineties); 0.5 also reads "point five"; 7900 also reads "seventy nine hundred"; "dollars" and
    "bucks" read as "dollar" (ASR turns "ten and one thousand dollar versions" into "$10 and $1,000 versions", and
    "seventy-nine hundred bucks" into "$7,900")."""
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)               # 1,000 -> 1000
    t = re.sub(r"\$(\d+(?:\.\d+)?)", r"\1 dollars", t)       # $5 -> 5 dollars
    t = t.replace("%", " percent ")
    t = re.sub(r"[\u2010-\u2015\-/]", " ", t)                # hyphens, dashes, slashes separate words
    if contractions:
        t = expand_contractions(t)                           # would've -> would have, don't -> do not
    t = t.replace("'", "")                                   # other apostrophes: john's -> johns
    t = re.sub(r"(\d)\.(\d)", r"\1<pt>\2", t)                # keep decimal points through the strip
    t = re.sub(r"[^a-z0-9<>\s]", " ", t).replace("<pt>", ".")
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])(?!(?:st|nd|rd|th)\b)(?!(?<=0)s\b)", " ", t)   # gpt4 -> gpt 4, 90s kept
    variants: list[list[str]] = [[]]
    for tok in t.split():
        if not tok[0].isdigit():
            if tok in ("dollars", "bucks", "buck"):   # ASR writes "$10" for "ten dollars" and "ten bucks" alike
                tok = "dollar"
            for v in variants:
                v.append(tok)
            continue
        readings = _number_readings(tok)
        if len(variants) * len(readings) > NUM_VARIANTS_MAX:
            readings = readings[:1]
        variants = [v + r for v in variants for r in readings]
    return variants


def norm_words(text: str) -> list[str]:
    return norm_variants(text)[0]


def edit_distance(ref, hyp) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def transcript_scores(text: str, transcript: str) -> tuple[float, float]:
    """(wer, cer_nospace), each the best over the spoken-form readings of both sides, with contractions expanded and
    also with them only stripped of their apostrophe (so "we're" heard as "were" costs nothing, as before).
    wer: word error rate on normalized words. cer_nospace: character error rate on the normalized
    words joined with no spaces, so word-splitting differences (chat gpt / chatgpt) cost nothing."""
    def readings(s: str) -> list[list[str]]:
        out = []
        for v in norm_variants(s) + norm_variants(s, contractions=False):
            if v not in out:
                out.append(v)
        return out

    refs, hyps = readings(text), readings(transcript)
    if not refs[0]:
        raise Fail(f"transcript check: script text has no words: {text!r}")
    best_w = best_c = float("inf")
    for r in refs:
        rc = "".join(r)
        for h in hyps:
            best_w = min(best_w, edit_distance(r, h) / len(r))
            best_c = min(best_c, edit_distance(rc, "".join(h)) / len(rc))
    return best_w, best_c


def check_take(spoken: str, transcript: str, samples: int, lex: dict) -> dict:
    """The transcript check for one take against its spoken text (say_text): accept when (wer <= 0.15 or
    cer_nospace <= 0.05) and the pace (spoken words per second of trimmed speech) is within 1.20-6.00. The transcript
    first gets the pronunciations (heard_text), so a word heard as written, as said or as a listed spelling counts."""
    lex = {k: e for k, e in lex.items()   # only words this sentence says: a "heard" spelling elsewhere is a real word
           if _replace_words(spoken, {e["say"].lower(): "\x00"}) != spoken}
    w, c = transcript_scores(spoken, heard_text(transcript, lex))
    pace = len(norm_words(spoken)) / (samples / SR)
    ok = (w <= MAX_WER or c <= MAX_CER_NOSPACE) and PACE_MIN <= pace <= PACE_MAX
    return {"wer": round(w, 3), "cer_nospace": round(c, 3), "pace_wps": round(pace, 2), "ok": ok}


def tts_text(text: str) -> str:
    """TTS text prep: dashes -> commas (the TTS spells out bare hyphens otherwise)."""
    return text.replace("\u2014", ", ").replace("\u2013", ", ").replace(" - ", ", ")


# ------------------------------------------------------------------ worker (lease payload)

def transcribe(items: list[dict], work: Path, label: str) -> dict:
    """{id: transcript} for [{id, wav}], one faster-whisper batch in the whisper venv (asr.whisper_python)."""
    if not items:
        return {}
    batch, out = work / "asr.json", work / "asr_result.json"
    write_json(batch, {"items": items})
    say(f"{label}: transcribing {len(items)} segment(s)")
    p = subprocess.run([str(tool("asr.whisper_python")), str(Path(__file__).resolve()), "_asr", "--batch", str(batch),
                        "--out", str(out)], stdout=sys.stderr, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"ASR batch failed (exit {p.returncode}) on {label}")
    return read_json(out)


def synthesize(eng: str, pending: list[dict], takes_dir: Path, take: int, seed: int | None) -> dict[str, Path]:
    """One take of every pending segment: {id: raw audio file}."""
    if eng in tts_cloud.ENGINES:
        say(f"take {take}{f' (seed {seed})' if seed is not None else ''}: {eng} TTS for {len(pending)} segment(s)")
        out = {}
        for i, seg in enumerate(pending, 1):
            say(f"  tts [{i}/{len(pending)}] {seg['id']}")
            try:
                out[seg["id"]] = tts_cloud.speak(eng, seg["spoken"], takes_dir / f"{seg['id']}_raw", seed,
                                                 seg.get("prev_text"), seg.get("next_text"))
            except tts_cloud.CloudError as e:
                raise Fail(f"segment {seg['id']}, take {take}: {e}")
        return out
    items = []
    for seg in pending:
        tf = takes_dir / f"{seg['id']}.txt"
        tf.write_text(tts_text(seg["spoken"]), encoding="utf-8")
        items.append({"id": seg["id"], "text_file": str(tf), "out": str(takes_dir / f"{seg['id']}_raw.wav"),
                      "seed": seed})
    tts_batch = takes_dir / "tts.json"
    write_json(tts_batch, {"items": items})
    say(f"take {take} (seed {seed}): synthesizing {len(items)} segment(s)")
    p = subprocess.run([str(tool("tts.qwen_python")), str(Path(__file__).resolve()), "_tts", "--batch", str(tts_batch)],
                       stdout=sys.stderr, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"TTS batch failed (exit {p.returncode}) on take {take}")
    return {it["id"]: Path(it["out"]) for it in items}


def cmd_worker(args) -> None:
    job_path = Path(args.job)
    job = read_json(job_path)
    run = Path(job["run"])
    eng, voice, lex = job["engine"], job["voice"], job["pronunciations"]
    work = job_path.parent
    pending = job["segments"]
    say(f"narrate worker: {len(pending)} segment(s) to synthesize")
    st = load_state(run)
    failures: dict[str, str] = {}
    for take in range(1, MAX_TAKES + 1):
        if not pending:
            break
        seed = None if eng == "openai" else SEEDS[take - 1]   # OpenAI's speech endpoint takes no seed
        takes_dir = work / f"take{take}"
        takes_dir.mkdir(parents=True, exist_ok=True)
        raws = synthesize(eng, pending, takes_dir, take, seed)
        pending = check_takes(run, st, pending, raws, take, takes_dir, lex, voice, seed, failures)

    if pending:
        detail = "; ".join(f"{s['id']} ({failures.get(s['id'], 'unknown')})" for s in pending)
        raise Fail(f"segment(s) failed the transcript check after {MAX_TAKES} takes: {detail}")
    shutil.rmtree(work, ignore_errors=True)
    say("narrate worker: all segments passed")


def check_takes(run: Path, st: dict, pending: list[dict], raws: dict[str, Path], take: int, takes_dir: Path, lex: dict,
                voice: str, seed: int | None, failures: dict[str, str],
                padded: frozenset = frozenset()) -> list[dict]:
    """Trim, transcribe and check one take of every pending segment; a passing take goes into state (and its
    trimmed audio to narration/segment_<id>.wav). Returns the segments that still need a take. A take in `padded`
    was asked for with PAD_SUFFIX: its throwaway word is cut off first (drop_pad)."""
    def prepare(seg: dict, x, suffix: str) -> tuple[Path, int] | None:
        level = cut_off_db(x) if engine() == "higgsfield" else None
        if level is not None:          # Seed Audio stopped mid-sound: no cleaning brings the lost sound back
            failures[seg["id"]] = cut_off_reason(take, level)
            say(f"  {seg['id']}: take {take} REJECT | {failures[seg['id']]}")
            return None
        y, dropped = clean_take(x)
        if y is None:
            failures[seg["id"]] = f"take {take}: silent output"
            return None
        if dropped:
            say(f"  {seg['id']}: take {take}: dropped a lone click/breath ({', '.join(dropped)})")
        tp = takes_dir / f"{seg['id']}_trim{suffix}.wav"
        write_pcm16(tp, y)
        return tp, len(y)

    def judge(seg: dict, tp: Path, n: int, hyp: str | None, pad_rule: bool) -> bool:
        if hyp is None:
            raise Fail(f"ASR returned no transcript for segment {seg['id']}")
        chk = check_take(seg["spoken"], hyp, n, lex)
        ok = chk["ok"]
        nums = f"wer={chk['wer']:.3f} cer_nospace={chk['cer_nospace']:.3f} pace={chk['pace_wps']:.2f} w/s"
        if ok and pad_rule and chk["cer_nospace"] > PAD_MAX_CER:
            ok = False
            nums += f" (padded take: cer_nospace above {PAD_MAX_CER}, an extra or missing word)"
        say(f"  {seg['id']}: take {take} {nums} {'OK' if ok else 'REJECT'} | {hyp.strip()}")
        if ok:
            seg["_chk"], seg["_hyp"] = chk, hyp
        else:
            failures[seg["id"]] = f"take {take}: {nums} transcript={hyp.strip()!r}"
        return ok

    asr_items, trimmed, full = [], {}, {}
    for seg in pending:
        raw = raws[seg["id"]]
        if not raw.is_file():
            raise Fail(f"TTS produced no file for segment {seg['id']}: {raw}")
        x = decode_48k(raw)
        if seg["id"] in padded:
            full[seg["id"]] = x        # the voice may skip the throwaway word: then the whole take is the sentence
            x, why = drop_pad(x)
            if x is None:
                failures[seg["id"]] = f"take {take}: padded take: {why}"
                say(f"  {seg['id']}: take {take} REJECT | {failures[seg['id']]}")
                continue
        got = prepare(seg, x, "")
        if got is None:
            continue
        trimmed[seg["id"]] = got
        asr_items.append({"id": seg["id"], "wav": str(got[0])})
    transcripts = transcribe(asr_items, takes_dir, f"take {take}")

    passed = {seg["id"] for seg in pending if seg["id"] in trimmed
              and judge(seg, *trimmed[seg["id"]], transcripts.get(seg["id"]), seg["id"] in padded)}
    # a padded take whose cut lost words (or found no pause): the voice may have left out the throwaway word, so the
    # uncut take is checked as an ordinary take (a cut-off end and the transcript check still apply)
    retry = [seg for seg in pending if seg["id"] in padded and seg["id"] not in passed]
    if retry:
        items2 = []
        for seg in retry:
            got = prepare(seg, full[seg["id"]], "_whole")
            if got is not None:
                trimmed[seg["id"]] = got
                items2.append({"id": seg["id"], "wav": str(got[0])})
        if items2:
            say(f"  padded take(s) {', '.join(i['id'] for i in items2)}: checking the uncut take (the throwaway "
                f"word may be missing)")
            t2 = transcribe(items2, takes_dir, f"take {take} uncut")
            passed |= {i["id"] for i in items2
                       if judge(next(s for s in retry if s["id"] == i["id"]), *trimmed[i["id"]], t2.get(i["id"]),
                                False)}

    still = []
    for seg in pending:
        if seg["id"] not in passed:
            still.append(seg)
            continue
        tp, n = trimmed[seg["id"]]
        hyp, chk = seg.pop("_hyp"), seg.pop("_chk")
        dst = run / seg_wav_rel(seg["id"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tp, dst)
        st["segments"][seg["id"]] = {"text": seg["text"], "text_sha256": seg["sha"],
                                     "spoken_sha256": seg["spoken_sha"], "wav": seg_wav_rel(seg["id"]),
                                     "samples": int(n), "transcript": hyp.strip(), "wer": chk["wer"],
                                     "cer_nospace": chk["cer_nospace"], "pace_wps": chk["pace_wps"],
                                     "takes": take, "seed": seed, "voice": voice,
                                     **({"padded": True} if seg["id"] in padded
                                        and not tp.name.endswith("_whole.wav") else {})}
        failures.pop(seg["id"], None)
        write_json(state_path(run), st)
    return still


# ------------------------------------------------------------------ Higgsfield handoff (narration.engine 'higgsfield')

def hf_pending(run: Path, script: Path):
    lex = load_pronunciations(run)
    segs = load_script(script, lex)
    if engine() != "higgsfield":
        raise Fail(f"{cfg.where('narration.engine')} is {engine()!r}, not 'higgsfield'")
    voice = voice_key("higgsfield", run)
    st = load_state(run)
    return lex, segs, voice, st, [s for s in segs if seg_state(run, s, st, voice) != "ready"]


def take_ref(gid: str, rec: dict) -> str:
    """The ledger ref of a take: narration/<id>/t<n>, n unique per sentence for ever (older records: the take number)."""
    return f"narration/{gid}/t{rec.get('n') or rec['take']}"


def text_approved_for_takes(run: Path, script: Path) -> None:
    """Setup runs: paid takes only for the run's own text that passed its check. The script is the run's own
    script.json (or, before it exists, narration.json); "keep my words" text matches the user's script word for word;
    rewritten text passed (or was flagged by) its voice review; a script.json carries exactly the narration's
    sentences. Raises Fail otherwise."""
    if not runsetup.required():
        return
    if script != run / "script.json":
        raise Fail(f"tts-plan: only the run's own script ({run / 'script.json'}) is recorded, not {script}")
    try:
        runsetup.require_start(run)                         # the start page first: the voice and the budget
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    import voice as vo   # imported here: voice.py is needed only for this check
    nar_p = run / "narration.json"
    if not nar_p.is_file():
        raise Fail("tts-plan: the run has no narration.json yet (writer pass 1 comes first)")
    nar = read_json(nar_p)
    try:
        vo.validate_narration(nar, run)                     # with keep my words: the word-for-word check
        parts = vo.fingerprint_parts(nar)
        if not vo.keep_words(run):
            status = vo.status_of(vo.load(run), vo.fingerprint(parts))
            if status not in ("passed", "flagged"):
                raise Fail(f"tts-plan: the narration's voice review is '{status}'; no take is recorded before it "
                           f"passes (scribe.py status)")
        if script.is_file():
            sp = read_json(script)
            have = [[g["id"], g["text"]] for sc in sp.get("scenes", []) for g in sc.get("segments", [])]
            want = [[g["id"], g["text"]] for p in nar.get("paragraphs", []) for g in p.get("sentences", [])]
            if have != want:
                raise Fail("tts-plan: script.json's sentences are not exactly narration.json's (ids and text, "
                           "spaces included); rebuild script.json from the approved narration")
    except vo.Fail as e:
        raise Fail(f"tts-plan: {e}") from e


def cmd_tts_plan(args) -> None:
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    text_approved_for_takes(run, script)
    lex, segs, voice, st, pending = hf_pending(run, script)
    hfst = st.setdefault("hf", {})
    items, in_flight, failed, plan = [], [], [], {}
    jobs = hf.ledger(run)["jobs"]
    paid = {}
    for j in jobs:                                       # the newest paid line per ref
        if j["state"] in ("submitted", "unknown"):
            paid[j["ref"]] = j
    reserved = {j["ref"] for j in jobs if j["state"] == "reserved"}
    submitting = {j["ref"]: j for j in jobs if j["state"] == "submitting" and not j.get("job_id")}
    for s in pending:
        rec = hfst.get(s["id"])
        if rec and rec["state"] == "requested" and take_ref(s["id"], rec) in submitting:
            # write-ahead: its submit started and no job was recorded; never planned or submitted again
            in_flight.append({"id": s["id"], "state": "submitting", "ref": take_ref(s["id"], rec),
                              "next": hf.pending_note(run, take_ref(s["id"], rec))})
            continue
        if rec and rec.get("spoken_sha") != s["spoken_sha"]:
            if rec["state"] == "requested":              # its take was planned but never submitted: release it, so
                hf.ledger_release(run, take_ref(s["id"], rec))   # the new take holds the only reservation
            rec = None                                   # the sentence (or how a word is said) changed: start over
        if rec and rec["state"] == "requested":          # the ledger already has this take's job (a crash between the
            ref = take_ref(s["id"], rec)                 # ledger and this state): never plan it again. An open
            j = paid.get(ref) if ref not in reserved else None   # reservation means its job was never recorded
            if j:
                rec.update(state=j["state"], job_id=j.get("job_id"), quoted=j.get("quoted"))
                write_json(state_path(run), st)   # saved at once: a later refusal in this call must not undo it
        if rec and rec["state"] in ("submitted", "unknown"):
            in_flight.append({"id": s["id"], "state": rec["state"], "job_id": rec.get("job_id")})
            continue
        take = rec["take"] + 1 if rec and rec["state"] == "rejected" else rec["take"] if rec else 1
        fails = rec.get("failures", []) if rec else []
        last_cut = bool(fails) and ": cut off at the end" in fails[-1]
        if rec and rec["state"] == "requested":
            pad = bool(rec.get("pad"))                   # the same open take keeps its request
        else:
            pad = last_cut                               # after a cut-off take, ask with a throwaway word after it
        # every sentence gets MAX_TAKES takes; one cut off every time gets one padded take more
        extra = pad and not rec.get("pad_tried") and all(": cut off at the end" in f for f in fails)
        if take > MAX_TAKES and not (take == MAX_TAKES + 1 and extra) \
                and not (rec and rec["state"] == "requested" and rec.get("pad")):
            failed.append(f"{s['id']} ({'; '.join(fails[-1:])})")
            continue
        # the take's ref number never repeats for a sentence (hf.max_take), except a requested take planned again
        n = ((rec.get("n") or rec["take"]) if rec and rec["state"] == "requested"   # the same open take: same ref,
             else hf.max_take(run, s["id"]) + 1)                                   # so its reservation is replaced
        plan[s["id"]] = {"take": take, "n": n, "state": "requested", "spoken_sha": s["spoken_sha"],
                         "failures": fails, "pad": pad,
                         "pad_tried": bool(pad or (rec and rec.get("pad_tried")))}
        text = tts_text(s["spoken"]) + (PAD_SUFFIX if pad else "")
        items.append({"id": s["id"], "take": take, "ref": f"narration/{s['id']}/t{n}", "padded": pad,
                      **hf.audio_request(text, run)})
    if failed:
        raise Fail(f"sentence(s) failed the take checks {MAX_TAKES} times: {', '.join(failed)}. For a transcript "
                   f"failure add the word to RUN/pronunciations.json (how to say it, or how the check hears it) or "
                   f"reword the sentence in narration.json (back through Gate 0); for a take cut off at the end every "
                   f"time, reword the sentence's ending; then tts-plan again")
    credits = None
    if items:
        try:   # checks the setup and reserves every take's estimated price until it is submitted
            credits = hf.ledger_check(run, "audio", [i["ref"] for i in items])
            for i in items:   # write-ahead: each printed request is bound to its reserved line
                i["request_sha"] = hf.bind(run, i["ref"], i)
        except hf.HFError as e:
            raise Fail(str(e))
    hfst.update(plan)
    write_json(state_path(run), st)
    print("PLAN_JSON " + json.dumps({"command": "tts-plan", "run": str(run), "items": items, "in_flight": in_flight,
                                     "credits": credits, "ready": len(segs) - len(pending), "segments": len(segs),
                                     "next": "submit each item (quote one first), then `tts-ingest` with the job ids; "
                                             "after jobs_wait, `tts-ingest` with the urls" if items or in_flight
                                             else "all takes ready: `narrate.py run`"}))


def cmd_tts_ingest(args) -> None:
    """Recording job ids never depends on the run's setup, and every record is saved the moment it is made (the narrate
    state first, then the ledger), so a later failure in the same call (a download, the speech check, a setup that no
    longer validates) can never leave a paid take looking unsubmitted and planned again."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    lex = load_pronunciations(run)
    segs = load_script(script, lex)
    if engine() != "higgsfield":
        raise Fail(f"{cfg.where('narration.engine')} is {engine()!r}, not 'higgsfield'")
    st = load_state(run)
    by_id = {s["id"]: s for s in segs}
    hfst = st.setdefault("hf", {})
    results = read_json(Path(args.results))
    if not isinstance(results, list):
        raise Fail(f"{args.results}: expected a JSON list of {{id, job_id, quoted, url | failed | unknown}}")
    def save() -> None:
        write_json(state_path(run), st)

    # The ledger is the record of every job id: a job id it already holds is never recorded again (whatever the
    # take's state says), and no line can block another. A failed/url line belongs to a take only when its job id is
    # the take's current job; the one exception is a `failed` line closing an `unknown` take that never got a job.
    seen = {j.get("job_id") for j in hf.ledger(run)["jobs"] if j.get("job_id")}
    to_check: dict[int, list[tuple[dict, Path]]] = {}
    recorded, failed_jobs, extra, problems, skipped = [], [], [], [], []
    # Pass 1: every new job id (and every unknown outcome) is recorded before anything else can go wrong. The ledger
    # is written first, then the narrate state; tts-plan reads the ledger, so a crash between the two never makes a
    # paid take look unsubmitted.
    try:
        for r in results:
            gid, job = r.get("id"), r.get("job_id")
            rec = hfst.get(gid)
            if job:
                if job in seen:
                    # already in the ledger. Only repair: the line for this very take was written, but an interrupted
                    # call never saved the take's state (still requested / unknown)
                    held = hf.ledger_job(run, job)
                    if (rec is not None and rec["state"] in ("requested", "unknown") and held
                            and held["ref"] == take_ref(gid, rec) and held["state"] == "submitted"):
                        rec.update(state="submitted", job_id=job, quoted=held.get("quoted"))
                        save()
                    elif held and rec is not None and held["ref"] != take_ref(gid, rec) and \
                            rec.get("job_id") != job and not held["ref"].startswith(f"{take_ref(gid, rec)}/extra-"):
                        # never silent: a job booked under another ref (an older take, a trial) is not this take's
                        problems.append(f"{gid}: job {job} is already booked as {held['ref']}; it is never attached "
                                        f"to take {take_ref(gid, rec)} (submit the planned request for that)")
                    continue
                if rec is not None and rec["state"] in ("requested", "unknown"):
                    hf.ledger_record(run, "audio", take_ref(gid, rec), job, r.get("quoted"), "submitted")
                    rec.update(state="submitted", job_id=job, quoted=r.get("quoted"))
                    save()
                    recorded.append(gid)
                else:
                    # a second paid job for a take that already has one (a resubmit), or a job for a sentence with
                    # no open take: never dropped, recorded on its own ref and reported, never used as a take
                    base = take_ref(gid, rec) if rec else f"narration/{gid}"
                    hf.ledger_record(run, "audio", f"{base}/extra-{job}", job, r.get("quoted"), "submitted")
                    extra.append({"id": gid, "job_id": job, "take_job_id": (rec or {}).get("job_id")})
                seen.add(job)
            elif r.get("unknown") and rec is not None and rec["state"] == "requested":
                hf.ledger_record(run, "audio", take_ref(gid, rec), None, r.get("quoted"), "unknown")
                rec.update(state="unknown", unknown=r["unknown"])
                save()
    finally:
        save()
    # Pass 2: refunds and results, matched by job id only (a line without one could belong to an older take and is
    # refused). A problem with one line is collected and reported; it never stops the others.
    for r in results:
        if not (r.get("failed") or r.get("url")):
            continue
        gid, job = r.get("id"), r.get("job_id")
        rec = hfst.get(gid)
        if not job:
            if r.get("failed") and rec is not None and rec["state"] == "unknown" and not rec.get("job_id"):
                # an unknown submission the account shows was never created: close it (counts 0) and plan it again
                hf.ledger_fail(run, take_ref(gid, rec))
                rec.update(state="requested", job_id=None)
                save()
                failed_jobs.append(gid)
                continue
            problems.append(f"{gid}: a failed/url line needs the job_id it belongs to (never applied to a take by "
                            f"guess); only an `unknown` take with no job takes a `failed` line without one")
            continue
        if rec is None or rec.get("job_id") != job:
            if r.get("failed") and hf.ledger_fail_job(run, job):   # a refunded extra (or older) job: counts 0 now
                failed_jobs.append(gid)
            else:
                skipped.append(gid)        # an earlier take's job (already checked, refunded or passed), or an extra
            continue
        ref = take_ref(gid, rec)
        if r.get("failed"):
            if rec["state"] not in ("submitted", "unknown"):
                skipped.append(gid)
                continue
            hf.ledger_fail(run, ref)
            rec.update(state="requested", job_id=None)   # refunded: tts-plan requests the same take again
            save()
            failed_jobs.append(gid)
            continue
        if r.get("url"):
            if rec["state"] != "submitted":
                skipped.append(gid)            # this take was already downloaded and checked
                continue
            if gid not in by_id:
                problems.append(f"{gid}: no longer in script.json; its job is recorded, its take is not used")
                continue
            dest = run / "narration" / "_hf" / f"take{rec['take']}" / f"{gid}_raw.wav"
            if dest.is_file():   # an earlier paid take with the same take number (the count restarts when the
                #                  spoken form changes): keep it, never overwrite paid audio
                keep = run / "narration" / "_hf" / "archive" / f"{gid}_take{rec['take']}_{int(dest.stat().st_mtime)}.wav"
                keep.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dest, keep)
            try:
                hf.download(r["url"], dest, job)
            except hf.HFError as e:
                problems.append(f"{gid}: {e}; the job is paid and recorded: run tts-ingest again with its url "
                                f"(never resubmit)")
                continue
            to_check.setdefault(rec["take"], []).append((by_id[gid], dest))
    save()
    voice = voice_key("higgsfield", run)   # needed only from here on: to check takes and to say what is left
    passed, rejected = [], []
    failures: dict[str, str] = {}
    for take, pairs in sorted(to_check.items()):
        takes_dir = run / "narration" / "_hf" / f"take{take}"
        padded = frozenset(s["id"] for s, _ in pairs if hfst.get(s["id"], {}).get("pad"))
        still = check_takes(run, st, [s for s, _ in pairs], {s["id"]: p for s, p in pairs}, take, takes_dir, lex,
                            voice, None, failures, padded)
        still_ids = {s["id"] for s in still}
        for s, _ in pairs:
            if s["id"] in still_ids:
                hfst[s["id"]].update(state="rejected")
                hfst[s["id"]].setdefault("failures", []).append(failures.get(s["id"], f"take {take}: rejected"))
                rejected.append(s["id"])
            else:
                hfst.pop(s["id"], None)
                passed.append(s["id"])
    write_json(state_path(run), st)
    left = [s["id"] for s in segs if seg_state(run, s, st, voice) != "ready"]
    print("INGEST_JSON " + json.dumps({"command": "tts-ingest", "recorded": recorded, "extra_jobs": extra,
                                       "passed": passed,
                                       "rejected": {g: failures.get(g) for g in rejected}, "failed_jobs": failed_jobs,
                                       "problems": problems, "skipped": skipped, "not_ready": left,
                                       "next": "narrate.py run" if not left else "wait for the submitted jobs, then "
                                               "tts-ingest their urls; for rejected or refunded takes, tts-plan again"}))
    if problems:   # after everything else was recorded and checked
        raise Fail("tts-ingest: " + "; ".join(problems))


def cmd_recheck(args) -> None:
    """Check each rejected Higgsfield take again from its raw file (free): after a fix to the checks, a take the old
    checks wrongly rejected can pass without a new paid take. Only the newest take of a sentence still waiting for a
    take (state rejected) is checked."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    lex = load_pronunciations(run)
    segs = {s["id"]: s for s in load_script(script, lex)}
    if engine() != "higgsfield":
        raise Fail(f"{cfg.where('narration.engine')} is {engine()!r}, not 'higgsfield'")
    voice = voice_key("higgsfield", run)
    st = load_state(run)
    hfst = st.setdefault("hf", {})
    passed, still_failed, failures = [], {}, {}
    for gid, rec in list(hfst.items()):
        if rec.get("state") != "rejected" or gid not in segs or rec.get("spoken_sha") != segs[gid]["spoken_sha"]:
            continue
        take = rec["take"]
        takes_dir = run / "narration" / "_hf" / f"take{take}"
        raw = takes_dir / f"{gid}_raw.wav"
        if not raw.is_file():
            still_failed[gid] = f"raw take {raw} missing"
            continue
        padded = frozenset([gid]) if rec.get("pad") else frozenset()
        if check_takes(run, st, [segs[gid]], {gid: raw}, take, takes_dir, lex, voice, None, failures, padded):
            still_failed[gid] = failures.get(gid, "rejected")
        else:
            hfst.pop(gid, None)
            passed.append(gid)
    write_json(state_path(run), st)
    print("RECHECK_JSON " + json.dumps({"command": "recheck", "passed": passed, "still_rejected": still_failed}))


# ------------------------------------------------------------------ trial takes (how to say a word)

def try_slug(say: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", say.lower()).strip("-")


def try_spoken(run: Path, script: Path, sid: str, word: str, say: str) -> dict:
    """Sentence `sid` as the voice is given it when `word` is said as `say` (the run's other pronunciations kept)."""
    lex = load_pronunciations(run)
    lex[word.strip().lower()] = {"written": word.strip(), "say": say.strip(), "heard": []}
    seg = next((s for s in load_script(script, lex) if s["id"] == sid), None)
    if seg is None:
        raise Fail(f"sentence {sid} is not in {script}")
    if word.strip().lower() not in seg["text"].lower():
        raise Fail(f"{word!r} is not in sentence {sid}: {seg['text']}")
    return seg


def cmd_try_plan(args) -> None:
    """Trial takes of one sentence, one per spelling of one word, so the user can pick how it is said by ear. Each is
    reserved in the ledger as narration/<id>/try-<spelling> and remembered (word, spelling, spoken form) in the
    narrate state, so try-adopt can later use the picked one as the sentence's take without paying again."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    text_approved_for_takes(run, script)
    if len(set(map(try_slug, args.say))) != len(args.say) or len(args.say) > 6:
        raise Fail("give 1 to 6 different spellings (--say)")
    items = []
    for say in args.say:
        seg = try_spoken(run, script, args.id, args.word, say)
        items.append({"say": say, "ref": f"narration/{args.id}/try-{try_slug(say)}",
                      **hf.audio_request(tts_text(seg["spoken"]), run)})
    try:
        credits = hf.ledger_check(run, "audio", [i["ref"] for i in items])
        for i in items:
            i["request_sha"] = hf.bind(run, i["ref"], i)
    except hf.HFError as e:
        raise Fail(str(e))
    print("PLAN_JSON " + json.dumps({"command": "try-plan", "id": args.id, "word": args.word, "items": items,
                                     "credits": credits,
                                     "next": "quote one, submit each, then `try-submitted` per job before waiting; "
                                             "download the results to narration/_tries/<id>/<spelling>.wav and put "
                                             "them on a page for the user to pick"}))


def cmd_try_submitted(args) -> None:
    """Record a submitted trial job (ledger and narrate state) at once, before waiting for it."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    seg = try_spoken(run, script, args.id, args.word, args.say)
    ref = f"narration/{args.id}/try-{try_slug(args.say)}"
    held = hf.ledger_job(run, args.job)
    if held and held["ref"] != ref:
        raise Fail(f"job {args.job} is already booked as {held['ref']}")
    hf.ledger_record(run, "audio", ref, args.job, args.quoted, "submitted")
    st = load_state(run)
    st.setdefault("tries", {}).setdefault(args.id, {})[try_slug(args.say)] = {
        "ref": ref, "word": args.word, "say": args.say, "job_id": args.job, "spoken_sha": seg["spoken_sha"]}
    write_json(state_path(run), st)
    print("TRY_JSON " + json.dumps({"command": "try-submitted", "id": args.id, "ref": ref, "job_id": args.job}))


def cmd_try_adopt(args) -> None:
    """Make the trial take the user picked sentence <id>'s take: no new job. Refused unless RUN/pronunciations.json
    already says the word that way (so the take matches what the sentence's spoken form is now), and while another
    take of the sentence is in flight. The take then goes through tts-ingest's download and checks like any other."""
    import argparse
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    st = load_state(run)
    trial = st.get("tries", {}).get(args.id, {}).get(try_slug(args.say))
    if not trial:
        raise Fail(f"no trial take of sentence {args.id} spelled {args.say!r} (try-plan / try-submitted)")
    held = hf.ledger_job(run, trial["job_id"])
    if not held or held["ref"] != trial["ref"] or held["state"] != "submitted":
        raise Fail(f"the ledger does not hold job {trial['job_id']} as a submitted {trial['ref']}")
    seg = next((s for s in load_script(script, load_pronunciations(run)) if s["id"] == args.id), None)
    if seg is None:
        raise Fail(f"sentence {args.id} is not in {script}")
    if seg["spoken_sha"] != trial["spoken_sha"]:
        raise Fail(f"sentence {args.id} is not spoken the way the trial was: add {trial['word']!r}: "
                   f"{trial['say']!r} to RUN/pronunciations.json first (or the sentence changed since the trial)")
    hfst = st.setdefault("hf", {})
    rec = hfst.get(args.id)
    if rec and rec["state"] in ("submitted", "unknown"):
        raise Fail(f"sentence {args.id} has take {take_ref(args.id, rec)} in flight; ingest it first")
    if rec and rec["state"] == "requested":
        hf.ledger_release(run, take_ref(args.id, rec))   # the planned take is never submitted now
    take = rec["take"] + 1 if rec and rec["state"] == "rejected" else rec["take"] if rec else 1
    hfst[args.id] = {"take": take, "n": hf.max_take(run, args.id) + 1, "state": "submitted",
                     "job_id": trial["job_id"], "quoted": held.get("quoted"), "spoken_sha": seg["spoken_sha"],
                     "failures": (rec or {}).get("failures", []), "pad": False,
                     "pad_tried": bool(rec and rec.get("pad_tried")), "adopted_from": trial["ref"]}
    write_json(state_path(run), st)
    results = run / "narration" / "_tries" / args.id / "adopt.json"
    results.parent.mkdir(parents=True, exist_ok=True)
    write_json(results, [{"id": args.id, "job_id": trial["job_id"], "url": args.url}])
    cmd_tts_ingest(argparse.Namespace(run=str(run), script=args.script, results=str(results)))


def cmd_tts(args) -> None:
    """Runs in the qwen-tts venv. Calls qwen_tts_synth.main() (settings tts.qwen_synth) once
    per item with the exact CLI (--text-file ... --model 1.7B --seed s; --ref/--ref-text when settings
    tts.reference_wav/_text are set, else the script's default reference), with Qwen3TTSModel.from_pretrained memoized
    so the model loads once for the whole batch."""
    batch = read_json(Path(args.batch))["items"]
    synth = tool("tts.qwen_synth")
    ref = []
    for key, flag in (("tts.reference_wav", "--ref"), ("tts.reference_text", "--ref-text")):
        if cfg.path(key):
            ref += [flag, str(tool(key))]
    sys.path.insert(0, str(synth.parent))
    import qwen_tts_synth
    from qwen_tts import Qwen3TTSModel

    original = Qwen3TTSModel.from_pretrained
    cache: dict = {}

    def cached(*a, **k):
        key = repr((a, sorted(k.items())))
        if key not in cache:
            cache[key] = original(*a, **k)
        return cache[key]

    Qwen3TTSModel.from_pretrained = staticmethod(cached)
    for i, it in enumerate(batch, 1):
        say(f"  tts [{i}/{len(batch)}] {it['id']} seed {it['seed']}")
        sys.argv = [str(synth), "--text-file", it["text_file"], it["out"], "--model", TTS_MODEL,
                    "--seed", str(it["seed"]), *ref]
        try:
            qwen_tts_synth.main()
        except SystemExit as e:
            if e.code not in (0, None):
                raise Fail(f"qwen_tts_synth exited {e.code} on segment {it['id']}")
        if not Path(it["out"]).is_file():
            raise Fail(f"qwen_tts_synth wrote nothing for segment {it['id']}")
    if len(cache) > 1:
        raise Fail(f"expected one model load, got {len(cache)}")


def cmd_asr(args) -> None:
    """Runs in the whisperx venv: faster-whisper, the engine whisperx.load_model wraps, without
    the VAD front end (clips are single trimmed sentences)."""
    from faster_whisper import WhisperModel
    batch = read_json(Path(args.batch))["items"]
    model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", local_files_only=True)
    out = {}
    for it in batch:
        segments, _info = model.transcribe(it["wav"], language="en", beam_size=5,
                                           condition_on_previous_text=False, vad_filter=False)
        out[it["id"]] = " ".join(s.text.strip() for s in segments).strip()
    write_json(Path(args.out), out)


def cmd_words(args) -> None:
    """Runs in the whisperx venv: faster-whisper word timestamps of a whole recording (engine 'recorded')."""
    from faster_whisper import WhisperModel
    model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8", local_files_only=True)
    segments, _info = model.transcribe(args.wav, language="en", beam_size=5, condition_on_previous_text=False,
                                       vad_filter=False, word_timestamps=True)
    words = [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
             for s in segments for w in (s.words or []) if w.word.strip()]
    write_json(Path(args.out), {"words": words})


# ------------------------------------------------------------------ recorded (the user's own reading)

def find_audio(run: Path, stem: str) -> Path | None:
    """RUN/narration/<stem>.<wav|mp3|m4a|flac>, or None; more than one is refused."""
    found = [run / "narration" / (stem + ext) for ext in REC_EXTS if (run / "narration" / (stem + ext)).is_file()]
    if len(found) > 1:
        raise Fail(f"more than one {stem}.* in {(run / 'narration').as_posix()}: "
                   f"{', '.join(p.name for p in found)}; keep one")
    return found[0] if found else None


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cut_key(kind: str, src_sha: str, seg: dict, segs: list[dict]) -> str:
    """What a recorded cut depends on: the source file and the spoken text it was aligned against (a retake: its own
    sentence; the recording: every sentence, since the word alignment spans the whole script)."""
    text = seg["spoken_sha"] if kind == "retake" else sha256("\n".join(f"{s['id']} {s['spoken_sha']}" for s in segs))
    return f"{src_sha}:{text}"


def recorded_plan(run: Path, segs: list[dict], st: dict) -> tuple[dict, list[str], Path | None]:
    """(todo, missing, recording). todo = {id: (kind, file, sha)}: segments to cut now from a source they were not
    made from, a retake_<id> file if there is one (it always wins), else the recording. missing = segments not ready
    with no such source: nothing recorded yet, or their last cut from this very file against this very script text
    failed (state "failed", keyed by cut_key)."""
    rec = find_audio(run, "recording")
    rec_sha = file_sha(rec) if rec else None
    failed = st.get("failed", {})
    todo, missing = {}, []
    for s in segs:
        r = st["segments"].get(s["id"], {})
        ready = seg_state(run, s, st, "recorded") == "ready"
        retake = find_audio(run, f"retake_{s['id']}")
        src = ("retake", retake, file_sha(retake)) if retake else ("recording", rec, rec_sha) if rec else None
        if src is None or failed.get(s["id"]) == cut_key(src[0], src[2], s, segs):
            if not ready:
                missing.append(s["id"])
        elif not (ready and r.get("source_sha256") == src[2]):
            todo[s["id"]] = src
    return todo, missing, rec


def recording_words(run: Path, x, sha: str, work: Path) -> list[dict]:
    """Word timestamps of the recording (decoded samples x), cached in narration/_recording_words.json by sha."""
    cache = run / "narration" / "_recording_words.json"
    if cache.is_file():
        c = read_json(cache)
        if c.get("sha256") == sha and c.get("model") == ASR_MODEL:
            return c["words"]
    wav, out = work / "recording_48k.wav", work / "words.json"
    write_pcm16(wav, x)
    say(f"recorded: word timestamps for {len(x) / SR:.1f} s of recording (faster-whisper {ASR_MODEL}, CPU)")
    p = subprocess.run([str(tool("asr.whisper_python")), str(Path(__file__).resolve()), "_words", "--wav", str(wav),
                        "--out", str(out)], stdout=sys.stderr, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"word timestamps failed (exit {p.returncode})")
    words = read_json(out)["words"]
    write_json(cache, {"sha256": sha, "model": ASR_MODEL, "words": words})
    return words


def align_words(segs: list[dict], words: list[dict]) -> dict[str, tuple[int, int] | None]:
    """{segment id: (first, last) index into words, or None when none of its words were heard}. The spoken script and
    the transcript are normalized as in the transcript check and aligned token by token at minimum edit distance; a
    segment owns the recording words matched or substituted against its own tokens."""
    ref, ref_seg, hyp, hyp_word = [], [], [], []
    for k, s in enumerate(segs):
        for t in norm_words(s["spoken"]):
            ref.append(t)
            ref_seg.append(k)
    for i, w in enumerate(words):
        for t in norm_words(w["w"]):
            hyp.append(t)
            hyp_word.append(i)
    n, m = len(ref), len(hyp)
    back = [bytearray(m + 1) for _ in range(n + 1)]   # 0 match/substitute, 1 script token unheard, 2 extra word
    back[0] = bytearray([2] * (m + 1))
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur, bi, r = [i] + [0] * m, back[i], ref[i - 1]
        bi[0] = 1
        for j in range(1, m + 1):
            d, u, left = prev[j - 1] + (r != hyp[j - 1]), prev[j] + 1, cur[j - 1] + 1
            if d <= u and d <= left:
                cur[j] = d
            elif u <= left:
                cur[j], bi[j] = u, 1
            else:
                cur[j], bi[j] = left, 2
        prev = cur
    owned: list[list[int]] = [[] for _ in segs]
    i, j = n, m
    while i > 0 or j > 0:
        b = back[i][j]
        if i > 0 and j > 0 and b == 0:
            owned[ref_seg[i - 1]].append(hyp_word[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and (j == 0 or b == 1):
            i -= 1
        else:
            j -= 1
    return {s["id"]: (min(o), max(o)) if o else None for s, o in zip(segs, owned)}


def frame_db(x):
    """10 ms RMS frames in dB, smoothed over 30 ms."""
    import numpy as np
    n = int(round(TRIM_FRAME_S * SR))
    frames = len(x) // n
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n).astype(np.float64) ** 2, axis=1))
    return np.convolve(20.0 * np.log10(np.maximum(rms, 1e-10)), np.ones(3) / 3.0, mode="same")


def quiet_cut(db, a_s: float, b_s: float) -> float:
    """A cut time in [a_s, b_s]: the middle of the longest run of frames within REC_QUIET_DB of the quietest one."""
    fa, fb = max(0, int(a_s / TRIM_FRAME_S)), min(len(db), int(b_s / TRIM_FRAME_S) + 1)
    if fb - fa < 2:
        return (a_s + b_s) / 2.0
    quiet = db[fa:fb] <= db[fa:fb].min() + REC_QUIET_DB
    best_start, best_len, start = 0, 0, None
    for k, q in enumerate(list(quiet) + [False]):
        if q and start is None:
            start = k
        elif not q and start is not None:
            if k - start > best_len:
                best_start, best_len = start, k - start
            start = None
    return min(max((fa + best_start + best_len / 2.0) * TRIM_FRAME_S, a_s), b_s)


def cut_span(x, db, words: list[dict], span: tuple[int, int], floor_s: float) -> tuple[int, int]:
    """Sample range of the recording for the words span: from a quiet point in the gap before its first word to one
    in the gap after its last (never before floor_s, the previous cut's end), at most REC_HEAD_PAD_S /
    REC_TAIL_PAD_S beyond the word timestamps."""
    wa, wb = span
    total = len(x) / SR
    first, last = words[wa]["s"], words[wb]["e"]
    before = max(words[wa - 1]["e"] if wa > 0 else 0.0, floor_s)
    after = words[wb + 1]["s"] if wb + 1 < len(words) else total
    a = max(quiet_cut(db, before, max(first, before)), first - REC_HEAD_PAD_S, floor_s)
    b = min(quiet_cut(db, last, max(after, last)), last + REC_TAIL_PAD_S, total)
    return int(round(a * SR)), max(int(round(a * SR)), int(round(b * SR)))


def cut_recorded(run: Path, segs: list[dict], st: dict, todo: dict, lex: dict) -> dict[str, str]:
    """Cut, trim and check every todo segment (recorded); a passing one goes into state like a take (takes 1). A
    failing one loses its old take (one track, one reading) and is marked failed for its source. {id: why} of the
    failures."""
    nar = run / "narration"
    work = nar / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    clips, failures = {}, {}
    from_rec = [gid for gid, src in todo.items() if src[0] == "recording"]
    if from_rec:
        rec, rec_sha = todo[from_rec[0]][1:]
        x = decode_48k(rec)
        words = recording_words(run, x, rec_sha, work)
        spans, db, floor_s = align_words(segs, words), frame_db(x), 0.0
        for s in segs:   # script order, so each cut starts after the previous sentence's cut
            sp = spans[s["id"]]
            if sp is None:
                if s["id"] in from_rec:
                    failures[s["id"]] = f"not heard in {rec.name}"
                continue
            a, b = cut_span(x, db, words, sp, floor_s)
            floor_s = b / SR
            if s["id"] in from_rec:
                clips[s["id"]] = (x[a:b], a)
    for gid, (kind, path, _sha) in todo.items():
        if kind == "retake":
            clips[gid] = (decode_48k(path), None)
    asr_items, trimmed = [], {}
    for gid, (y, off) in clips.items():
        ab = trim_bounds(y)
        if ab is None:
            failures[gid] = "silent"
            continue
        tp = work / f"{gid}_trim.wav"
        write_pcm16(tp, y[ab[0]:ab[1]])
        span = None if off is None else [round((off + ab[0]) / SR, 3), round((off + ab[1]) / SR, 3)]
        trimmed[gid] = (tp, ab[1] - ab[0], span)
        asr_items.append({"id": gid, "wav": str(tp)})
    transcripts = transcribe(asr_items, work, "recorded")
    failed = st.setdefault("failed", {})
    by_id = {s["id"]: s for s in segs}
    for gid, (kind, path, sha) in todo.items():
        seg = by_id[gid]
        if gid in trimmed:
            tp, n, span = trimmed[gid]
            hyp = transcripts.get(gid)
            if hyp is None:
                raise Fail(f"ASR returned no transcript for segment {gid}")
            chk = check_take(seg["spoken"], hyp, n, lex)
            nums = f"wer={chk['wer']:.3f} cer_nospace={chk['cer_nospace']:.3f} pace={chk['pace_wps']:.2f} w/s"
            say(f"  {gid}: {path.name} {nums} {'OK' if chk['ok'] else 'REJECT'} | {hyp.strip()}")
            if chk["ok"]:
                shutil.copyfile(tp, run / seg_wav_rel(gid))
                st["segments"][gid] = {"text": seg["text"], "text_sha256": seg["sha"],
                                       "spoken_sha256": seg["spoken_sha"], "wav": seg_wav_rel(gid),
                                       "samples": int(n), "transcript": hyp.strip(), "wer": chk["wer"],
                                       "cer_nospace": chk["cer_nospace"], "pace_wps": chk["pace_wps"],
                                       "takes": 1, "seed": None, "voice": "recorded", "source": path.name,
                                       "source_sha256": sha, "source_span_s": span}
                failed.pop(gid, None)
                continue
            failures[gid] = f"{nums}, heard {hyp.strip()!r}"
        failed[gid] = cut_key(kind, sha, seg, segs)
        st["segments"].pop(gid, None)
        (run / seg_wav_rel(gid)).unlink(missing_ok=True)
    write_json(state_path(run), st)
    shutil.rmtree(work, ignore_errors=True)
    return failures


def reading_path(run: Path) -> Path:
    return run / "narration" / "reading.md"


def write_reading(run: Path, script: Path, segs: list[dict], retake: list[str], lex: dict) -> Path:
    """narration/reading.md: every sentence, numbered, with its id, grouped by paragraph (scene); first how to say
    the words the pronunciations cover, and the sentences to re-record when `retake` names any."""
    title = read_json(script).get("title") or run.name
    nar = (run / "narration").as_posix()
    out = [f"# Reading script: {title}", "",
           f"Read every sentence below aloud, in order, into ONE recording. Save it as `{nar}/recording.wav` "
           "(`.mp3`, `.m4a` or `.flac` also work), then run:", "",
           f"    {cfg.command(PY, Path(__file__).resolve(), 'run', '--run', run)}", "",
           "- Don't read the numbers or the ids in brackets.",
           "- Leave a breath (about half a second) between sentences and a longer pause between paragraphs; "
           "the video sets its own pauses afterwards.",
           "- A quiet room, the microphone at a steady distance, no music.",
           "- Stumbled? Pause and keep going. narrate.py names every sentence that does not match the text; record "
           f"just that sentence alone and save it as `{nar}/retake_<id>.wav` (for example `retake_1.2.wav`). A "
           "retake always wins over the long recording for its sentence; delete it to use the recording again.", ""]
    script_text = "\n".join(s["text"] for s in segs)
    used = [e for k, e in lex.items() if _replace_words(script_text, {k: "\x00"}) != script_text]
    if used:
        out += ["## Say these words like this", ""]
        out += [f"- {e['written']}: say \"{e['say']}\"" for e in used]
        out.append("")
    by_id = {s["id"]: s for s in segs}
    if retake:
        out += ["## Re-record these sentences, each alone", ""]
        out += [f"- [{gid}] {by_id[gid]['text']}  ->  `retake_{gid}.wav`" for gid in retake]
        out.append("")
    scene = None
    for k, s in enumerate(segs, 1):
        if s["scene"] != scene:
            scene = s["scene"]
            out += ([""] if out[-1] else []) + [f"## Paragraph {scene}", ""]
        out.append(f"{k}. [{s['id']}] {s['text']}")
    p = reading_path(run)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


def recorded_next(run: Path, segs: list[dict], st: dict) -> tuple[str, str, list[str]]:
    """(next, action, retake ids) for the recorded engine: run (a new source to cut, or only the track to rebuild),
    record (nothing recorded yet) or retake (sentences to re-record alone)."""
    todo, missing, rec = recorded_plan(run, segs, st)
    nar = (run / "narration").as_posix()
    if todo or not missing:
        return "run", "cut the recording into sentences and check them" if todo else "rebuild the narration track", []
    if rec is None and not any(seg_state(run, s, st, "recorded") == "ready" for s in segs):
        return ("record", f"the user records the narration: narrate.py reading writes {nar}/reading.md; read it "
                          f"aloud into one file saved as {nar}/recording.wav (.mp3, .m4a, .flac also work), then "
                          f"narrate.py run", [])
    return ("retake", f"the user re-records sentence(s) {', '.join(missing)}, each alone, saved as "
                      f"{nar}/retake_<id>.wav (listed at the top of {nar}/reading.md), then narrate.py run", missing)


# ------------------------------------------------------------------ run

def gpu_lease(purpose: str, job_file: Path) -> None:
    """Run `_worker --job job_file` inside a GPU-queue lease (settings gpu_queue), or directly when no queue is set."""
    queue = cfg.path("gpu_queue")
    if queue is None:
        say(f"gpu: {purpose} (no GPU queue set; running directly)")
        p = subprocess.run([str(PY), str(Path(__file__).resolve()), "_worker", "--job", str(job_file)],
                           stdout=sys.stderr, creationflags=NO_WINDOW)
        if p.returncode != 0:
            raise Fail(f"narration worker failed with exit {p.returncode} (see the worker output above)")
        return
    pwsh = cfg.powershell()
    if pwsh is None:
        raise Fail(f"gpu: {cfg.where('gpu_queue')} needs PowerShell; none on PATH, set powershell in settings")
    payload = f'& "{PY}" "{Path(__file__).resolve()}" _worker --job "{job_file}"'
    cmd = [str(pwsh), "-NoProfile", "-Command",
           f"& '{queue}' run -Agent scribeboard-narrate -Purpose '{purpose}' -Timeout 3600 -Payload '{payload}'"]
    say(f"gpu: waiting for a GPU-queue lease ({purpose})")
    p = subprocess.run(cmd, stdout=sys.stderr, creationflags=NO_WINDOW)
    if p.returncode != 0:
        log = cfg.path("gpu_queue_log")
        raise Fail(f"lease job failed with exit {p.returncode} (75 = no lease before timeout; "
                   + (f"see {log} and " if log else "see ") + "the worker output above)")


def build_narration(run: Path, segs: list[dict], st: dict) -> dict:
    import numpy as np
    nar = run / "narration"
    pieces, offsets, pos = [], [], 0
    prev_scene = None
    for seg in segs:
        if prev_scene is not None:
            gap = int(round((PAUSE_SCENES_S if seg["scene"] != prev_scene else PAUSE_SEGMENTS_S) * SR))
            pieces.append(np.zeros(gap, dtype=np.float32))
            pos += gap
        x = read_pcm16(run / seg_wav_rel(seg["id"]))
        rec = st["segments"][seg["id"]]
        if len(x) != rec["samples"]:
            raise Fail(f"{seg_wav_rel(seg['id'])}: {len(x)} samples, state says {rec['samples']}")
        offsets.append((pos, pos + len(x)))
        pieces.append(x)
        pos += len(x)
        prev_scene = seg["scene"]
    joined = np.concatenate(pieces)
    total = len(joined)

    raw = nar / "_joined_raw.wav"
    write_pcm16(raw, joined)
    wav = nar / "narration.wav"
    tmp = nar / "narration.tmp.wav"
    eq = presence_eq()
    run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", f"{eq},{LOUDNORM}" if eq else LOUDNORM,
             "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(tmp)], "ffmpeg EQ+loudnorm")
    raw.unlink()
    out = read_pcm16(tmp)
    if len(out) != total:
        tmp.unlink()
        raise Fail(f"EQ+loudnorm changed the length: {len(out)} samples vs joined {total}")
    os.replace(tmp, wav)
    mp3 = nar / "narration.mp3"
    run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)],
            "ffmpeg mp3 encode")

    duration = total / SR
    starts = [a / SR for a, _ in offsets]
    starts[0] = 0.0
    seg_rows = []
    for i, (seg, (a, b)) in enumerate(zip(segs, offsets)):
        rec = st["segments"][seg["id"]]
        end = starts[i + 1] if i + 1 < len(segs) else duration
        seg_rows.append({"id": seg["id"], "scene": seg["scene"], "text": seg["text"], "text_sha256": seg["sha"],
                         "spoken_sha256": seg["spoken_sha"], "wav_sha256": file_sha(run / seg_wav_rel(seg["id"])),
                         "start_s": round(starts[i], 3), "end_s": round(end, 3), "speech_end_s": round(b / SR, 3),
                         "wav": seg_wav_rel(seg["id"]), "transcript": rec["transcript"], "wer": rec["wer"],
                         "cer_nospace": rec["cer_nospace"], "pace_wps": rec["pace_wps"], "takes": rec["takes"]})
    scene_ids = []
    for s in seg_rows:
        if s["scene"] not in scene_ids:
            scene_ids.append(s["scene"])
    scene_start = {sid: next(r["start_s"] for r in seg_rows if r["scene"] == sid) for sid in scene_ids}
    scene_start[scene_ids[0]] = 0.0
    scene_rows = [{"id": sid, "start_s": scene_start[sid],
                   "end_s": scene_start[scene_ids[k + 1]] if k + 1 < len(scene_ids) else round(duration, 3)}
                  for k, sid in enumerate(scene_ids)]
    alignment = {"version": 1, "sample_rate": SR, "duration_s": round(duration, 3),
                 "pause_between_segments_s": PAUSE_SEGMENTS_S, "pause_between_scenes_s": PAUSE_SCENES_S,
                 "segments": seg_rows, "scenes": scene_rows}
    write_json(run / "alignment.json", alignment)
    return alignment


def cmd_run(args) -> None:
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    lex = load_pronunciations(run)
    segs = load_script(script, lex)
    eng = engine()
    voice = voice_key(eng, run)
    st = load_state(run)
    nar = run / "narration"
    nar.mkdir(parents=True, exist_ok=True)

    # Drop cache entries (and their takes) for segments no longer in the script.
    live = {s["id"] for s in segs}
    for gid in [g for g in st["segments"] if g not in live]:
        (run / seg_wav_rel(gid)).unlink(missing_ok=True)
        del st["segments"][gid]
        say(f"removed stale segment {gid}")
    for gid in [g for g in st.get("failed", {}) if g not in live]:
        del st["failed"][gid]
    write_json(state_path(run), st)

    # Re-score cached takes from their stored transcript with the current check, so every record
    # carries the current numbers; a cached take that no longer passes is synthesized again.
    for s in segs:
        if seg_state(run, s, st, voice) == "ready":
            rec = st["segments"][s["id"]]
            chk = check_take(s["spoken"], rec["transcript"], rec["samples"], lex)
            rec.update(wer=chk["wer"], cer_nospace=chk["cer_nospace"], pace_wps=chk["pace_wps"])
            if not chk["ok"]:
                say(f"cached take {s['id']} no longer passes the check ({chk}); synthesizing again")
                del st["segments"][s["id"]]
    write_json(state_path(run), st)

    if eng == "recorded":
        todo, missing, _rec = recorded_plan(run, segs, st)
        say(f"narrate (recorded): {len(segs)} segments, {len(segs) - len(todo) - len(missing)} reused, "
            f"{len(todo)} to cut, {len(missing)} without a recording")
        failures = cut_recorded(run, segs, st, todo, lex) if todo else {}
        st = load_state(run)
        made = [gid for gid in todo if gid not in failures]
        nxt, action, retake = recorded_next(run, segs, st)
        if nxt != "run":
            p = write_reading(run, script, segs, retake, lex)
            if nxt == "record":
                raise Fail(f"no recording yet: {action} (written: {p.as_posix()})")
            why = "; ".join(f"{gid} ({failures.get(gid) or 'its last cut failed the check'})" for gid in retake)
            raise Fail(f"{len(retake)} sentence(s) to re-record: {why}. {action}")
        reused = [s["id"] for s in segs if s["id"] not in made]
    else:
        pending = [s for s in segs if seg_state(run, s, st, voice) != "ready"]
        made = [s["id"] for s in pending]
        reused = [s["id"] for s in segs if s not in pending]
        say(f"narrate: {len(segs)} segments, {len(reused)} reused, {len(pending)} to synthesize")
        if pending and eng == "higgsfield":
            raise Fail(f"{len(pending)} sentence(s) still need a Higgsfield take ({', '.join(made[:8])}"
                       f"{' ...' if len(made) > 8 else ''}): run `narrate.py tts-plan --run {run.as_posix()}`, submit "
                       f"the requests, then `tts-ingest`")
        if pending:
            work = nar / "_work"
            shutil.rmtree(work, ignore_errors=True)
            job = work / "job.json"
            texts = {s["id"]: (segs[k - 1]["spoken"] if k else None,
                               segs[k + 1]["spoken"] if k + 1 < len(segs) else None) for k, s in enumerate(segs)}
            write_json(job, {"run": str(run), "engine": eng, "voice": voice, "pronunciations": lex,
                             "segments": [{**s, "prev_text": texts[s["id"]][0], "next_text": texts[s["id"]][1]}
                                          for s in pending]})
            if eng == "qwen-clone":
                gpu_lease(f"scribeboard narration: Qwen3-TTS voice clone, {len(pending)} segment(s)", job)
            else:
                say(f"{eng}: cloud TTS, one request at a time (no GPU lease)")
                cmd_worker(argparse.Namespace(job=str(job)))
            st = load_state(run)
            missing = [s["id"] for s in pending if seg_state(run, s, st, voice) != "ready"]
            if missing:
                raise Fail(f"worker finished but segment(s) not ready: {', '.join(missing)}")

    al = build_narration(run, segs, st)
    print("NARRATE_JSON " + json.dumps({
        "command": "run", "run": str(run), "engine": eng, "segments": len(segs),
        "synthesized": made, "reused": reused,
        "duration_s": al["duration_s"], "alignment": str(run / "alignment.json"),
        "narration_wav": str(nar / "narration.wav"), "narration_mp3": str(nar / "narration.mp3")}))


# ------------------------------------------------------------------ status

def track_current(run: Path, segs: list[dict]) -> bool:
    """True when narration.wav and alignment.json hold each sentence's text, spoken form and the take now on disk.
    A retake (e.g. after a pronunciation was added) keeps the text but replaces the take, so the track is stale."""
    al_path = run / "alignment.json"
    if not (al_path.is_file() and (run / "narration" / "narration.wav").is_file()):
        return False
    have = [(r.get("id"), r.get("text_sha256"), r.get("spoken_sha256"), r.get("wav_sha256"))
            for r in read_json(al_path).get("segments", [])]
    want = [(s["id"], s["sha"], s["spoken_sha"],
             file_sha(run / seg_wav_rel(s["id"])) if (run / seg_wav_rel(s["id"])).is_file() else None)
            for s in segs]
    return have == want


def cmd_status(args) -> None:
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    segs = load_script(script, load_pronunciations(run))
    eng = engine()
    voice = voice_key(eng, run)
    st = load_state(run)
    rows = []
    # recorded: a ready segment whose recording or retake file was replaced since its cut is changed too
    superseded = set(recorded_plan(run, segs, st)[0]) if eng == "recorded" else set()
    for s in segs:
        state = seg_state(run, s, st, voice)
        if state == "ready" and s["id"] in superseded:
            state = "changed"
        rec = st["segments"].get(s["id"], {})
        rows.append({"id": s["id"], "state": state, "takes": rec.get("takes"), "wer": rec.get("wer"),
                     "cer_nospace": rec.get("cer_nospace"), "pace_wps": rec.get("pace_wps")})
        extra = (f" takes={rec['takes']} wer={rec['wer']} cer_nospace={rec.get('cer_nospace')} "
                 f"pace={rec['pace_wps']}") if state == "ready" else ""
        say(f"  {s['id']:>6}  {state:<8}{extra}  {s['text'][:60]}")
    current = track_current(run, segs)
    ready = sum(r["state"] == "ready" for r in rows)
    say(f"ready {ready}/{len(rows)}, alignment {'current' if current else 'stale or missing'}")
    retake: list[str] = []
    if current and ready == len(rows):
        nxt, action = "done", None
    elif eng == "recorded":
        nxt, action, retake = recorded_next(run, segs, st)
    elif eng == "higgsfield":
        nxt, action = ("run", "build the narration track") if ready == len(rows) else \
            ("tts-plan", "make the missing takes through Higgsfield (narrate.py tts-plan, submit, tts-ingest)")
    else:
        nxt, action = "run", {"qwen-clone": "render narration", "elevenlabs": "render narration (ElevenLabs TTS)",
                              "openai": "render narration (OpenAI TTS)"}[eng]
    print("NARRATE_JSON " + json.dumps({"command": "status", "run": str(run), "engine": eng, "segments": len(rows),
                                        "ready": ready, "pending": [r["id"] for r in rows if r["state"] != "ready"],
                                        "alignment_current": current and ready == len(rows), "next": nxt,
                                        "action": action, "retake": retake, "rows": rows}))


def cmd_reading(args) -> None:
    """Write narration/reading.md (any engine; the recorded engine needs it), with the sentences to re-record first."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    lex = load_pronunciations(run)
    segs = load_script(script, lex)
    retake = recorded_next(run, segs, load_state(run))[2]
    p = write_reading(run, script, segs, retake, lex)
    say(f"wrote {p.as_posix()}: {len(segs)} sentences" + (f", re-record {', '.join(retake)}" if retake else ""))
    print("NARRATE_JSON " + json.dumps({"command": "reading", "run": str(run), "reading": str(p),
                                        "sentences": len(segs), "retake": retake,
                                        "recording": str(run / "narration" / "recording.wav")}))


def cmd_clone_voice(args) -> None:
    sample = Path(args.sample).resolve()
    if not sample.is_file():
        raise Fail(f"no sample file {sample}")
    try:
        out = tts_cloud.clone_voice(sample, args.name, args.remove_background_noise)
    except tts_cloud.CloudError as e:
        raise Fail(str(e))
    say(f"ElevenLabs voice {args.name!r} created: voice_id {out['voice_id']}"
        + (" (ElevenLabs asks to verify this voice before use)" if out.get("requires_verification") else ""))
    say(f'put it in settings.local.json: "tts": {{"elevenlabs": {{"voice_id": "{out["voice_id"]}"}}}}, '
        '"narration": {"engine": "elevenlabs"}')
    print("NARRATE_JSON " + json.dumps({"command": "clone-voice", **out}))


# ------------------------------------------------------------------ doctor

def cmd_doctor(args) -> None:
    checks = {}

    def need(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = ok
        say(f"  {'ok  ' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}")

    eng = engine()
    keys = ENGINE_KEYS[eng]
    say("  settings (value, and the file it came from):")
    for line in cfg.lines(("narration.engine",) + keys + ("pronunciations",)):
        say(f"    {line}")
    for line in cfg.missing(keys):
        need(line, False)
    need(f"python {PY}", PY.is_file())
    tools = {}
    tool_keys = ("tts.qwen_python", "tts.qwen_synth") if eng == "qwen-clone" else ()
    for key in tool_keys + ("tts.presence_eq", "asr.whisper_python"):
        if key == "tts.presence_eq" and cfg.path(key) is None:
            say("  tts.presence_eq not set: the narration track gets loudnorm only, no EQ")
            continue
        try:
            tools[key] = tool(key)
            need(f"{key} {tools[key].as_posix()}", True)
        except Fail as e:
            need(key, False, str(e))
    if eng == "qwen-clone":
        queue = cfg.path("gpu_queue")
        if queue is None:
            say("  gpu_queue not set: the narration worker runs directly, without a lease")
        else:
            need("PowerShell for the GPU queue (powershell, else pwsh/powershell on PATH)", cfg.powershell() is not None)
    if eng == "higgsfield":   # the voice itself is chosen per run on the setup page (runsetup.py), never in settings
        try:
            n_voices = len(runsetup.voices()["voices"])
            need(f"voice catalog {runsetup.VOICES} ({n_voices} narrator voices for the setup page)", True)
        except runsetup.SetupError as e:
            need("voice catalog for the setup page", False, str(e))
    for exe in ("ffmpeg", "ffprobe"):
        need(f"{exe} on PATH", shutil.which(exe) is not None)
    if cfg.path("pronunciations") is not None:
        try:
            n = len(_read_pronunciations(tool("pronunciations")))
            need(f"pronunciations {cfg.path('pronunciations').as_posix()}", True, f"{n} word(s)")
        except Fail as e:
            need("pronunciations", False, str(e))
    if "tts.presence_eq" in tools:
        try:
            need("PRESENCE_EQ parsed", True, presence_eq()[:60] + "...")
        except Fail as e:
            need("PRESENCE_EQ parsed", False, str(e))
    if eng in tts_cloud.ENGINES:
        bad = tts_cloud.problems(eng)
        need(f"{eng}: {tts_cloud.key_env(eng)} in the environment, voice set", not bad, "; ".join(bad))
    qpy, synth = tools.get("tts.qwen_python"), tools.get("tts.qwen_synth")
    if qpy and synth:
        p = subprocess.run([str(qpy), str(synth), "--help"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        need("qwen_tts_synth --help lists --text-file and --seed",
             p.returncode == 0 and "--text-file" in p.stdout and "--seed" in p.stdout)
        probe = ("import os,sys; sys.path.insert(0, sys.argv[1]); import qwen_tts, qwen_tts_synth as q; "
                 "print(os.path.isdir(q.MODEL_BY_SIZE[sys.argv[2]]), os.path.isfile(sys.argv[3] or q.DEFAULT_REF), "
                 "os.path.isfile(sys.argv[4] or q.DEFAULT_REF_TEXT))")
        refs = [str(cfg.path(k) or "") for k in ("tts.reference_wav", "tts.reference_text")]
        p = subprocess.run([str(qpy), "-c", probe, str(synth.parent), TTS_MODEL, *refs], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        need(f"qwen_tts importable, {TTS_MODEL} model + voice reference present",
             p.returncode == 0 and p.stdout.strip().splitlines()[-1:] == ["True True True"],
             (p.stdout + p.stderr).strip()[-200:])
    if "asr.whisper_python" in tools:
        probe = ("import sys; from faster_whisper.utils import download_model; "
                 "print(download_model(sys.argv[1], local_files_only=True))")
        p = subprocess.run([str(tools["asr.whisper_python"]), "-c", probe, ASR_MODEL], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        hint = "" if p.returncode == 0 else (f" (download it once: \"{tools['asr.whisper_python'].as_posix()}\" -c "
                                             f"\"from faster_whisper import WhisperModel; WhisperModel('{ASR_MODEL}')\")")
        need(f"faster-whisper '{ASR_MODEL}' cached locally", p.returncode == 0,
             (p.stdout + p.stderr).strip()[-200:] + hint)
    bad = [k for k, v in checks.items() if not v]
    if bad:
        raise Fail("doctor: " + "; ".join(bad))
    print("NARRATE_JSON " + json.dumps({"command": "doctor", "ok": True, "engine": eng, "checks": len(checks)}))


# ------------------------------------------------------------------ main

def cmd_retrim(args) -> None:
    """higgsfield: clean every ready take again from the raw file Higgsfield returned (clean_take) and run the take
    checks again. Costs nothing. A take that now fails a check (cut off at the end, the speech check, a padded take's
    strict match) keeps its audio for now, so the video still builds, and is marked for a retake: `tts-plan` then asks
    for a new one."""
    run = Path(args.run).resolve()
    script = Path(args.script).resolve() if args.script else run / "script.json"
    lex = load_pronunciations(run)
    segs = load_script(script, lex)
    if engine() != "higgsfield":
        raise Fail(f"{cfg.where('narration.engine')} is {engine()!r}, not 'higgsfield'")
    voice = voice_key("higgsfield", run)
    st = load_state(run)
    groups: dict[int, list[dict]] = {}
    marked_before = [s["id"] for s in segs if (st["segments"].get(s["id"]) or {}).get("retake")]
    segs = [s for s in segs if s["id"] not in marked_before]
    for s in segs:
        if seg_state(run, s, st, voice) != "ready":
            raise Fail(f"{s['id']}: take is not ready ({seg_state(run, s, st, voice)}); finish the takes first")
        take = st["segments"][s["id"]]["takes"]
        raw = run / "narration" / "_hf" / f"take{take}" / f"{s['id']}_raw.wav"
        if not raw.is_file():
            raise Fail(f"{s['id']}: the raw take {raw} is missing; it cannot be cleaned again")
        groups.setdefault(take, []).append(s)
    before = {s["id"]: st["segments"][s["id"]]["samples"] for s in segs}
    failures: dict[str, str] = {}
    retake = []
    hfst = st.setdefault("hf", {})
    for take, group in sorted(groups.items()):
        takes_dir = run / "narration" / "_hf" / f"take{take}"
        raws = {s["id"]: takes_dir / f"{s['id']}_raw.wav" for s in group}
        old = {s["id"]: dict(st["segments"][s["id"]]) for s in group}
        padded = frozenset(s["id"] for s in group if st["segments"][s["id"]].get("padded"))
        still = check_takes(run, st, group, raws, take, takes_dir, lex, voice, None, failures, padded)
        for s in still:          # the old record comes back (check_takes never overwrote the wav of a failing take)
            st["segments"][s["id"]] = old[s["id"]]
            # keep the audio so the video still builds, but the sentence is not ready until a new take passes
            st["segments"][s["id"]]["retake"] = failures[s["id"]]
            prev = hfst.get(s["id"]) or {}
            hfst[s["id"]] = {"take": take, "state": "rejected", "spoken_sha": s["spoken_sha"],
                             "failures": prev.get("failures", []) + [failures[s["id"]]],
                             "pad_tried": bool(prev.get("pad_tried") or old[s["id"]].get("padded"))}
            retake.append(s["id"])
        write_json(state_path(run), st)
    after = {s["id"]: st["segments"][s["id"]]["samples"] for s in segs}
    changed = [i for i in after if after[i] != before[i]]
    print("RETRIM_JSON " + json.dumps({"command": "retrim", "segments": len(segs) + len(marked_before),
                                       "length_changed": changed, "retake": retake + marked_before,
                                       "failures": {k: failures[k] for k in retake},
                                       "next": ("tts-plan: record the sentences marked for a retake, then "
                                                "narrate.py run") if retake or marked_before
                                               else "narrate.py run (rebuild the narration track and alignment)"}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("run")
    r.add_argument("--run", required=True)
    r.add_argument("--script", help="default RUN/script.json")
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    s.add_argument("--script", help="default RUN/script.json")
    g = sub.add_parser("reading")
    g.add_argument("--run", required=True)
    g.add_argument("--script", help="default RUN/script.json")
    c = sub.add_parser("clone-voice", help="ElevenLabs Instant Voice Cloning from one sample (env ELEVENLABS_API_KEY)")
    c.add_argument("--sample", required=True, help="1-2 minutes of your clean speech (wav/mp3/m4a)")
    c.add_argument("--name", default="Scribeboard narrator", help="the voice's name in your ElevenLabs Voices")
    c.add_argument("--remove-background-noise", action="store_true")
    w = sub.add_parser("_worker")
    w.add_argument("--job", required=True)
    t = sub.add_parser("_tts")
    t.add_argument("--batch", required=True)
    a = sub.add_parser("_asr")
    a.add_argument("--batch", required=True)
    a.add_argument("--out", required=True)
    wd = sub.add_parser("_words")
    wd.add_argument("--wav", required=True)
    wd.add_argument("--out", required=True)
    tp = sub.add_parser("tts-plan", help="higgsfield: the generate_audio requests for every sentence needing a take")
    tp.add_argument("--run", required=True)
    tp.add_argument("--script", help="default RUN/script.json")
    ti = sub.add_parser("tts-ingest", help="higgsfield: record job ids, download and check finished takes")
    ti.add_argument("--run", required=True)
    ti.add_argument("--script", help="default RUN/script.json")
    ti.add_argument("--results", required=True, help="JSON list of {id, job_id, quoted, url | failed | unknown}")
    rt = sub.add_parser("retrim", help="higgsfield: clean every ready take again from its raw file and re-check it (free)")
    rt.add_argument("--run", required=True)
    rt.add_argument("--script", help="default RUN/script.json")
    rc = sub.add_parser("recheck", help="higgsfield: check rejected takes again from their raw files (free)")
    rc.add_argument("--run", required=True)
    rc.add_argument("--script", help="default RUN/script.json")
    yp = sub.add_parser("try-plan", help="higgsfield: trial takes of one sentence, one per spelling of a word")
    yp.add_argument("--run", required=True)
    yp.add_argument("--script", help="default RUN/script.json")
    yp.add_argument("--id", required=True, help="sentence id")
    yp.add_argument("--word", required=True, help="the word as written in the sentence")
    yp.add_argument("--say", required=True, action="append", help="a spelling to try (repeat, up to 6)")
    ys = sub.add_parser("try-submitted", help="higgsfield: record a submitted trial job")
    ys.add_argument("--run", required=True)
    ys.add_argument("--script", help="default RUN/script.json")
    ys.add_argument("--id", required=True)
    ys.add_argument("--word", required=True)
    ys.add_argument("--say", required=True)
    ys.add_argument("--job", required=True)
    ys.add_argument("--quoted", type=float, required=True)
    ya = sub.add_parser("try-adopt", help="higgsfield: use the trial take the user picked as the sentence's take")
    ya.add_argument("--run", required=True)
    ya.add_argument("--script", help="default RUN/script.json")
    ya.add_argument("--id", required=True)
    ya.add_argument("--say", required=True)
    ya.add_argument("--url", required=True, help="the trial job's result url (jobs_wait)")
    args = ap.parse_args()
    handlers = {"doctor": cmd_doctor, "run": cmd_run, "status": cmd_status, "reading": cmd_reading,
                "clone-voice": cmd_clone_voice, "_worker": cmd_worker, "_tts": cmd_tts, "_asr": cmd_asr,
                "_words": cmd_words, "tts-plan": cmd_tts_plan, "tts-ingest": cmd_tts_ingest, "retrim": cmd_retrim,
                "recheck": cmd_recheck, "try-plan": cmd_try_plan, "try-submitted": cmd_try_submitted, "try-adopt": cmd_try_adopt}
    try:
        handlers[args.cmd](args)
    except Fail as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:  # unexpected: still fail loud in the contract format
        import traceback
        traceback.print_exc()
        print(f"FAILED {args.cmd}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
