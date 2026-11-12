r"""settings.py - the one source of machine paths and engine switches for every Scribeboard script.

Files, read in this order; a later file overrides an earlier one key by key:
  1. REPO/settings.json         committed neutral defaults for a fresh machine; it holds every key (KEYS)
  2. REPO/settings.local.json   this machine's values (gitignored; optional)
  3. the file named by the environment variable SCRIBEBOARD_SETTINGS (optional)
Nested objects group keys (`render.drivers.chatgpt`); keys starting with "_" are comments. A key that KEYS does not
know is refused, so a typo never silently does nothing. A path may use %VARS% and ~; a relative path resolves
against the folder of the file that sets it. null = not set (each key's meaning of "not set" is in settings.json).

Library: python(), powershell(), path(key), get(key), source(key), where(key), lines(keys), missing(keys), override(),
command(exe, *args).

Command line (stdlib only, so any Python can run it):
  python scripts/settings.py python    print the scripts' Python (setting `python` after the whole file chain, else
                                       the interpreter running this command) as the start of a command line that
                                       runs in PowerShell and bash (command(): forward slashes, no quotes, an 8.3
                                       short path instead of spaces), nothing else.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Installed as a Claude Code plugin (REPO/.claude-plugin/plugin.json): the plugin root's settings.json belongs to
# Claude Code, so the defaults are scribe-settings.json, and this machine's values live outside the plugin (plugin
# updates replace its folder): the per-user app folder's settings.json.
PLUGIN = (REPO / ".claude-plugin" / "plugin.json").is_file()


def _user_dir() -> Path:
    if os.environ.get("SCRIBE_HF_DATA"):   # tests and trial installs: a throwaway per-user folder
        return Path(os.environ["SCRIBE_HF_DATA"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "scribe-higgsfield"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "scribe-higgsfield"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "scribe-higgsfield"


DEFAULTS = REPO / ("scribe-settings.json" if PLUGIN else "settings.json")
LOCAL = _user_dir() / "settings.json" if PLUGIN else REPO / "settings.local.json"
ENV = "SCRIBEBOARD_SETTINGS"

PATH, BOOL, TEXT, NUM = "path", "bool", "text", "number"
KEYS = {   # every setting and its kind; what each one means, and what null means for it, is in settings.json
    "python": PATH, "powershell": PATH, "chrome": PATH, "styles_dir": PATH,
    "gpu_queue": PATH, "gpu_queue_log": PATH,
    "engines.qwen": BOOL, "comfy_host": TEXT,
    "render.engine_registry": PATH, "render.prompt_meta_dir": PATH,
    "render.drivers.chatgpt": PATH, "render.drivers.qwen_t2i": PATH, "render.drivers.qwen_ref": PATH,
    "realesrgan_dir": PATH,
    "html_questions": PATH, "md_questions_legacy": PATH,
    "tts.qwen_python": PATH, "tts.qwen_synth": PATH, "tts.reference_wav": PATH, "tts.reference_text": PATH,
    "tts.presence_eq": PATH,
    "tts.elevenlabs.voice_id": TEXT, "tts.elevenlabs.model_id": TEXT, "tts.elevenlabs.output_format": TEXT,
    "tts.elevenlabs.base_url": TEXT,
    "tts.openai.model": TEXT, "tts.openai.voice": TEXT, "tts.openai.instructions": TEXT, "tts.openai.base_url": TEXT,
    "asr.whisper_python": PATH,
    "narration.engine": TEXT,
    "voice_profile": PATH, "pronunciations": PATH, "runs_home": PATH,
    "images.engine": TEXT,
    "higgsfield.voice_model": TEXT, "higgsfield.voice_id": TEXT,
    "higgsfield.max_credits": NUM, "higgsfield.image_credits": NUM, "higgsfield.audio_credits": NUM,
}


class SettingsError(Exception):
    pass


def _flatten(doc: dict, src: Path, prefix: str = "") -> dict[str, tuple]:
    out = {}
    for k, v in doc.items():
        if k.startswith("_"):
            continue
        key = prefix + k
        if isinstance(v, dict):
            out.update(_flatten(v, src, key + "."))
        elif key not in KEYS:
            raise SettingsError(f"{src}: unknown key {key!r} (known: {', '.join(KEYS)})")
        else:
            out[key] = (v, src)
    return out


def _resolve(key: str, value, src: Path):
    kind = KEYS[key]
    if value is None:
        return None
    if kind == BOOL:
        if not isinstance(value, bool):
            raise SettingsError(f"{src}: {key} must be true or false, not {value!r}")
        return value
    if kind == NUM:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise SettingsError(f"{src}: {key} must be a number >= 0 or null, not {value!r}")
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{src}: {key} must be a non-empty string or null, not {value!r}")
    if kind == TEXT:
        return value
    p = Path(os.path.expanduser(os.path.expandvars(value)))
    return p if p.is_absolute() else Path(os.path.normpath(src.parent / p))


@functools.cache
def load() -> dict[str, tuple]:
    """{key: (value, source file)}. Exits with a FAILED line when a settings file is unreadable or wrong."""
    try:
        files = [DEFAULTS, LOCAL] if LOCAL.is_file() else [DEFAULTS]
        if os.environ.get(ENV) and not PLUGIN:   # the repo's dev overlay never applies to an installed plugin
            env = Path(os.environ[ENV]).resolve()
            if not env.is_file():
                raise SettingsError(f"{ENV}={os.environ[ENV]} is not a file")
            os.environ[ENV] = str(env)   # children that inherit the environment read the same file from any cwd
            files.append(env)
        merged: dict[str, tuple] = {}
        for f in files:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise SettingsError(f"cannot read {f}: {e}")
            if not isinstance(doc, dict):
                raise SettingsError(f"{f} must hold a JSON object")
            for key, (v, src) in _flatten(doc, f).items():
                merged[key] = (_resolve(key, v, src), src)
            if f == DEFAULTS and len(merged) != len(KEYS):
                raise SettingsError(f"{DEFAULTS} lacks {', '.join(k for k in KEYS if k not in merged)}")
        return merged
    except SettingsError as e:
        raise SystemExit(f"FAILED settings: {e}")


def override() -> Path | None:
    """The SCRIBEBOARD_SETTINGS file in effect (absolute), or None. A process started without this process's
    environment (WMI: render_scenes.spawn_outside_job) must be handed it explicitly."""
    load()
    return Path(os.environ[ENV]) if os.environ.get(ENV) and not PLUGIN else None


def get(key: str):
    return load()[key][0]


def path(key: str) -> Path | None:
    if KEYS[key] != PATH:
        raise KeyError(f"{key} is not a path setting")
    return get(key)


def source(key: str) -> str:
    return load()[key][1].name if load()[key][1].parent == REPO else load()[key][1].as_posix()


def where(key: str) -> str:
    """'key (from file)' for messages."""
    return f"{key} in {source(key)}"


def python() -> Path:
    return path("python") or Path(sys.executable)


def powershell() -> Path | None:
    p = path("powershell")
    if p:
        return p
    found = shutil.which("pwsh") or shutil.which("powershell")
    return Path(found) if found else None


_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:=+%~")   # safe unquoted in pwsh and bash


def _short(p: str) -> str:
    """The 8.3 short form of an existing Windows path (no spaces), or p unchanged."""
    if os.name != "nt" or not os.path.exists(p):
        return p
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
    return buf.value if 0 < n < len(buf) else p


def command(exe, *args) -> str:
    """A command line to show an agent or a user that runs the same in PowerShell and in bash: forward slashes,
    double quotes only around arguments that need them, and an unquoted program (a quoted first word is a string
    in PowerShell). A program path with spaces is replaced by its 8.3 short form; when that has spaces too, the
    line starts with PowerShell's call operator `& ` (PowerShell only)."""
    def word(s) -> str:
        s = str(s).replace("\\", "/")
        return s if s and set(s) <= _PLAIN else '"' + s + '"'

    prog = str(exe).replace("\\", "/")
    if not set(prog) <= _PLAIN:
        prog = _short(str(exe)).replace("\\", "/")
    head = prog if set(prog) <= _PLAIN else "& " + word(prog)
    return " ".join([head, *(word(a) for a in args)])


def lines(keys=None) -> list[str]:
    """One 'key = value  (file)' line per setting, for doctor output."""
    out = []
    for k in keys or KEYS:
        v = get(k)
        shown = "null (not set)" if v is None else (v.as_posix() if isinstance(v, Path) else json.dumps(v))
        out.append(f"{k} = {shown}  ({source(k)})")
    return out


def missing(keys=None) -> list[str]:
    """A problem line for every configured path that does not exist."""
    return [f"{k} = {path(k).as_posix()} ({source(k)}) does not exist"
            for k in keys or KEYS if KEYS[k] == PATH and get(k) is not None and not path(k).exists()]


if __name__ == "__main__":
    if sys.argv[1:] != ["python"]:
        raise SystemExit("usage: python settings.py python")
    sys.stdout.reconfigure(newline="\n")   # no CR on Windows: shells capture the line verbatim
    print(command(python()))
