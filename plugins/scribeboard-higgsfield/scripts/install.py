r"""install.py - the checks and installs behind the setup wizard (/scribeboard-higgsfield:setup, or the plugin's
skills/setup/SKILL.md read straight after `claude plugin install`). Stdlib only, so any Python 3.10+ runs it.

The wizard asks the user about every missing item in chat, one plain question each (the `ask` block below), and
runs `install` only for the items the user said yes to. `install` refuses without that answer (--approved) and
records it, the command it ran and the result in DATA/install.json.

  python install.py check
      CHECK_JSON {ok, platform, data_dir, settings_file, items: [...]}. Each item:
        {id, label, required, state, found, detail, why, ask}
      state: ok | missing | broken (present but unusable) | restart (installed; Claude Code must restart to see it)
             | choice (optional item found, waiting for the user to pick)
      ask: null when nothing is needed, else {question, header, options: [{label, description, choice}]}; a
      "skip" choice means the user does it themselves, following the item's `manual` steps.
  python install.py install --item ID --choice CHOICE --approved "the user's answer" [--path P]
      INSTALL_JSON {item, choice, done, restart, next}. Runs one fix; never a "skip" choice. Choice "path"
      (python, ffmpeg: "I already have it") checks the copy at P and adds its folder to the user's PATH.
  python install.py status
      STATUS_JSON: install.json as it is.

Items, in the order the wizard asks them:
  python      Python 3.12+ as `python` on PATH (the plugin's hooks call it; the scripts run on it)
  ffmpeg      ffmpeg and ffprobe on PATH, with libx264, aac, libmp3lame and loudnorm
  packages    the Python packages the scripts import (settings `python`) and faster-whisper (`asr.whisper_python`)
  whisper     the faster-whisper "medium" model for the speech check (about 1.5 GB, downloaded once)
  upscaler    Real-ESRGAN ncnn Vulkan, which enlarges a picture that comes back small (45 MB; settings realesrgan_dir)
  higgsfield  the Higgsfield MCP server registered in Claude Code (whether it is signed in, the agent checks: balance)
  chrome      optional: Google Chrome for the gate pages; without it they open in the default browser

DATA is the per-user folder (settings.py _user_dir: %LOCALAPPDATA%\scribe-higgsfield on Windows; the environment
variable SCRIBE_HF_DATA moves it, for tests). Its settings.json holds this machine's paths; `install` changes only
the keys it names there and keeps every other key. Installs that need a package manager use winget on Windows;
elsewhere they FAIL with the command to run by hand. Any failure prints FAILED <reason> and exits 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks  # noqa: E402
import oslib  # noqa: E402
import settings as cfg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MCP_URL = "https://mcp.higgsfield.ai/mcp"
MIN_PY = (3, 12)
MAIN_IMPORTS = ("PIL", "numpy", "scipy", "cv2", "skimage", "markdown_it")   # what the scripts import
WHISPER_IMPORTS = ("faster_whisper",)
WHISPER_MODEL = "medium"   # narrate.py ASR_MODEL
WINGET = {   # item: winget package id (Windows)
    "python": ["Python.Python.3.12", "--override", "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1"],
    "ffmpeg": ["Gyan.FFmpeg"],
}
ESRGAN = {"url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
                 "realesrgan-ncnn-vulkan-20220424-windows.zip",
          "size": 45474481, "sha256": "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d"}
CHROME_PLACES = ("%ProgramFiles%/Google/Chrome/Application/chrome.exe",
                 "%ProgramFiles(x86)%/Google/Chrome/Application/chrome.exe",
                 "%LOCALAPPDATA%/Google/Chrome/Application/chrome.exe",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SKIP = {"label": "I'll do it myself", "choice": "skip"}


class Fail(Exception):
    pass


# ------------------------------------------------------------------ small helpers


def data_dir() -> Path:
    return cfg._user_dir()


def settings_file() -> Path:
    return cfg.LOCAL


def record_path() -> Path:
    return data_dir() / "install.json"


def requirements() -> Path:
    for name in ("requirements.txt", "requirements-higgsfield.txt"):   # plugin / development repo
        if (ROOT / name).is_file():
            return ROOT / name
    raise Fail(f"no requirements file in {ROOT}")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(cmd: list[str], what: str, timeout: float = 3600) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=oslib.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Fail(f"{what}: {e}")
    if p.returncode != 0:
        tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-10:])
        raise Fail(f"{what} failed (exit {p.returncode}):\n{tail}")
    return p.stdout


def shown(cmd: list[str]) -> str:
    return " ".join(f'"{c}"' if " " in c else c for c in cmd)


def setting(key: str) -> Path | None:
    cfg.load.cache_clear()
    return cfg.path(key)


def py_info(exe: Path | str) -> dict | None:
    """{version: (major, minor), executable} of an interpreter, or None when it does not run (the Windows Store
    stub in WindowsApps prints nothing and fails)."""
    try:
        p = subprocess.run([str(exe), "-c", "import sys; print(sys.version_info[0], sys.version_info[1], "
                            "sys.executable)"], capture_output=True, text=True, timeout=30,
                           creationflags=oslib.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = (p.stdout or "").strip().split(" ", 2)
    if p.returncode != 0 or len(parts) != 3:
        return None
    return {"version": (int(parts[0]), int(parts[1])), "executable": parts[2]}


def missing_imports(python: Path, modules: tuple[str, ...]) -> list[str] | None:
    """The modules that python cannot import; None when python does not run."""
    code = ("import importlib, json, sys\nbad = []\nfor m in sys.argv[1:]:\n    try:\n"
            "        importlib.import_module(m)\n    except Exception:\n        bad.append(m)\nprint(json.dumps(bad))")
    try:
        p = subprocess.run([str(python), "-c", code, *modules], capture_output=True, text=True, timeout=300,
                           creationflags=oslib.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return json.loads(p.stdout.strip().splitlines()[-1])


def load_record() -> dict:
    p = record_path()
    if not p.is_file():
        return {"installs": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(f"cannot read {p}: {e}")


def save_record(doc: dict) -> None:
    p = record_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def write_settings(updates: dict[str, str]) -> Path:
    """Set dotted keys (asr.whisper_python) in the per-user settings.json, keeping every other key."""
    p = settings_file()
    doc = {"_comment": "this machine's paths for the scribe-higgsfield plugin (written by the setup wizard; "
                       "overrides the plugin's scribe-settings.json key by key)"}
    if p.is_file():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Fail(f"cannot read {p} ({e}); fix or delete it, then run the setup again")
        if not isinstance(doc, dict):
            raise Fail(f"{p} must hold a JSON object")
    for key, value in updates.items():
        if key not in cfg.KEYS:
            raise Fail(f"unknown settings key {key}")
        node, parts = doc, key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise Fail(f"{p}: {part} is not an object")
        node[parts[-1]] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    cfg.load.cache_clear()
    return p


def posix(p: Path | str) -> str:
    return str(p).replace("\\", "/")


# ------------------------------------------------------------------ where things are


def path_python() -> dict | None:
    """`python` on PATH, if it runs."""
    found = oslib.which("python")
    return py_info(found) if found else None


def installed_python() -> Path | None:
    """A Python 3.12 that winget (or python.org's installer) put in its usual place, on PATH or not."""
    if oslib.system() != "windows":
        return None
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    for cand in sorted(base.glob("Python3*/python.exe"), reverse=True):
        info = py_info(cand)
        if info and info["version"] >= MIN_PY:
            return cand
    return None


def scripts_python() -> Path | None:
    """The Python the scripts run on: settings `python`, else `python` on PATH."""
    p = setting("python")
    if p:
        return p
    info = path_python()
    return Path(info["executable"]) if info else None


def whisper_python() -> Path | None:
    return setting("asr.whisper_python") or scripts_python()


def winget_ffmpeg() -> Path | None:
    """ffmpeg.exe that winget installed but this session's PATH does not see yet."""
    if oslib.system() != "windows":
        return None
    link = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    return link if link.is_file() else None


def find_chrome() -> Path | None:
    for c in CHROME_PLACES:
        p = Path(os.path.expandvars(c))
        if "%" not in str(p) and p.is_file():
            return p
    return None


# ------------------------------------------------------------------ check


def item(id_: str, label: str, required: bool, state: str, found, detail: str, why: str, ask=None) -> dict:
    return {"id": id_, "label": label, "required": required, "state": state,
            "found": posix(found) if found else None, "detail": detail, "why": why,
            "ask": None if state == "ok" else ask, "manual": manual(id_) if id_ in CHECKS else ""}


def manual(id_: str) -> str:
    """What the user does by hand, for a "skip" answer or anything Claude cannot do: real paths, no placeholders
    that differ from what the plugin reads."""
    data, sf = data_dir(), settings_file()
    env_py = oslib.venv_python(data / "env")
    return {
        "python": "Download Python 3.12 from https://www.python.org/downloads/ and run the installer with 'Add "
                  "python.exe to PATH' ticked. Open a new terminal: `python --version` must print 3.12 or later.",
        "ffmpeg": "In a terminal: `winget install --exact --id Gyan.FFmpeg` (or download a full build from "
                  "https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to your PATH). Open a new terminal: "
                  "`ffmpeg -version` and `ffprobe -version` must both run.",
        "packages": f"In a terminal: `python -m venv \"{data / 'env'}\"`, then `\"{env_py}\" -m pip install "
                    f"--only-binary :all: -r \"{ROOT / 'requirements.txt'}\"`, and set `python` and "
                    f"`asr.whisper_python` to \"{posix(env_py)}\" in {sf}.",
        "whisper": f"In a terminal: `\"{whisper_python() or env_py}\" -c \"from faster_whisper.utils import "
                   f"download_model; download_model('{WHISPER_MODEL}')\"` (1.5 GB, once).",
        "upscaler": f"Download {ESRGAN['url']}, unzip it to {data / 'realesrgan'} and set `realesrgan_dir` to "
                    f"\"{posix(data / 'realesrgan')}\" in {sf}.",
        "higgsfield": f"In a terminal: `claude mcp add --transport http --scope user higgsfield {MCP_URL}`. Restart "
                      "Claude Code, run /mcp, pick higgsfield and sign in.",
        "chrome": f"Optional: set `chrome` in {sf} to the full path of chrome.exe, or leave it out to use your "
                  "default browser.",
    }[id_]


def have_it(what: str, wants: str) -> dict:
    return {"label": "I already have it", "choice": "path",
            "description": f"Tell me where {wants} is (its folder or full path). I'll check that it works and add "
                           f"that folder to your PATH. Claude Code must restart afterwards."}


def check_python() -> dict:
    why = "Runs the plugin's scripts and its credit guard (the hooks call `python`)."
    info = path_python()
    if info and info["version"] >= MIN_PY:
        return item("python", "Python 3.12+", True, "ok", info["executable"],
                    f"Python {info['version'][0]}.{info['version'][1]}", why)
    later = installed_python()
    if later:
        return item("python", "Python 3.12+", True, "restart", later,
                    f"installed at {posix(later)} but not on this session's PATH; restart Claude Code", why)
    have = f"`python` is Python {info['version'][0]}.{info['version'][1]}" if info else "no `python` on PATH"
    return item("python", "Python 3.12+", True, "missing", None, have, why, {
        "header": "Python",
        "question": f"I couldn't find Python 3.12 or newer ({have}). Do you already have it, or should I install "
                    "it for you?",
        "options": [{"label": "Install it for me (Recommended)", "choice": "install",
                     "description": "I'll run winget to install Python 3.12 for your user only (about 30 MB) and "
                                    "add it to your PATH. Claude Code must restart afterwards."},
                    have_it("Python", "python.exe (3.12 or newer)"),
                    {**SKIP, "description": "I'll give you the steps."}]})


def check_ffmpeg() -> dict:
    why = "Cuts the narration and builds the video."
    exe, probe = oslib.which("ffmpeg"), None
    if exe:
        probe = checks.probe_ffmpeg(exe.resolve())
        if probe["ok"] and oslib.which("ffprobe"):
            return item("ffmpeg", "FFmpeg", True, "ok", exe, f"FFmpeg {probe.get('version')}", why)
    later = winget_ffmpeg()
    if later and not exe:
        return item("ffmpeg", "FFmpeg", True, "restart", later,
                    "installed but not on this session's PATH; restart Claude Code", why)
    if exe and not probe["ok"]:
        state, have = "broken", f"the FFmpeg at {posix(exe)} lacks {', '.join(probe['missing']) or 'a working run'}"
    elif exe:
        state, have = "broken", f"FFmpeg is at {posix(exe)} but ffprobe is not on PATH"
    else:
        state, have = "missing", "no `ffmpeg` on PATH"
    question = (f"I couldn't find FFmpeg ({have}). Do you already have it, or should I install it for you?"
                if state == "missing" else
                f"FFmpeg is needed, but {have}. Should I install a full build for you?")
    return item("ffmpeg", "FFmpeg", True, state, exe, have, why, {
        "header": "FFmpeg", "question": question,
        "options": [{"label": "Install it for me (Recommended)", "choice": "install",
                     "description": "I'll run winget to install the full FFmpeg build (about 100 MB) and put ffmpeg "
                                    "and ffprobe on your PATH. Claude Code must restart afterwards."},
                    *([have_it("FFmpeg", "ffmpeg.exe (with ffprobe.exe beside it)")] if state == "missing" else []),
                    {**SKIP, "description": "I'll give you the steps."}]})


def check_packages() -> dict:
    why = "Draws the animation (Pillow, NumPy, SciPy, OpenCV, scikit-image), builds the gate pages (markdown-it) " \
          "and checks every narration take (faster-whisper)."
    main, wpy = scripts_python(), whisper_python()
    if not main:
        return item("packages", "Python packages", True, "missing", None, "needs Python first", why, {
            "header": "Packages", "question": "Python packages: install Python first, then run the setup again.",
            "options": [SKIP | {"description": "Python comes first."}]})
    bad_main = missing_imports(main, MAIN_IMPORTS)
    bad_whisper = missing_imports(wpy, WHISPER_IMPORTS) if wpy else None
    if bad_main == [] and bad_whisper == []:
        where = posix(main) + ("" if wpy == main else f"; faster-whisper in {posix(wpy)}")
        return item("packages", "Python packages", True, "ok", main, f"all present ({where})", why)
    gone = (bad_main if bad_main is not None else [f"({posix(main)} does not run)"]) + \
        (bad_whisper if bad_whisper is not None else [f"faster_whisper ({posix(wpy)} does not run)"])
    have = f"{posix(main)} lacks {', '.join(gone)}"
    return item("packages", "Python packages", True, "missing", main, have, why, {
        "header": "Packages",
        "question": f"Some Python packages are missing ({', '.join(gone)}). How should they be installed?",
        "options": [{"label": "Private environment (Recommended)", "choice": "private",
                     "description": f"Creates a separate Python environment in {posix(data_dir() / 'env')} and "
                                    "installs the exact versions the plugin was tested with (about 700 MB). Your own "
                                    "Python packages are not touched."},
                    {"label": "Into my Python", "choice": "mine",
                     "description": f"pip installs the same versions into {posix(main)}; this can change versions "
                                    "of packages you already use."},
                    {**SKIP, "description": "I'll give you the commands to run."}]})


def check_whisper() -> dict:
    why = "Listens to every narration take and checks it says exactly its words."
    wpy = whisper_python()
    if not wpy or missing_imports(wpy, WHISPER_IMPORTS) != []:
        return item("whisper", "Speech-check model", True, "missing", None, "needs the Python packages first", why, {
            "header": "Speech model",
            "question": "The speech-check model needs the Python packages first. Download it once they are in?",
            "options": [{"label": "Download it (Recommended)", "choice": "install",
                         "description": f"Downloads the faster-whisper '{WHISPER_MODEL}' model (about 1.5 GB) "
                                        "into your Hugging Face cache, once."},
                         {**SKIP, "description": "I'll give you the command."}]})
    code = ("import sys\nfrom faster_whisper.utils import download_model\n"
            "print(download_model(sys.argv[1], local_files_only=True))")
    try:
        out = run([str(wpy), "-c", code, WHISPER_MODEL], "looking for the model", timeout=120).strip()
        return item("whisper", "Speech-check model", True, "ok", out.splitlines()[-1], f"'{WHISPER_MODEL}' cached",
                    why)
    except Fail:
        pass
    return item("whisper", "Speech-check model", True, "missing", None, f"'{WHISPER_MODEL}' is not downloaded", why, {
        "header": "Speech model",
        "question": f"The speech-check model ('{WHISPER_MODEL}', about 1.5 GB) is not downloaded. Download it now?",
        "options": [{"label": "Download it (Recommended)", "choice": "install",
                     "description": "Downloads it once into your Hugging Face cache; it runs on your computer."},
                    {**SKIP, "description": "I'll give you the command; every narration check fails until the "
                                            "model is there."}]})


def check_upscaler() -> dict:
    why = "Enlarges a picture that comes back smaller than the video needs."
    d = setting("realesrgan_dir")
    exe = d / oslib.exe_name("realesrgan-ncnn-vulkan") if d else None
    if exe and exe.is_file() and (d / "models" / "realesrgan-x4plus.param").is_file():
        return item("upscaler", "Picture upscaler", True, "ok", d, "Real-ESRGAN ncnn Vulkan", why)
    have = f"settings realesrgan_dir = {posix(d)} has no {exe.name if exe else ''} with its models" if d else \
        "not installed"
    return item("upscaler", "Picture upscaler", True, "missing", d, have, why, {
        "header": "Upscaler",
        "question": "The picture upscaler (Real-ESRGAN) is not installed. Download it now?",
        "options": [{"label": "Download it (Recommended)", "choice": "install",
                     "description": f"Downloads Real-ESRGAN ncnn Vulkan from its GitHub release (45 MB, checked "
                                    f"against a pinned hash) into {posix(data_dir() / 'realesrgan')}."},
                    {**SKIP, "description": "I'll tell you where to download it and which setting to change."}]})


def check_higgsfield() -> dict:
    why = "Makes every picture and every narration take (needs a paid Higgsfield plan)."
    regs = [g for g in checks.mcp_registrations(MCP_URL) if g["harness"] == "claude-code"]
    if regs:
        return item("higgsfield", "Higgsfield MCP", True, "ok", regs[0]["config"],
                    f"registered as '{regs[0]['name']}'; sign-in is checked next", why)
    return item("higgsfield", "Higgsfield MCP", True, "missing", None, "not registered in Claude Code", why, {
        "header": "Higgsfield",
        "question": "Higgsfield is not connected to Claude Code. Add the Higgsfield MCP server now?",
        "options": [{"label": "Add it (Recommended)", "choice": "install",
                     "description": f"Runs `claude mcp add --transport http --scope user higgsfield {MCP_URL}`. "
                                    "After a restart you sign in once with /mcp."},
                    {**SKIP, "description": "I'll give you the command. The Higgsfield connector in the Claude "
                                            "app alone is not enough."}]})


def check_chrome() -> dict:
    why = "Opens the gate pages in a Chrome tab; without it they open in your default browser."
    have = setting("chrome")
    if have and have.is_file():
        return item("chrome", "Google Chrome (optional)", False, "ok", have, "set in settings", why)
    found = find_chrome()
    if not found:
        return item("chrome", "Google Chrome (optional)", False, "ok", None,
                    "not found: gate pages open in your default browser", why)
    return item("chrome", "Google Chrome (optional)", False, "choice", found,
                f"found at {posix(found)}, not chosen yet", why, {
        "header": "Chrome",
        "question": f"Open the approval pages in Google Chrome ({posix(found)})?",
        "options": [{"label": "Use Chrome", "choice": "use", "description": "Saves its path in your settings."},
                    {"label": "Default browser", "choice": "skip",
                     "description": "The pages open in whatever browser opens web links."}]})


CHECKS = {"python": check_python, "ffmpeg": check_ffmpeg, "packages": check_packages, "whisper": check_whisper,
          "upscaler": check_upscaler, "higgsfield": check_higgsfield, "chrome": check_chrome}


def cmd_check(_a=None) -> dict:
    items = [f() for f in CHECKS.values()]
    return {"ok": all(i["state"] == "ok" for i in items if i["required"]), "platform": oslib.system(),
            "data_dir": posix(data_dir()), "settings_file": posix(settings_file()), "items": items}


# ------------------------------------------------------------------ installs


def need_windows(what: str, manual: str) -> None:
    if oslib.system() != "windows":
        raise Fail(f"{what} installs itself on Windows only; on this system: {manual}")


def winget(item_id: str) -> list[str]:
    exe = oslib.which("winget")
    if not exe:
        raise Fail("winget is not available (Windows 10 1809+ with App Installer); install it from the Microsoft "
                   "Store, or install this item yourself")
    pkg, extra = WINGET[item_id][0], WINGET[item_id][1:]
    return [str(exe), "install", "--exact", "--id", pkg, "--scope", "user", "--silent",
            "--accept-package-agreements", "--accept-source-agreements", *extra]


def add_to_user_path(folders: list[Path]) -> list[str]:
    """Put folders at the front of the user's PATH (Windows: HKCU\\Environment), so new processes find them first
    (ahead of the Microsoft Store's python stub). Returns the folders added; ones already there are left alone."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in current.split(";") if p]
        have = {os.path.normcase(os.path.expandvars(p)).rstrip("\\/") for p in parts}
        new = [str(f) for f in folders if os.path.normcase(str(f)).rstrip("\\/") not in have]
        if new:
            winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                              else winreg.REG_EXPAND_SZ, ";".join(new + parts))
    if new:   # tell running programs (Explorer, new terminals) that the environment changed
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    return new


def given_exe(path: str | None, name: str) -> Path:
    """The executable the user pointed at: a file, or a folder holding name(.exe)."""
    if not path or not path.strip():
        raise Fail(f"--path must name the {name} the user already has (its folder or full path)")
    p = Path(os.path.expandvars(path.strip().strip("\"'"))).expanduser()
    if p.is_dir():
        p = p / oslib.exe_name(name)
    if not p.is_file():
        raise Fail(f"no {oslib.exe_name(name)} at {posix(p)}")
    return p


def fix_python(choice: str, path: str | None = None) -> dict:
    if choice == "path":
        exe = given_exe(path, "python")
        info = py_info(exe)
        if not info:
            raise Fail(f"{posix(exe)} does not run")
        if info["version"] < MIN_PY:
            raise Fail(f"{posix(exe)} is Python {info['version'][0]}.{info['version'][1]}; 3.12 or newer is needed")
        need_windows("Adding Python to PATH", f"add {posix(exe.parent)} to your PATH yourself")
        added = add_to_user_path([exe.parent, exe.parent / "Scripts"])
        return {"done": f"Python {info['version'][0]}.{info['version'][1]} at {posix(exe)}; "
                        + (f"added to your PATH: {', '.join(added)}" if added else "already on your PATH"),
                "command": "user PATH (HKCU\\Environment)", "restart": True}
    need_windows("Python", "install Python 3.12 from python.org (or your package manager) so `python` runs it")
    cmd = winget("python")
    run(cmd, "installing Python 3.12 with winget", timeout=1800)
    later = installed_python()
    if not later:
        raise Fail("winget finished but no Python 3.12 is in %LOCALAPPDATA%\\Programs\\Python")
    return {"done": f"Python 3.12 at {posix(later)}", "command": shown(cmd), "restart": True}


def fix_ffmpeg(choice: str, path: str | None = None) -> dict:
    if choice == "path":
        exe = given_exe(path, "ffmpeg")
        if not (exe.parent / oslib.exe_name("ffprobe")).is_file():
            raise Fail(f"no {oslib.exe_name('ffprobe')} beside {posix(exe)}; both are needed")
        probe = checks.probe_ffmpeg(exe.resolve())
        if not probe["ok"]:
            raise Fail(f"the FFmpeg at {posix(exe)} lacks {', '.join(probe['missing']) or 'a working run'}; "
                       f"choose 'Install it for me' instead")
        need_windows("Adding FFmpeg to PATH", f"add {posix(exe.parent)} to your PATH yourself")
        added = add_to_user_path([exe.parent])
        return {"done": f"FFmpeg {probe.get('version')} at {posix(exe)}; "
                        + (f"added to your PATH: {', '.join(added)}" if added else "already on your PATH"),
                "command": "user PATH (HKCU\\Environment)", "restart": True}
    need_windows("FFmpeg", "install FFmpeg with your package manager (brew install ffmpeg / apt install ffmpeg)")
    cmd = winget("ffmpeg")
    run(cmd, "installing FFmpeg with winget", timeout=1800)
    exe = winget_ffmpeg() or oslib.which("ffmpeg")
    if not exe:
        raise Fail("winget finished but ffmpeg.exe is not in %LOCALAPPDATA%\\Microsoft\\WinGet\\Links")
    probe = checks.probe_ffmpeg(Path(exe).resolve())
    if not probe["ok"]:
        raise Fail(f"the installed FFmpeg lacks {', '.join(probe['missing'])}")
    return {"done": f"FFmpeg {probe.get('version')} at {posix(exe)}", "command": shown(cmd), "restart": True}


def base_python() -> Path:
    """A Python 3.12+ to build the private environment with: `python` on PATH, else the newly installed one."""
    info = path_python()
    if info and info["version"] >= MIN_PY:
        return Path(info["executable"])
    later = installed_python()
    if later:
        return later
    raise Fail("no Python 3.12+ found: install Python first")


def fix_packages(choice: str) -> dict:
    req = requirements()
    if choice == "private":
        env = data_dir() / "env"
        cmds = [[str(base_python()), "-m", "venv", "--clear", str(env)]]
        run(cmds[0], "creating the private environment", timeout=600)
        py = oslib.venv_python(env)
        cmds.append([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary", ":all:",
                     "-r", str(req)])
        run(cmds[1], "installing the packages", timeout=3600)
    else:
        py = scripts_python() or base_python()
        info = py_info(py)
        if not info or info["version"] < MIN_PY:
            raise Fail(f"{posix(py)} is not Python 3.12+")
        in_venv = run([str(py), "-c", "import sys; print(sys.prefix != sys.base_prefix)"], "checking the Python")
        cmds = [[str(py), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary", ":all:",
                 *([] if in_venv.strip() == "True" else ["--user"]), "-r", str(req)]]
        run(cmds[0], "installing the packages", timeout=3600)
    bad = missing_imports(py, MAIN_IMPORTS + WHISPER_IMPORTS)
    if bad != []:
        raise Fail(f"after the install {posix(py)} still cannot import {bad}")
    written = write_settings({"python": posix(py), "asr.whisper_python": posix(py)})
    return {"done": f"packages in {posix(py)}; settings python and asr.whisper_python set in {posix(written)}",
            "command": " && ".join(shown(c) for c in cmds), "restart": False}


def fix_whisper(_choice: str) -> dict:
    wpy = whisper_python()
    if not wpy or missing_imports(wpy, WHISPER_IMPORTS) != []:
        raise Fail("faster-whisper is not installed yet: install the Python packages first")
    code = "import sys\nfrom faster_whisper.utils import download_model\nprint(download_model(sys.argv[1]))"
    cmd = [str(wpy), "-c", code, WHISPER_MODEL]
    out = run(cmd, f"downloading the '{WHISPER_MODEL}' model", timeout=7200).strip().splitlines()[-1]
    return {"done": f"'{WHISPER_MODEL}' in {posix(out)}", "command": f"{posix(wpy)} -c download_model('{WHISPER_MODEL}')",
            "restart": False}


def fix_upscaler(_choice: str) -> dict:
    need_windows("The upscaler", f"download the Real-ESRGAN ncnn Vulkan build for your system from "
                                 f"https://github.com/xinntao/Real-ESRGAN/releases and set realesrgan_dir")
    dest = data_dir() / "realesrgan"
    zpath = data_dir() / "realesrgan.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(ESRGAN["url"], timeout=120) as r, zpath.open("wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        raise Fail(f"downloading {ESRGAN['url']}: {e}")
    data = zpath.read_bytes()
    if len(data) != ESRGAN["size"] or hashlib.sha256(data).hexdigest() != ESRGAN["sha256"]:
        zpath.unlink()
        raise Fail(f"the download does not match the pinned size and SHA-256; nothing was installed")
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise Fail(f"the zip holds a path outside its folder: {info.filename}")
        z.extractall(dest)
    zpath.unlink()
    if not (dest / oslib.exe_name("realesrgan-ncnn-vulkan")).is_file():
        raise Fail(f"{dest} has no realesrgan-ncnn-vulkan after unpacking")
    written = write_settings({"realesrgan_dir": posix(dest)})
    return {"done": f"Real-ESRGAN in {posix(dest)}; realesrgan_dir set in {posix(written)}",
            "command": f"download {ESRGAN['url']} (SHA-256 checked) and unzip", "restart": False}


def fix_higgsfield(_choice: str) -> dict:
    claude = oslib.which("claude")
    if not claude:
        raise Fail("the `claude` command is not on PATH; add the server yourself: "
                   f"claude mcp add --transport http --scope user higgsfield {MCP_URL}")
    cmd = [str(claude), "mcp", "add", "--transport", "http", "--scope", "user", "higgsfield", MCP_URL]
    run(cmd, "adding the Higgsfield MCP server", timeout=120)
    if not [g for g in checks.mcp_registrations(MCP_URL) if g["harness"] == "claude-code"]:
        raise Fail("`claude mcp add` finished but no Higgsfield entry appeared in ~/.claude.json")
    return {"done": "Higgsfield MCP registered (user scope)", "command": shown(cmd), "restart": True,
            "next": "restart Claude Code, run /mcp, pick higgsfield and sign in"}


def fix_chrome(_choice: str) -> dict:
    found = find_chrome()
    if not found:
        raise Fail("Chrome was not found in its usual places")
    written = write_settings({"chrome": posix(found)})
    return {"done": f"chrome = {posix(found)} in {posix(written)}", "command": "", "restart": False}


FIXES = {"python": fix_python, "ffmpeg": fix_ffmpeg, "packages": fix_packages, "whisper": fix_whisper,
         "upscaler": fix_upscaler, "higgsfield": fix_higgsfield, "chrome": fix_chrome}


def cmd_install(a) -> dict:
    if not cfg.PLUGIN:
        raise Fail("the setup wizard configures the installed plugin (its per-user settings.json); run it from "
                   "the plugin, not from the development repo")
    if a.item not in FIXES:
        raise Fail(f"unknown item {a.item!r} (known: {', '.join(FIXES)})")
    approved = (a.approved or "").strip()
    if not approved:
        raise Fail("--approved must hold the user's answer to this item's question; nothing is installed without it")
    st = CHECKS[a.item]()
    if st["state"] == "ok":
        return {"item": a.item, "choice": a.choice, "done": "already present: " + st["detail"], "restart": False}
    choices = [o["choice"] for o in (st["ask"] or {}).get("options", []) if o["choice"] != "skip"]
    if a.choice not in choices:
        raise Fail(f"--choice for {a.item} must be one of {', '.join(choices) or '(none: nothing to install)'}; "
                   f"'skip' means the user installs it")
    rec = load_record()
    entry = {"item": a.item, "choice": a.choice, "approved": approved, "at": now(),
             **({"path": a.path} if getattr(a, "path", "") else {})}
    try:
        result = FIXES[a.item](a.choice, a.path) if a.item in ("python", "ffmpeg") else FIXES[a.item](a.choice)
        entry.update(result, ok=True)
    except Fail as e:
        entry.update(ok=False, error=str(e))
        raise
    finally:
        rec.setdefault("installs", []).append(entry)
        save_record(rec)
    return {"item": a.item, "choice": a.choice, **result}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    i = sub.add_parser("install")
    i.add_argument("--item", required=True)
    i.add_argument("--choice", default="install")
    i.add_argument("--approved", default="")
    i.add_argument("--path", default="", help="choice 'path': the folder or file of the Python/FFmpeg the user has")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if a.cmd == "check":
            print("CHECK_JSON " + json.dumps(cmd_check(a)))
        elif a.cmd == "install":
            print("INSTALL_JSON " + json.dumps(cmd_install(a)))
        else:
            print("STATUS_JSON " + json.dumps(load_record()))
    except Fail as e:
        print(f"FAILED {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
