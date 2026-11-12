r"""checks.py - the checks shared by install.py and the doctor commands (stdlib only).

Each check returns a plain dict and never installs anything.

  stamp(path)                    a file's fingerprint: {"size", "sha256"}, or {"size", "mtime_ns"} above
                                 STAMP_HASH_MAX bytes (model weights, FFmpeg builds: hashing them on every run
                                 would cost seconds), so the fast check stays fast
  stamp_ok(path, recorded)       the file exists and still matches its recorded stamp
  probe_env(python)              import every package the skill uses inside that interpreter; {"ok", "versions"} or
                                 {"ok": False, "error"}
  probe_ffmpeg(exe)              {"ok", "version", "missing": [...]}: libx264, aac, libmp3lame encoders and the
                                 loudnorm filter are required
  ffmpeg_candidates()            FFmpeg executables found on PATH (deduplicated, resolved)
  probe_whisper(python, dir)     the faster-whisper model in dir loads on the CPU (int8, local files only)
  mcp_registrations(url)         [{"harness", "config", "name"}]: every harness config that names the MCP url
                                 (Claude Code ~/.claude.json, Codex ~/.codex/config.toml, omp ~/.omp/agent/mcp.json;
                                 omp also reads ~/.claude.json, which is reported as harness "claude-code")
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import oslib

STAMP_HASH_MAX = 64 * 1024 * 1024
ENV_IMPORTS = ("PIL", "numpy", "scipy", "cv2", "skimage", "markdown_it", "faster_whisper", "ctranslate2", "av",
               "imageio_ffmpeg")
FFMPEG_ENCODERS = ("libx264", "aac", "libmp3lame")
FFMPEG_FILTERS = ("loudnorm",)


def stamp(p: Path) -> dict:
    st = p.stat()
    if st.st_size > STAMP_HASH_MAX:
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"size": st.st_size, "sha256": h.hexdigest()}


def stamp_ok(p: Path, recorded: dict | None) -> bool:
    if not recorded or not p.is_file():
        return False
    try:
        return stamp(p) == recorded
    except OSError:
        return False


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=oslib.NO_WINDOW)


def probe_env(python: Path) -> dict:
    if not python.is_file():
        return {"ok": False, "error": f"no interpreter at {python}"}
    code = ("import importlib, json, sys\n"
            "out = {'python': sys.version.split()[0]}\n"
            f"for m in {list(ENV_IMPORTS)!r}:\n"
            "    mod = importlib.import_module(m)\n"
            "    out[m] = getattr(mod, '__version__', '?')\n"
            "print(json.dumps(out))")
    try:
        p = _run([str(python), "-c", code], 180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{python} did not run: {exc}"}
    if p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or ["(no output)"])[-1]
        return {"ok": False, "error": f"import failed in {python}: {tail}"}
    return {"ok": True, "versions": json.loads(p.stdout.strip().splitlines()[-1])}


def probe_ffmpeg(exe: Path) -> dict:
    if not exe.is_file():
        return {"ok": False, "error": f"no FFmpeg at {exe}", "missing": []}
    try:
        ver = _run([str(exe), "-hide_banner", "-version"], 30)
        enc = _run([str(exe), "-hide_banner", "-encoders"], 30)
        fil = _run([str(exe), "-hide_banner", "-filters"], 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{exe} did not run: {exc}", "missing": []}
    m = re.search(r"ffmpeg version (\S+)", ver.stdout)
    enc_names = {ln.split()[1] for ln in enc.stdout.splitlines() if len(ln.split()) > 1}
    fil_names = {ln.split()[1] for ln in fil.stdout.splitlines() if len(ln.split()) > 1}
    missing = [e for e in FFMPEG_ENCODERS if e not in enc_names] + [f for f in FFMPEG_FILTERS if f not in fil_names]
    return {"ok": not missing and ver.returncode == 0, "version": m.group(1) if m else None, "missing": missing}


def ffmpeg_candidates() -> list[Path]:
    found = []
    p = oslib.which("ffmpeg")
    if p:
        found.append(p.resolve())
    return list(dict.fromkeys(found))


def probe_whisper(python: Path, model_dir: Path) -> dict:
    if not (model_dir / "model.bin").is_file():
        return {"ok": False, "error": f"no model.bin in {model_dir}"}
    code = ("import sys\nfrom faster_whisper import WhisperModel\n"
            "WhisperModel(sys.argv[1], device='cpu', compute_type='int8', local_files_only=True)\nprint('ok')")
    try:
        p = _run([str(python), "-c", code, str(model_dir)], 300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"the model did not load: {exc}"}
    if p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or ["(no output)"])[-1]
        return {"ok": False, "error": f"the model did not load: {tail}"}
    return {"ok": True}


def _urls(servers) -> list[tuple[str, str]]:
    if not isinstance(servers, dict):
        return []
    return [(name, s.get("url", "")) for name, s in servers.items() if isinstance(s, dict)]


def _same(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def mcp_registrations(url: str) -> list[dict]:
    out = []
    claude = oslib.home() / ".claude.json"
    if claude.is_file():
        try:
            doc = json.loads(claude.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        pools = [doc.get("mcpServers")] + [p.get("mcpServers") for p in (doc.get("projects") or {}).values()
                                           if isinstance(p, dict)]
        for pool in pools:
            for name, u in _urls(pool):
                if _same(u, url):
                    out.append({"harness": "claude-code", "config": str(claude), "name": name})
    codex = oslib.home() / ".codex" / "config.toml"
    if codex.is_file():
        try:
            import tomllib
            doc = tomllib.loads(codex.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        for name, u in _urls(doc.get("mcp_servers")):
            if _same(u, url):
                out.append({"harness": "codex", "config": str(codex), "name": name})
    omp = oslib.home() / ".omp" / "agent" / "mcp.json"
    if omp.is_file():
        try:
            doc = json.loads(omp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        for name, u in _urls(doc.get("mcpServers")):
            if _same(u, url):
                out.append({"harness": "omp", "config": str(omp), "name": name})
    seen, uniq = set(), []
    for r in out:
        key = (r["harness"], r["config"], r["name"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq
