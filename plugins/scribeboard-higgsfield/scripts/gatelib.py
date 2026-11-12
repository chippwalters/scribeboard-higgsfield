"""Shared plumbing for gate0.py, gate1.py, gate2.py and gate3.py (contract section `Gates`).

Gate pages are html-questions pages (one self-contained HTML file per round) built from a generated spec. Every
round joins the run's gate rail, RUN/gates/gates.js (one spot per gate, the newest round of each). Nothing here
renders HTML.
"""
from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import settings as cfg   # scripts/settings.py: python, html_questions, md_questions_legacy, chrome
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

# Legacy only: rounds built with md-questions before the switch to html-questions (their gate.json has `markdown`
# and `backlotManifest`) still validate their answers with md_questions read (settings md_questions_legacy).
# Nothing else uses md-questions.
GATE_POINTER = "gate.json"  # which bundle in the round folder is the live one, plus the answer map
GATES_MANIFEST = "gates.js"  # RUN/gates/gates.js: the rail across Gate 0-3 shown on every gate page
GATE_LABELS = {0: "Gate 0 · Narration", 1: "Gate 1 · Images", 2: "Gate 2 · Board", 3: "Gate 3 · Video",
               5: "Start",                     # gate5: setup runs only, the first page (script, voice, budget)
               6: "Style · Scene 1"}           # gate6: setup runs only, scene 1 approved before the other pictures
# scribe-higgsfield (runsetup.required()): the order the user sees, folder number -> (rail spot, label). The folders
# keep their numbers (gate0 = the narration/setup page, gate1 = images, ...); only the rail and page titles change.
# There is no board page (gate2): the board is built and shown on the video page, Gate 3.
SETUP_RAIL = {5: (0, "Gate 0 · Start"), 0: (1, "Gate 1 · Script & setup"), 6: (2, "Style · Scene 1"),
              1: (3, "Gate 2 · Images"), 3: (4, "Gate 3 · Video")}


def rail_spot(n: int) -> tuple[int, str]:
    import runsetup   # imported here: runsetup is only needed for the scribe-higgsfield rail
    if runsetup.required():
        if n not in SETUP_RAIL:
            raise Fail(f"gate{n} has no page in a scribe-higgsfield run (its pages: "
                       f"{', '.join(f'gate{k}' for k in SETUP_RAIL)}); follow scribe.py status")
        return SETUP_RAIL[n]
    return n, GATE_LABELS[n]
ROUND_DIR = re.compile(r"^gate(\d)-r\d+$")
DECISION = [
    {"value": "approve", "label": "Approve"},
    {"value": "edit", "label": "Edit (say what in the note)"},
]


def decision_options(previous: str | None = None) -> list[dict]:
    """DECISION with the user's previous answer preselected, for an item shown again exactly as it was answered
    (the same sentences, the same sheet). None (a changed or new item) leaves both unselected."""
    return [{**o, "selected": o["value"] == previous} for o in DECISION]


class Fail(Exception):
    pass


def load_json(path: Path, what: str):
    if not path.is_file():
        raise Fail(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Fail(f"{what} is not valid JSON ({path}): {exc}") from exc


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def require_file(path: Path, what: str) -> Path:
    if not path.is_file():
        raise Fail(f"{what} not found: {path}")
    return path


def timecode(seconds: float) -> str:
    """m:ss.ss, e.g. 1:02.35."""
    minutes, rest = divmod(float(seconds), 60.0)
    return f"{int(minutes)}:{rest:05.2f}"


def span(start: float, end: float) -> str:
    return f"{timecode(start)} – {timecode(end)} ({end - start:.2f} s)"


def load_script(run: Path) -> dict:
    doc = load_json(run / "script.json", "script.json")
    scenes = doc.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise Fail("script.json has no scenes")
    for scene in scenes:
        if not isinstance(scene.get("id"), int) or not scene.get("title") or not scene.get("segments"):
            raise Fail(f"script.json scene {scene.get('id')!r} needs an integer id, a title and segments")
    return doc


def paragraph(scene: dict) -> str:
    return " ".join(seg["text"].strip() for seg in scene["segments"])


def gate_dir(run: Path, gate: str, rnd: int) -> Path:
    if rnd < 1:
        raise Fail(f"--round must be >= 1, got {rnd}")
    return run / "gates" / f"{gate}-r{rnd}"


def image_sha(run: Path, sid: int) -> str:
    """sha256 of the scene's selected image (scenes/scene_N_4k.png); '' when it is missing."""
    p = run / "scenes" / f"scene_{sid}_4k.png"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def earlier_approvals(run: Path, gate: str, rnd: int) -> dict[int, int]:
    """scene id -> the round it was first approved in, from every earlier round's changes.json. An approval holds
    only for the image it was given to: changes.json `approved_sha256` must name the current scene_N_4k.png."""
    approved: dict[int, int] = {}
    current: dict[int, str] = {}
    for r in range(1, rnd):
        changes = load_json(gate_dir(run, gate, r) / "changes.json",
                            f"{gate} round {r} changes.json (apply round {r} before building round {rnd})")
        if changes.get("round") != r:
            raise Fail(f"{gate} round {r} changes.json says round {changes.get('round')!r}")
        shas = changes.get("approved_sha256", {})
        for sid in changes.get("approved_scenes", []):
            sid = int(sid)
            if sid not in current:
                current[sid] = image_sha(run, sid)
            if current[sid] and shas.get(str(sid)) == current[sid]:
                approved.setdefault(sid, r)
    return approved


def approved_intro(scenes: list[dict], approved: dict[int, int]) -> str:
    """Collapsed entries for scenes approved in earlier rounds (no question)."""
    rows = []
    for scene in scenes:
        r = approved.get(scene["id"])
        if r is None:
            continue
        rows.append(f"<details><summary>Scene {scene['id']}: {html.escape(scene['title'])} — approved in round {r}"
                    f"</summary><p>{html.escape(paragraph(scene))}</p></details>")
    return "\n".join(rows)


def _tool(key: str, what: str) -> Path:
    """The html-questions (or legacy md-questions) script from settings."""
    p = cfg.path(key)
    if p is None:
        raise Fail(f"{what}: {key} is not set in settings (settings.local.json)")
    if not p.is_file():
        raise Fail(f"{what}: {p} ({cfg.where(key)}) does not exist")
    return p


def _script(key: str, *args: str, what: str) -> dict:
    """Run an html-questions (or legacy md-questions) command; its stdout is one JSON object."""
    proc = subprocess.run([str(cfg.python()), str(_tool(key, what)), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise Fail(f"{what} failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Fail(f"{what} printed no JSON: {proc.stdout.strip()[:300]}") from exc


def _stem(pointer: dict) -> str:
    return Path(pointer["bundle"]).name.removesuffix(".bundle.json")


def is_legacy(pointer: dict) -> bool:
    """A round built with md-questions (before html-questions)."""
    return "markdown" in pointer or "backlotManifest" in pointer


def unanswered_stem(out: Path) -> str | None:
    """The file stem of the round's current page when the round was built but not answered (a rebuild replaces it);
    None for a round never built. A round with an answers file or a changes.json is refused."""
    if (out / "changes.json").exists():
        raise Fail(f"{out} was already applied (changes.json exists); build the next round instead")
    if not (out / GATE_POINTER).is_file():
        return None
    pointer = load_json(out / GATE_POINTER, "gate pointer")
    if pointer.get("expectedAnswers") and (out / Path(pointer["expectedAnswers"]).name).is_file():
        raise Fail(f"round {pointer.get('round')} has answers ({out / Path(pointer['expectedAnswers']).name}); "
                   f"apply it instead of rebuilding it")
    return _stem(pointer) if pointer.get("bundle") else None


def run_title(run: Path) -> str:
    """The rail's title: the story title from script.json or narration.json, else the run folder's name."""
    for name in ("script.json", "narration.json"):
        p = run / name
        if p.is_file():
            try:
                title = json.loads(p.read_text(encoding="utf-8")).get("title")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(title, str) and title.strip():
                return title.strip()
    return run.name


def build_bundle(spec: dict, out: Path, meta: dict) -> dict:
    """Write spec.json into the round folder, build the html-questions page there (joining the run's gate rail,
    RUN/gates/gates.js, as this gate's spot, state waiting), record the live bundle (plus `meta`, what apply needs
    to map answers back) in gate.json. Rebuilding an unanswered round deletes its previous page's files."""
    m = ROUND_DIR.match(out.name)
    if not m or out.parent.name != "gates" or int(m.group(1)) not in GATE_LABELS:
        raise Fail(f"{out} is not a gate round folder (RUN/gates/gate<N>-r<R>)")
    n = int(m.group(1))
    rail_spot(n)   # a page the run's skill does not have fails here, before anything is written
    out.mkdir(parents=True, exist_ok=True)
    old_stem = unanswered_stem(out)
    pos, label = rail_spot(n)
    if pos != n or label != GATE_LABELS[n]:   # scribe-higgsfield order: the page's own title follows the rail
        spec = {**spec, "title": re.sub(rf"^Gate {n}\b", label.split(" · ")[0], spec.get("title", "")),
                "stageLabel": label.split(" · ")[0]}
    spec = {**spec, "gate": {"manifest": f"../{GATES_MANIFEST}", "n": pos, "label": label,
                             "title": run_title(out.parent.parent)}}
    spec_path = out / "spec.json"
    write_json(spec_path, spec)
    result = _script("html_questions", "build", "--spec", str(spec_path), "--out", str(out), what="html_questions build")
    for key in ("bundle", "html", "url", "expectedAnswers"):
        if key not in result:
            raise Fail(f"html_questions build result lacks {key}")
    require_file(Path(result["html"]), "generated page")
    require_file(Path(result["bundle"]), "generated bundle")
    write_json(out / GATE_POINTER, {**result, **meta})
    new_stem = _stem(result)
    if old_stem and old_stem != new_stem:   # a rebuilt unanswered round: drop the replaced page's files
        for f in out.glob(f"{old_stem}*"):
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
    return {**result, "rebuilt": bool(old_stem)}


def _render_scenes():
    """render_scenes.py as a module: its WMI launcher (spawn_outside_job) is reused, never copied."""
    spec = importlib.util.spec_from_file_location("render_scenes", Path(__file__).with_name("render_scenes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def open_page(url: str, skip: bool) -> dict:
    """Open a gate page in a new tab of the user's Chrome (else the default browser). Chrome is started through
    WMI (render_scenes.spawn_outside_job), so it is no child of this script and survives the agent's turn; its
    window shows normally (SW_SHOWNORMAL), no console. Returns {"opened": bool}, plus "open_error" when the
    launch failed: the page is written either way, so a failed launch never fails the build."""
    if skip:
        return {"opened": False}
    rs = _render_scenes()
    chrome = cfg.path("chrome")
    try:
        if chrome and chrome.is_file():
            rs.spawn_outside_job(subprocess.list2cmdline([str(chrome), "--new-tab", url]), chrome.parent,
                                 show_window=1, what="Chrome")
        else:
            os.startfile(url)
    except (OSError, rs.Fail) as exc:
        return {"opened": False, "open_error": f"could not open the page ({exc}); open {url} by hand"}
    return {"opened": True}


def read_answers(out: Path) -> tuple[dict, dict]:
    """Validate the pasted answers (written to the bundle's expectedAnswers path). Returns (responses, gate pointer);
    the pointer's `gateState` is the page's rail state after the read (None for a legacy page or one a newer round
    of its gate has replaced on the rail)."""
    pointer = load_json(out / GATE_POINTER, "gate pointer (run build first)")
    bundle = out / Path(pointer["bundle"]).name
    answers = require_file(out / Path(pointer["expectedAnswers"]).name,
                           "answers file (paste the copied JSON to this exact path)")
    if is_legacy(pointer):   # a round built with md-questions before the switch: validate it the old way
        result = _script("md_questions_legacy", "read", "--bundle", str(bundle), "--answers", str(answers),
                         what="md_questions read (a legacy round, built with md-questions before html-questions)")
    else:
        result = _script("html_questions", "read", "--bundle", str(bundle), "--answers", str(answers),
                         what="html_questions read")
    return result["responses"], {**pointer, "gateState": result.get("gateState")}


def mark_gate(out: Path, pointer: dict, passed: bool) -> str | None:
    """After apply: this round's rail dot turns `accepted` when the round passed the gate, `answered` when it asked for
    edits (their colours: html-questions page.css). Returns the state, or None when the page holds no rail spot
    (legacy, or replaced)."""
    if pointer.get("gateState") is None:
        return None
    state = "accepted" if passed else "answered"
    if pointer["gateState"] == state:
        return state
    result = _script("html_questions", "mark", "--bundle", str(out / Path(pointer["bundle"]).name), "--state", state,
                     what="html_questions mark")
    return result["state"]


def response(responses: dict, qid: str) -> dict:
    if qid not in responses:
        raise Fail(f"answers lack question {qid}")
    return responses[qid]


def decision(responses: dict, qid: str) -> tuple[str, str]:
    r = response(responses, qid)
    if r.get("value") not in ("approve", "edit"):
        raise Fail(f"question {qid} has no approve/edit answer: {r.get('value')!r}")
    return r["value"], r.get("note", "")


def emit(name: str, payload: dict) -> None:
    print(f"{name}_JSON " + json.dumps(payload, ensure_ascii=False), flush=True)


def run_main(fn, argv=None) -> int:
    try:
        return fn(argv)
    except Fail as exc:
        print(f"FAILED {exc}", file=sys.stderr, flush=True)
        return 1
