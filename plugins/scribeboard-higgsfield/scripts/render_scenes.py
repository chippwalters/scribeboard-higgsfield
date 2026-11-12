#!/usr/bin/env python
r"""render_scenes.py - Scribeboard scene-image renderer with a scored review loop
(spec phase 6). Driven by the /render-scribe skill.

The script owns every deterministic step, so no step depends on an agent
remembering it. Agents only do the two things a script cannot: render agents
run `render` (one attempt), review agents look at the result and fill in the
checklist that `verdict` scores.

  doctor       preflight: every setting and its file, configured paths, Codex login,
               ComfyUI (engines.qwen only), Real-ESRGAN, prompt stamping, GPU queue, style guide
  init         validate scenes.json (spec phase 3 rules), write prompts, create state
  status       per-scene table and the NEXT action for each scene (the loop driver)
  render       one attempt for one scene: ChatGPT (2 tries) -> Qwen-Image-2.1 fallback (engines.qwen),
               then flatten, frame to the output aspect, upscale to the 4K scene size;
               a `chatgpt+insert` scene: ChatGPT leaves empty spaces, Qwen-Image-2.1 draws
               the insert character into them, the new ink is composited onto ChatGPT's image
  render-prep / render-submitted / render-ingest
               images.engine 'higgsfield': the three-step handoff (scripts/hf.py) for one attempt: prep reserves
               it and prints the generate_image arguments, the agent submits them (quote first) and reports the job
               id, ingest downloads the finished picture and frames it like `render`
  review-prep  review sheet (full view, 1:1 quadrants, style reference) + checklist
  verdict      validate the reviewer's checklist, compute score and pass, log it;
               a pass selects the attempt, the 5th fail flags the best attempt
  regions      rebuild a selected scene's segment/arrow/residual masks and highlight images
  rebox        re-box a selected image (element boxes without connector arrows, arrows boxed
               on their own) with no re-render and no re-score: without --file it writes
               the box sheet, with --file it applies the review agent's boxes and rebuilds masks
  gate         every scene selected, 4K image present at the exact size, verdict
               matches the image it reviewed, renderer != reviewer
  report       contact sheet, run summary, open the 4K images
  gpu-jobs     internal: GPU work, inside a GPU-queue lease when settings gpu_queue is set

Engines are external drivers; settings.json (scripts/settings.py) says where they are:
  ChatGPT         render/engines/codex/codex_render.py                  render.drivers.chatgpt
  Qwen t2i        render/engines/comfy/comfy_render.py  (variant qwen21) render.drivers.qwen_t2i
  Qwen reference  edit-image/engines/qwen21_edit.py                     render.drivers.qwen_ref
A driver not set there comes from the /render registry (render.engine_registry). The Qwen engines run only with
engines.qwen true. Upscaler: Real-ESRGAN ncnn Vulkan (realesrgan-x4plus) in realesrgan_dir.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import hf                # scripts/hf.py: the Higgsfield handoff (images.engine 'higgsfield')
import palette           # scripts/palette.py: the run's colours fill the style's prompt blocks
import runsetup          # scripts/runsetup.py: the run's setup page answers (style, palette), required with Higgsfield
import settings as cfg   # scripts/settings.py: machine paths and engine switches
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

REPO = Path(__file__).resolve().parents[1]
STYLES = cfg.path("styles_dir")
PY = cfg.python()
QWEN_ON = cfg.get("engines.qwen")
ENGINE_ROLES = ("chatgpt", "qwen_t2i", "qwen_ref") if QWEN_ON else ("chatgpt",)
ENGINE_KEYS = {"chatgpt": "chatgpt", "qwen_t2i": "qwen-image-2.1", "qwen_ref": "qwen-image-2.1-edit"}
QWEN_T2I_VARIANT = "qwen21"       # comfy_render.py variant when render.drivers.qwen_t2i is set directly
COMFY_HOST = cfg.get("comfy_host")
REALESRGAN_DIR = cfg.path("realesrgan_dir")
REALESRGAN_EXE_NAME = "realesrgan-ncnn-vulkan.exe"
REALESRGAN_MODEL = "realesrgan-x4plus"

MAX_ATTEMPTS = 5                  # per scene; then the best attempt is flagged
PASS_SCORE = 80
SCENE_LONG_SIDE = 3840            # spec phase 2
FRAME_MARGIN = 0.05               # white margin on each side when framing a cut-out
AI_UPSCALE_ABOVE = 1.5            # scale factors above this use Real-ESRGAN, below it Lanczos
INK_THRESHOLD = 245               # a pixel darker than this in any channel is ink
CHATGPT_TRIES = 2                 # engine failures before the Qwen fallback
STALE_RENDER_MIN = 120           # backstop: a "rendering" attempt older than this is failed even if a process looks alive
QWEN_SEED_BASE = 42
QWEN_NATIVE = {"16:9": (2752, 1536), "9:16": (1536, 2752), "1:1": (2048, 2048),
               "4:3": (2400, 1792), "3:4": (1792, 2400)}
MAX_LABEL_WORDS = 5               # words per drawn label (D73)
MAX_SEGMENT_WORDS = 25            # spec phase 3/4: Qwen3-TTS level drift on longer reads
# Segment balance: one pen draws each segment's elements at the style's constant speed while its sentence is
# spoken, so a 1-word sentence gets ~0.4 s of drawing and a 17-word one ~6 s. Even sentences give even drawing.
# Words stand in for spoken time (numbers are spoken words). The voice profile's median beat is 11 words.
SEGMENT_TARGET_WORDS = (8, 14)    # the writer's aim per sentence
SEGMENT_BALANCE_WORDS = (6, 16)   # hard band for new narration (voice.py validate-narration, verdict fixes)

# Review rubric: fixed deductions. The reviewer answers the checklist; the
# score is computed here, never typed in by the reviewer.
DEDUCT = {"missing_element": 20, "bad_label": 20, "extra_text": 10, "extra_object": 5,
          "hand_or_pen_visible": 15, "frame_or_border": 15, "background_not_plain_white": 15,
          "photographic_or_3d": 15, "gradients_or_grey_wash": 15, "element_cut_off": 15,
          "elements_touching": 10, "layout_not_followed": 10, "style_differs_from_reference": 10,
          "character_mismatch": 20, "density_off": 0}
STYLE_KEYS = ["hand_or_pen_visible", "frame_or_border", "background_not_plain_white",
              "photographic_or_3d", "gradients_or_grey_wash", "element_cut_off"]
KEY_STYLE = {"hand_or_pen_visible", "element_cut_off"}   # any of these fails the attempt outright

REF_QWEN = ("<image1> is a blank white canvas: draw the new scene on it, filling the canvas. <image2> is a "
            "style reference only: match its line weight, lettering, colour use and drawing style exactly, "
            "but do not copy any of its content, characters or layout. ")

# engine "chatgpt+insert" (a character ChatGPT will not draw): ChatGPT leaves empty white space, Qwen draws him in
EMPTY_SPACE = "a gap of plain blank paper"   # stands in for his name in ChatGPT's prompt; never hint at a figure (D76)
INSERT_QWEN = ("In <image1>, draw the character shown in <image2> into each empty white space: {where}. Same character "
               "exactly as <image2>, sized to fit each space, standing on the same ground line as the figures next to it. "
               "Match <image1>'s black chisel-marker outlines, stroke weight and flat colour fills. Change nothing else "
               "in <image1>.")
INSERT_WHITE = 235                # ChatGPT pixel this light in every channel is empty paper
INSERT_INK = 215                  # Qwen pixel darker than this in any channel is ink
INSERT_NEAR_PX = 4                # new ink this close to ChatGPT's ink is a shifted redraw of an existing line
INSERT_FAR_SHARE = 0.5            # a kept component has at least this share of its new ink farther than that
INSERT_MIN_AREA = 0.0004          # a kept component has at least this share of the image's pixels as new ink
INSERT_MAX_AREA = 0.25            # kept ink over this share of the image means Qwen redrew the picture, not an insert
INSERT_CLOSE_PX = 2               # closing radius that joins one figure's strokes into one component
INSERT_GROW_PX = 2                # the kept components grow this much before pasting (anti-aliased edges)


class Fail(Exception):
    """A step failed. The message says which and why."""


def engine(role: str) -> dict:
    """Driver path (and variant) for an engine role: render.drivers.<role> when set, else the /render registry
    (render.engine_registry), read every time. A Qwen role fails while engines.qwen is false."""
    key = ENGINE_KEYS[role]
    if role not in ENGINE_ROLES:
        raise Fail(f"engine: {key} is off (engines.qwen = false in {cfg.source('engines.qwen')})")
    direct = cfg.path(f"render.drivers.{role}")
    if direct:
        if not direct.is_file():
            raise Fail(f"engine: {key} driver {direct} ({cfg.where(f'render.drivers.{role}')}) does not exist")
        return {"key": key, "driver": direct, "variant": QWEN_T2I_VARIANT if role == "qwen_t2i" else None}
    registry = cfg.path("render.engine_registry")
    if registry is None:
        raise Fail(f"engine: no driver for {key}: set render.drivers.{role} (or render.engine_registry) in settings")
    if not registry.is_file():
        raise Fail(f"engine: registry {registry} ({cfg.where('render.engine_registry')}) does not exist")
    reg = json.loads(registry.read_text(encoding="utf-8"))
    for e in reg["engines"] if isinstance(reg, dict) else reg:
        if e.get("key") == key:
            if not e.get("enabled"):
                raise Fail(f"engine: {key} is disabled in {registry}")
            path = (registry.parent / e["driver_path"]).resolve()
            if not path.is_file():
                raise Fail(f"engine: {key} driver {path} (from {registry}) does not exist")
            return {"key": key, "driver": path, "variant": e.get("variant")}
    raise Fail(f"engine: {key} not found in {registry}")


def say(msg: str) -> None:
    print(f"[render_scenes] {msg}", flush=True)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ state, lock, log

@contextlib.contextmanager
def locked(out: Path):
    lock = out / "state.lock"
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() - t0 > 120:
                raise Fail(f"state: {lock} held for 120 s; if no render_scenes.py is running, delete it")
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def load_state(out: Path) -> dict:
    p = out / "state.json"
    if not p.is_file():
        raise Fail(f"state: {p} not found; run `init` first")
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(out: Path, st: dict) -> None:
    tmp = out / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, out / "state.json")


def log_event(out: Path, event: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    with open(out / "run_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_summary(out: Path, st: dict) -> str:
    lines = ["# render-scribe run summary", "", f"Style `{st['style']}`, aspect {st['aspect']}, "
             f"scene size {st['scene_size'][0]}x{st['scene_size'][1]}. Updated {time.strftime('%Y-%m-%d %H:%M:%S')}.", "",
             "| Scene | Status | Attempts | Scores | Engines | Selected |", "|---|---|---|---|---|---|"]
    for sid in st["order"]:
        sc = st["scenes"][str(sid)]
        scores = ", ".join(str(a["verdict"]["score"]) if a.get("verdict") else "-" for a in sc["attempts"])
        engines = ", ".join(a.get("engine") or a["state"] for a in sc["attempts"])
        lines.append(f"| {sid} | {scene_status(st, sid)} | {len(sc['attempts'])} | {scores or '-'} | {engines or '-'} | "
                     f"{sc.get('selected') or '-'} |")
    fails = [(sid, a) for sid in st["order"] for a in st["scenes"][str(sid)]["attempts"]
             if a.get("verdict") and not a["verdict"]["pass"]]
    if fails:
        lines += ["", "## Failed attempts", ""]
        for sid, a in fails:
            v = a["verdict"]
            lines.append(f"- Scene {sid} attempt {a['k']}: score {v['score']}. {v['worstIssue']} Fix: {v['requiredFix']}")
    text = "\n".join(lines) + "\n"
    (out / "run_summary.md").write_text(text, encoding="utf-8")
    return text


# ------------------------------------------------------------------ validation + prompts

def one_sentence(text: str) -> bool:
    t = re.sub(r"\b(Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|etc|e\.g|i\.e|U\.S)\.(?=\s)", r"\1<dot>", text.strip())
    if not re.search(r"[.!?][\"')\]]*$", t):
        return False
    return len([p for p in re.split(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'])", t) if p.strip()]) == 1


def validate(doc: dict) -> list[dict]:
    errs = []
    scenes = doc.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise Fail("validate: scenes.json needs a non-empty 'scenes' list")
    ids = [s.get("id") for s in scenes]
    if len(set(ids)) != len(ids) or not all(isinstance(i, int) and i > 0 for i in ids):
        errs.append("scene ids must be unique positive integers")
    for s in scenes:
        sid = s.get("id")
        els, segs = s.get("elements") or [], s.get("segments") or []
        if not els:
            errs.append(f"scene {sid}: no elements")
        if not segs:
            errs.append(f"scene {sid}: no segments")
        if not str(s.get("layout_hint", "")).strip():
            errs.append(f"scene {sid}: missing layout_hint")
        if s.get("engine") not in (None, "auto", "qwen", "chatgpt+insert"):
            errs.append(f"scene {sid}: engine must be 'auto' (ChatGPT first, Qwen fallback), 'qwen' or 'chatgpt+insert', "
                        f"not {s.get('engine')!r}")
        elif s.get("engine") in ("qwen", "chatgpt+insert") and not QWEN_ON:
            errs.append(f"scene {sid}: engine {s['engine']!r} needs the local Qwen-Image engines, which are off on this "
                        f"machine (engines.qwen = false in {cfg.source('engines.qwen')}); use 'auto' (ChatGPT only), "
                        f"or turn engines.qwen on in settings.local.json")
        el_ids = [e.get("id") for e in els]
        if len(set(el_ids)) != len(el_ids):
            errs.append(f"scene {sid}: duplicate element ids")
        for e in els:
            if not str(e.get("desc", "")).strip():
                errs.append(f"scene {sid}: element {e.get('id')} has no desc")
            if e.get("text") is not None and len(str(e["text"]).split()) > MAX_LABEL_WORDS:
                errs.append(f"scene {sid}: label {e['text']!r} is over {MAX_LABEL_WORDS} words")
        revealed = []
        for g in segs:
            text = str(g.get("text", ""))
            if not one_sentence(text):
                errs.append(f"segment {g.get('id')}: must be exactly one sentence: {text!r}")
            nw = len(text.split())
            if nw > MAX_SEGMENT_WORDS:
                errs.append(f"segment {g.get('id')}: {nw} words, over {MAX_SEGMENT_WORDS}")
            elif not SEGMENT_BALANCE_WORDS[0] <= nw <= SEGMENT_BALANCE_WORDS[1]:
                print(f"WARNING segment {g.get('id')}: {nw} word{'s' * (nw != 1)}, outside the segment balance band "
                      f"{SEGMENT_BALANCE_WORDS[0]}-{SEGMENT_BALANCE_WORDS[1]} (its drawing will look rushed or slow)",
                      file=sys.stderr, flush=True)
            rv = g.get("reveals") or []
            if not rv:
                errs.append(f"segment {g.get('id')}: reveals no element")
            for r in rv:
                if r not in el_ids:
                    errs.append(f"segment {g.get('id')}: reveals unknown element {r!r}")
            revealed += rv
        for e in el_ids:
            if revealed.count(e) != 1:
                errs.append(f"scene {sid}: element {e!r} revealed {revealed.count(e)} times (must be exactly 1)")
    chars = doc.get("characters", [])
    cids = [c.get("id") for c in chars] if isinstance(chars, list) else []
    if not isinstance(chars, list) or len(set(cids)) != len(cids):
        errs.append("characters must be a list with unique ids")
    for c in chars if isinstance(chars, list) else []:
        if not all(str(c.get(k, "")).strip() for k in ("id", "name", "desc")):
            errs.append(f"character {c.get('id')!r}: needs id, name and desc")
        if c.get("reference") and not Path(c["reference"]).is_file():
            errs.append(f"character {c.get('id')!r}: reference {c['reference']} not found")
    cnames = {c.get("id"): str(c.get("name", "")) for c in chars} if isinstance(chars, list) else {}
    for s in scenes:
        for e in s.get("elements") or []:
            for cid in e.get("characters", []):
                if cid not in cids:
                    errs.append(f"scene {s.get('id')}: element {e.get('id')} shows undeclared character {cid!r}")
        ins = s.get("insert")
        if s.get("engine") == "chatgpt+insert":
            if not isinstance(ins, dict) or set(ins) - {"character", "where"}:
                errs.append(f"scene {s.get('id')}: engine chatgpt+insert needs insert = {{character, where}}")
            else:
                if ins.get("character") not in cids:
                    errs.append(f"scene {s.get('id')}: insert.character {ins.get('character')!r} is not declared in characters")
                w = ins.get("where")
                if not (isinstance(w, str) and w.strip() or isinstance(w, list) and w
                        and all(isinstance(x, str) and x.strip() for x in w)):
                    errs.append(f"scene {s.get('id')}: insert.where must describe the empty spot the character goes in, "
                                f"or list one description per spot")
                name = cnames.get(ins.get("character"), "").strip()
                for e in s.get("elements") or []:     # ChatGPT draws the labels, and never sees his name
                    if name and e.get("text") and re.search(rf"\b{re.escape(name)}(?:['\u2019]?s)?\b", str(e["text"]), re.I):
                        errs.append(f"scene {s.get('id')}: label {e['text']!r} names the insert character {name!r}")
        elif ins is not None:
            errs.append(f"scene {s.get('id')}: insert only goes with engine 'chatgpt+insert'")
    wb = doc.get("written_by")
    if wb is not None and not (isinstance(wb, str) and wb.strip()):
        errs.append("written_by must be the writer agent's name (a non-empty string)")
    voice = doc.get("voice")
    if voice is not None:
        if not isinstance(voice, dict) or set(voice) - {"profile", "opening", "signoff"}:
            errs.append("voice must be an object with only profile, opening and signoff")
        else:
            if "profile" in voice and not (isinstance(voice["profile"], str) and voice["profile"].strip()):
                errs.append("voice.profile must be the voice profile's path")
            errs += [f"voice.{k} must be true or false" for k in ("opening", "signoff")
                     if k in voice and not isinstance(voice[k], bool)]
    if errs:
        raise Fail("validate:\n  " + "\n  ".join(errs))
    names = {c["id"]: c["name"] for c in chars}   # element prompts name the character they show
    for s in scenes:
        for e in s["elements"]:
            e["_names"] = [names[cid] for cid in e.get("characters", [])]
        if s.get("insert"):
            s["insert"]["_name"] = names[s["insert"]["character"]]
    return sorted(scenes, key=lambda s: s["id"])


def style_block(style: str, run: Path | None = None) -> str:
    """The style guide's ```image-prompt block, its colour placeholders filled from the run's palette (the setup page's
    choice when setup is required, else the style's palette.json)."""
    try:
        return palette.prompt_block(STYLES, style, "image-prompt", run)
    except palette.PaletteError as e:
        raise Fail(f"prompts: {e}")


def runsetup_ok(run: Path) -> None:
    """With setup required: refuse while the run's setup is missing or invalid (before any other prep work)."""
    try:
        runsetup.require(run)
    except runsetup.SetupError as e:
        raise Fail(f"no paid job before the setup page is answered: {e}") from e


def cast_approved(run: Path) -> None:
    """With setup required: refuse while a character sheet is not approved on Gate 1 as it is now (gate4.cast_problem).
    Every scene that shows a character is drawn from its sheet, so no scene is set up or rendered before that."""
    if not runsetup.required():
        return
    import gate4   # imported here: gate4 imports gatelib, which imports this module lazily
    why = gate4.cast_problem(run)
    if why:
        raise Fail(f"cast: {why}")


def setup_style(run: Path, style: str) -> None:
    """With setup required, the run's style is the setup page's choice; any other --style is refused."""
    if not runsetup.required():
        return
    try:
        chosen = runsetup.style(run)
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    if style != chosen:
        raise Fail(f"style {style!r} is not the run's: its setup page chose {chosen!r} (RUN/setup.json)")


def sheeted_ids(run: Path, chars: list[dict]) -> set[str]:
    """The characters drawn from a character sheet: with setup required, those the setup page gave one (a sheet is
    optional, runsetup.sheet_ids); otherwise every declared character."""
    if runsetup.required():
        try:
            return set(runsetup.sheet_ids(run))
        except runsetup.SetupError as e:
            raise Fail(str(e)) from e
    return {c["id"] for c in chars}


def sheet_info(run: Path, chars: list[dict]) -> dict:
    """What element prompts need about the characters: which have a sheet, and each one's name and desc. Built when
    prompts are made (init, sync); never stored in the scenes, so the stored scenes stay validate()'s output."""
    return {"sheeted": sheeted_ids(run, chars),
            "info": {c["id"]: (c["name"], str(c.get("desc", "")).strip()) for c in chars}}


def check_insert_sheets(scenes: list[dict], sheet: dict) -> None:
    """A chatgpt+insert scene pastes its character from the sheet: refuse one whose character has no sheet."""
    bad = [f"scene {s['id']} ({s['insert']['character']})" for s in scenes
           if s.get("insert") and s["insert"]["character"] not in sheet["sheeted"]]
    if bad:
        raise Fail(f"engine chatgpt+insert needs the character's sheet, but the setup page chose no sheet for: "
                   f"{', '.join(bad)}; give the character a sheet or use another engine for the scene")


def element_phrase(e: dict, sheet: dict | None = None) -> str:
    """An element's prompt phrase. A character with a sheet is drawn "exactly as on the character sheet"; one
    without (sheets are optional) is described in the phrase itself, since no reference shows it. Without `sheet`,
    every named character has a sheet (validate()'s _names)."""
    desc = str(e["desc"]).strip()
    if sheet is None:
        names, described = e.get("_names") or [], []
    else:
        ids = e.get("characters", [])
        names = [sheet["info"][c][0] for c in ids if c in sheet["sheeted"]]
        described = [f"{sheet['info'][c][0]} ({sheet['info'][c][1]})" if sheet["info"][c][1] else sheet["info"][c][0]
                     for c in ids if c not in sheet["sheeted"]]
    lead = []
    if names:
        lead.append(f"{' and '.join(names)} (drawn exactly as on the character sheet)")
    if described:
        lead.append(" and ".join(described))
    if lead:
        desc = f"{'; '.join(lead)}: {desc}"
    if e.get("text") and f'"{e["text"]}"' not in desc:
        desc += f', labeled "{e["text"]}"'
    return desc


def scene_prompt(scene: dict, block: str, budget: dict | None = None, sheet: dict | None = None) -> str:
    ins = scene.get("insert")
    if ins:   # chatgpt+insert: ChatGPT's prompt asks for the empty spaces and never names the character (hide_insert)
        view = {**scene, "insert": None,
                "elements": [{**e, "_names": [n for n in e["_names"] if n != ins["_name"]],
                              "characters": [c for c in e.get("characters", []) if c != ins["character"]]}
                             for e in scene["elements"]]}
        return hide_insert(f"{scene_prompt(view, block, budget, sheet)} {empty_spaces(insert_spots(ins))}", scene)
    if not budget:
        elems = "; ".join(f"({i}) {element_phrase(e, sheet)}" for i, e in enumerate(scene["elements"], 1))
        return f"A single wide scene containing exactly these elements: {elems}. Layout: {scene['layout_hint'].strip()}. {block}"
    # With narration timing: one group per sentence, each sized by its share of the scene's drawing time,
    # so the pen can draw every group at the same speed while its sentence is spoken (density pass).
    els = {e["id"]: e for e in scene["elements"]}
    groups = []
    for i, g in enumerate(scene["segments"], 1):
        share = budget["segment_shares"][g["id"]]
        size = "a large, detailed" if share >= 0.35 else "a medium-sized" if share >= 0.2 else "a small, simple"
        items = "; ".join(element_phrase(els[r], sheet) for r in g["reveals"])
        groups.append(f"Group {i} ({size} group, about {round(share * 100)}% of all the line work in the picture): {items}")
    return (f"A single wide scene made of {len(groups)} groups of drawings, one per narrated sentence, arranged in reading "
            f"order, each group standing on its own with clear white space around it. " + ". ".join(groups) +
            f". Layout: {scene['layout_hint'].strip().rstrip('.')}. The amount of line work in each group must match its percentage: "
            f"the scene is drawn line by line at a steady pen speed over about {budget['duration_s']:.0f} seconds, and each "
            f"group is drawn while its sentence is spoken. {budget['tier_prompt']} {block}")


def hide_insert(text: str, scene: dict) -> str:
    """ChatGPT's safety system blocks or silently swaps some (copyrighted) characters, so on a chatgpt+insert scene its prompt
    never names the insert character: every mention of his name (and its plural or possessive) anywhere in the prompt (element
    descs, layout hint, where, a review fix) becomes EMPTY_SPACE. Labels naming him are refused by validate()."""
    ins = scene.get("insert")
    if not ins:
        return text
    return re.sub(rf"\b{re.escape(ins['_name'])}(?:['\u2019]?s)?\b", EMPTY_SPACE, text, flags=re.I)


def insert_spots(ins: dict) -> list[str]:
    """insert.where as a list of spots: a string is one spot. Qwen fills one spot per call (asked for several spots at
    once it draws only one), so each spot gets its own pass."""
    return [w.strip().rstrip(".") for w in ([ins["where"]] if isinstance(ins["where"], str) else ins["where"])]


def empty_spaces(spots: list[str]) -> str:
    # Wording that mentions "the size of a figure" or "a character is drawn into later" makes ChatGPT draw empty
    # person silhouettes, so the sentence only describes blank paper.
    return (f"Leave these spots as plain blank paper: {'; '.join(spots)}. Draw absolutely nothing there: no person, no "
            f"outline, no silhouette, no placeholder shape, no shadow, no label, no mark. Just a clear gap of white paper "
            f"between the drawings around it.")


def insert_prompts(scene: dict) -> list[str]:
    """Qwen-Image-2.1's instruction for each insert pass, one per spot."""
    return [INSERT_QWEN.format(where=spot) for spot in insert_spots(scene["insert"])]


def write_prompt_files(out: Path, s: dict, p: str) -> None:
    d = out / "prompts"
    (d / f"scene_{s['id']}.chatgpt.txt").write_text(p, encoding="utf-8")
    if s.get("insert"):          # an insert scene has no Qwen fallback prompt; one line per insert pass
        (d / f"scene_{s['id']}.qwen.txt").unlink(missing_ok=True)
        (d / f"scene_{s['id']}.insert.txt").write_text("\n".join(insert_prompts(s)), encoding="utf-8")
    else:
        (d / f"scene_{s['id']}.insert.txt").unlink(missing_ok=True)
        (d / f"scene_{s['id']}.qwen.txt").write_text(for_qwen(p), encoding="utf-8")


def for_qwen(prompt: str) -> str:
    """Qwen-Image draws bracketed hex codes into the picture as text."""
    return re.sub(r"\s*\(#[0-9A-Fa-f]{6}\)", "", prompt)


def with_fix(prompt: str, fix: str) -> str:
    if not fix:
        return prompt
    lead = ("Corrections from the earlier reviews (each one still applies)" if fix.lstrip().startswith("(1) ")
            else "Correction from the previous attempt's review")
    return f"{prompt} {lead}: {fix.strip()}"


def target_size(aspect: str) -> tuple[int, int]:
    a, b = (int(x) for x in aspect.split(":"))
    return (SCENE_LONG_SIDE, round(SCENE_LONG_SIDE * b / a)) if a >= b else (round(SCENE_LONG_SIDE * a / b), SCENE_LONG_SIDE)


# ------------------------------------------------------------------ budget (spec phase 5)

def load_module(style: str) -> dict:
    p = STYLES / style / "module.json"
    if not p.is_file():
        raise Fail(f"budget: {p} not found; a style needs module.json to be budgeted from narration")
    return json.loads(p.read_text(encoding="utf-8"))


def compute_budget(scenes: list[dict], alignment: dict, module: dict) -> dict:
    """Scene duration (from the narration) -> drawing time -> ink budget -> detail tier. One pen runs the whole
    story: no end hold, only the story's first sentence waits for the lead-in, and the camera rests
    camera.dwell_s on every finished segment, so each segment has its sentence minus dwell_s to draw in."""
    by_id = {s["id"]: s for s in alignment["scenes"]}
    seg_ids = {g["id"] for g in alignment["segments"]}
    speed = module["drawing_speed_px_s"]
    out = {}
    for s in scenes:
        if s["id"] not in by_id:
            raise Fail(f"budget: scene {s['id']} is not in alignment.json; re-run narrate.py")
        missing = [g["id"] for g in s["segments"] if g["id"] not in seg_ids]
        if missing:
            raise Fail(f"budget: segments {missing} are not in alignment.json; re-run narrate.py")
        dur = by_id[s["id"]]["end_s"] - by_id[s["id"]]["start_s"]
        lead = module["lead_in_s"] if s is scenes[0] else 0
        dwell = module["camera"]["dwell_s"]
        draw = max(1.0, dur - lead - dwell * len(s["segments"]))
        ink = round(draw * speed)
        tier = next(t for t in module["detail_tiers"] if t["max_ink_px"] is None or ink <= t["max_ink_px"])
        segt = {g["id"]: g for g in alignment["segments"]}
        avail = {}
        for i, g in enumerate(s["segments"]):
            t = segt[g["id"]]
            avail[g["id"]] = max(0.1, t["end_s"] - t["start_s"] - (lead if i == 0 else 0) - dwell)
        total = sum(avail.values())
        shares = {k: round(v / total, 3) for k, v in avail.items()}
        out[s["id"]] = {"id": s["id"], "duration_s": round(dur, 3), "draw_time_s": round(draw, 3), "ink_budget_px": ink,
                        "tier": tier["name"], "tier_prompt": tier["prompt"], "n_segments": len(s["segments"]),
                        "segment_shares": shares, "segment_available_s": {k: round(v, 3) for k, v in avail.items()}}
    return out


def write_budget(out: Path, module: dict, budget: dict) -> Path:
    p = out.parent / "budget.json"          # contract: RUN/budget.json, scenes live in RUN/scenes
    p.write_text(json.dumps({"drawing_speed_px_s": module["drawing_speed_px_s"],
                             "scenes": [{k: v for k, v in b.items() if k != "tier_prompt"} for b in budget.values()]},
                            indent=2), encoding="utf-8")
    return p


# ------------------------------------------------------------------ segment regions (Gate 1 image TWO, reveal masks)

FULL_W = 1600                      # review sheet width; reviewer boxes are in these pixels
HIGHLIGHT_FADE = 0.15              # other segments' ink strength in a highlight image
BLOB_SHARE = 0.6                   # a blob with this share inside one box (or inside the arrow boxes) goes there whole
SPLIT_SHARE = 0.03                 # ... unless this share of it (and at least SPLIT_MIN_PX) lies in another segment's box,
SPLIT_MIN_PX = 2000                #     outside the winning box and every box enclosing it: then it is split pixel by pixel
RESIDUAL_SPECK_PX = 50             # leftover ink pieces smaller than this stay `residual`; larger ones join the nearest ink
ARROW_OUTLINE = (254, 64, 35)


def box_error(b, bw: int, bh: int) -> str:
    """Why `b` is not a valid [x0, y0, x1, y1] box inside a bw x bh sheet; "" when it is."""
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b)):
        return "must be [x0, y0, x1, y1] in full.png pixels"
    if not (0 <= b[0] < b[2] <= bw and 0 <= b[1] < b[3] <= bh and b[2] - b[0] >= 8 and b[3] - b[1] >= 8):
        return f"{b} must lie inside {bw}x{bh} with x0<x1 and y0<y1, at least 8 px each way"
    return ""


def region_boxes(sc: dict, att: dict) -> tuple[dict, list, str]:
    """(element id -> box, arrow boxes, source) in full.png pixels: the attempt's rebox if it has one, else its
    review. Boxes from reviews written before connector arrows were boxed on their own enclose them and list none; they are refused."""
    if att.get("rebox"):
        return att["rebox"]["elements"], att["rebox"]["arrows"], "rebox"
    c = att["verdict"]["checklist"]
    if "arrows" not in c:
        raise Fail(f"regions: scene {sc['scene']['id']} attempt {att['k']} was boxed before connector arrows were boxed "
                   f"separately; run `rebox --scene {sc['scene']['id']}` with a review agent")
    return {e["id"]: e["box"] for e in c["elements"] if e.get("present") and e.get("box")}, c["arrows"], "review"


def build_regions(out: Path, st: dict, sc: dict, att: dict) -> dict:
    """Assign every ink pixel of the selected image to a segment, to the connector arrows, or to `residual`,
    from the reviewer's element and arrow boxes (`region_boxes`):
    1. Arrow ink: a blob with at least BLOB_SHARE of its pixels inside the arrow boxes, and a larger share there than
       inside any one element box, is arrow ink whole. Any other ink inside an arrow box and outside every element box
       is arrow ink too. Arrow ink is erased: it is in no segment mask, only in scene_N_arrows_mask.png.
    2. Element ink: a blob with at least BLOB_SHARE inside one element box goes to it whole, unless at least
       SPLIT_SHARE of it and SPLIT_MIN_PX 4K px lie inside the box of an element of another segment, outside the
       winning box (a box that encloses the winning box does not count: a nested element's overshoot is not
       another element's ink), e.g. a clipboard outline drawn touching a desk. Such a blob, and a blob across
       several element boxes, is split pixel by pixel (smaller boxes win where boxes overlap; its ink outside every
       box stays with the winning box).
    3. Leftover ink (blobs touching no box): every connected piece of RESIDUAL_SPECK_PX or more joins the element or
       the arrows whose ink is nearest; smaller specks stay `residual`.
    Writes the masks, the highlight images and scene_N_segments.json."""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage
    sid = sc["scene"]["id"]
    try:
        thr = load_module(st["style"])["ink_threshold"]
    except Fail:
        thr = 200
    elem_boxes, arrow_boxes, source = region_boxes(sc, att)
    img = Image.open(att["scene_4k"]).convert("RGB")
    W, H = img.size
    k = W / FULL_W
    rgb = np.asarray(img)
    ink = (np.asarray(img.convert("L")) < thr)

    def to4k(b):
        return [max(0, round(b[0] * k)), max(0, round(b[1] * k)), min(W, round(b[2] * k)), min(H, round(b[3] * k))]

    boxes = {i: to4k(b) for i, b in elem_boxes.items()}
    arrows = [to4k(b) for b in arrow_boxes]
    ids = list(boxes)
    in_e, in_a = np.zeros((H, W), bool), np.zeros((H, W), bool)
    for b in boxes.values():
        in_e[b[1]:b[3], b[0]:b[2]] = True
    for b in arrows:
        in_a[b[1]:b[3], b[0]:b[2]] = True
    ARROW = len(ids)                                  # label value for arrow ink; element i has label i, -1 = unassigned
    n, comp, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    tot = np.maximum(stats[:, cv2.CC_STAT_AREA].astype(np.float64), 1)
    best, bestf = np.zeros(n, np.int64), np.zeros(n)
    if ids:
        bx = np.array(list(boxes.values()), np.int64)
        area = (bx[:, 2] - bx[:, 0]) * (bx[:, 3] - bx[:, 1])
        # frac[i, c] = share of blob c's pixels inside element box i, counted in one pass per box
        frac = np.stack([np.bincount(comp[b[1]:b[3], b[0]:b[2]].ravel(), minlength=n) / tot for b in boxes.values()])
        frac[:, 0] = 0                                # component 0 is background
        bestf = frac.max(0)
        # a blob equally inside several boxes (nested boxes) goes to the smallest of them
        best = np.where(frac >= bestf - 1e-9, area[:, None], np.iinfo(np.int64).max).argmin(0)
    fa = np.bincount(comp[in_a], minlength=n) / tot
    fa[0] = 0
    is_arrow = (fa >= BLOB_SHARE) & (fa > bestf)
    whole = (bestf >= BLOB_SHARE) & ~is_arrow
    if ids:
        def clip(b, x, y, w, h):                       # box b as slices of the w x h window at (x, y)
            return (slice(max(b[1] - y, 0), max(min(b[3] - y, h), 0)), slice(max(b[0] - x, 0), max(min(b[2] - x, w), 0)))
        # encl[j, i]: box j encloses box i; seg[i]: the segment revealing element i
        encl = ((bx[:, None, 0] <= bx[None, :, 0]) & (bx[:, None, 1] <= bx[None, :, 1])
                & (bx[:, None, 2] >= bx[None, :, 2]) & (bx[:, None, 3] >= bx[None, :, 3]))
        seg = [next((g["id"] for g in sc["scene"]["segments"] if i in g["reveals"]), None) for i in ids]
        for c in np.flatnonzero(whole & (tot >= SPLIT_MIN_PX)):
            x, y, w, h = stats[c, :4]
            sub = comp[y:y + h, x:x + w] == c
            other = np.zeros(sub.shape, bool)
            for j in np.flatnonzero(~encl[:, best[c]]):  # boxes of other segments that do not enclose the winner
                if seg[j] != seg[best[c]]:
                    other[clip(bx[j], x, y, w, h)] = True
            other[clip(bx[best[c]], x, y, w, h)] = False
            px = int((sub & other).sum())
            if px >= SPLIT_MIN_PX and px >= SPLIT_SHARE * tot[c]:
                whole[c] = False
    comp_to = np.full(n, -1, np.int32)
    comp_to[whole] = best[whole]
    comp_to[is_arrow] = ARROW
    label = comp_to[comp].astype(np.int16)
    label[~ink] = -1
    if ids:
        for c in np.flatnonzero((bestf > 0) & ~whole & ~is_arrow):   # a blob across several boxes: split pixel by pixel
            x, y, w, h = stats[c, :4]
            sub = comp[y:y + h, x:x + w] == c
            choice = np.full(sub.shape, best[c], np.int16)
            for i in np.argsort(-area):               # smaller boxes overwrite larger ones
                choice[clip(bx[i], x, y, w, h)] = i
            region = label[y:y + h, x:x + w]
            region[sub] = choice[sub]
    label[ink & in_a & ~in_e] = ARROW                 # box-clipped arrow ink that no element box claims
    left = ink & (label == -1)
    reassigned = 0
    if left.any():
        ln, lcomp, lstats, _ = cv2.connectedComponentsWithStats(left.astype(np.uint8), connectivity=8)
        big = np.flatnonzero(lstats[:, cv2.CC_STAT_AREA] >= RESIDUAL_SPECK_PX)
        big = big[big != 0]
        targets = [t for t in range(len(ids) + 1) if (label == t).any()]
        if len(big) and targets:
            # distance from every pixel to each target's ink; a piece joins the target nearest to any of its pixels
            near = np.stack([ndimage.minimum(cv2.distanceTransform(np.where(label == t, 0, 255).astype(np.uint8),
                                                                   cv2.DIST_L2, 3), lcomp, big) for t in targets])
            to = np.full(ln, -1, np.int16)
            to[big] = np.array(targets, np.int16)[near.argmin(0)]
            moved = left & (to[lcomp] >= 0)
            label[moved] = to[lcomp][moved]
            reassigned = int(moved.sum())
    arrow = label == ARROW
    drawn = ink & ~arrow                              # the ink that stays on the board
    faded = (255 - (255 - rgb.astype(np.float32)) * HIGHLIGHT_FADE).astype(np.uint8)
    white = np.full_like(rgb, 255)

    def highlight(full: np.ndarray, outlines: list, colour: tuple, name: str) -> None:
        hi = np.where(full[..., None], rgb, np.where(drawn[..., None], faded, white))
        him = Image.fromarray(hi).resize((FULL_W, round(FULL_W * H / W)), Image.LANCZOS)
        dr = ImageDraw.Draw(him)
        for b in outlines:
            dr.rectangle([v / k for v in b], outline=colour, width=3)
        him.save(out / name)

    el_to_seg = {r: g["id"] for g in sc["scene"]["segments"] for r in g["reveals"]}
    segs = []
    for g in sc["scene"]["segments"]:
        idx = [ids.index(r) for r in g["reveals"] if r in ids]
        m = np.isin(label, idx) & ink if idx else np.zeros((H, W), bool)
        mname, hname = f"scene_{sid}_seg_{g['id']}_mask.png", f"scene_{sid}_seg_{g['id']}_highlight.png"
        Image.fromarray((m * 255).astype(np.uint8)).save(out / mname)
        highlight(m, [boxes[r] for r in g["reveals"] if r in boxes], (47, 111, 235), hname)
        segs.append({"id": g["id"], "elements": [{"id": r, "box": boxes.get(r)} for r in g["reveals"]],
                     "mask": mname, "ink_px": int(m.sum()), "highlight": hname})
    aname, ahname = f"scene_{sid}_arrows_mask.png", f"scene_{sid}_arrows_highlight.png"
    Image.fromarray((arrow * 255).astype(np.uint8)).save(out / aname)
    highlight(arrow, arrows, ARROW_OUTLINE, ahname)
    res = (label < 0) & ink
    rname = f"scene_{sid}_residual_mask.png"
    Image.fromarray((res * 255).astype(np.uint8)).save(out / rname)
    doc = {"scene": sid, "size": [W, H], "ink_threshold": thr, "attempt": att["k"], "sha256": att["sha256"], "boxes_from": source,
           "segments": segs, "arrows": arrows,
           "arrows_mask": {"mask": aname, "ink_px": int(arrow.sum()), "highlight": ahname},
           "residual": {"mask": rname, "ink_px": int(res.sum()), "speck_below_px": RESIDUAL_SPECK_PX},
           "missing_boxes": [e for e in el_to_seg if e not in boxes]}
    (out / f"scene_{sid}_segments.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    log_event(out, "regions", scene=sid, attempt=att["k"], boxes_from=source, arrows=len(arrows), arrow_px=int(arrow.sum()),
              reassigned_px=reassigned, residual_px=int(res.sum()), ink_px=int(ink.sum()),
              segments={s["id"]: s["ink_px"] for s in segs}, missing_boxes=doc["missing_boxes"])
    return doc


# ------------------------------------------------------------------ loop logic

_RENDER_PROCS: list[tuple[int, str]] | None = None


def render_processes() -> list[tuple[int, str]]:
    """Every live `render_scenes.py render` process (pid, command line), read once per invocation."""
    global _RENDER_PROCS
    if _RENDER_PROCS is None:
        ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*' -or $_.Name -eq 'cmd.exe') -and "
                             "$_.CommandLine -match 'render_scenes' } | Select-Object ProcessId,CommandLine | "
                             "ConvertTo-Json -Compress"],
                            capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
        data = json.loads(ps.stdout) if ps.stdout.strip() else []
        data = data if isinstance(data, list) else [data]
        _RENDER_PROCS = [(int(p["ProcessId"]), p["CommandLine"] or "") for p in data
                         if int(p["ProcessId"]) != os.getpid()
                         and re.search(r"render_scenes\.py\"?\s+render\b", p["CommandLine"] or "")]
    return _RENDER_PROCS


def render_alive(att: dict, sid: int) -> bool:
    """Is the process that reserved this attempt still running? Attempts recorded before pids were
    stored fall back to any live render of the same scene number."""
    procs = render_processes()
    if att.get("pid"):
        return any(pid == att["pid"] for pid, _ in procs)
    return any(re.search(rf"--scene\s+{sid}\b", cmd) for _, cmd in procs)


def round_attempts(sc: dict) -> list[dict]:
    """Attempts in the current round. A Gate 1 edit (`reopen`/`sync`) starts a new round with a fresh
    five-attempt budget; earlier rounds' attempts stay in the list for the record."""
    return sc["attempts"][sc.get("round_start", 0):]


def scene_status(st: dict, sid: int) -> str:
    sc = st["scenes"][str(sid)]
    if sc.get("flagged"):
        return "flagged"
    if sc.get("selected"):
        return "passed"
    cur = round_attempts(sc)
    if not cur:
        return "not rendered"
    last = cur[-1]
    if last["state"] == "requested":
        return "awaiting submission"
    if last["state"] == "submitted":
        return "generating"
    if last["state"] == "unknown":
        return "submission unknown"
    if last["state"] == "rendering":
        started = time.mktime(time.strptime(last["started"], "%Y-%m-%d %H:%M:%S"))
        if time.time() - started > STALE_RENDER_MIN * 60 or not render_alive(last, sc["scene"]["id"]):
            return "render stalled"
        return "rendering"
    if last["state"] == "failed":
        return "render failed"
    if not last.get("verdict"):
        return "awaiting review"
    return "failed review"


def next_action(st: dict, sid: int, out: Path | None = None) -> str:
    first = st["order"][0]
    status = scene_status(st, sid)
    sc = st["scenes"][str(sid)]
    cur = round_attempts(sc)
    if status in ("passed", "flagged"):
        return "done"
    if status == "rendering":
        return "wait (render in progress)"
    if status == "awaiting submission":
        note = hf.pending_note(out.parent, f"scene{sid}/a{cur[-1]['k']}") if out is not None else None
        if note:   # the ledger saw the submit start (write-ahead) or holds its job: never submit it again
            return note
        return f"submit attempt {cur[-1]['k']} (its REQUEST_JSON: work/scene_{sid}_a{cur[-1]['k']}_request.json)"
    if status == "generating":
        return f"wait for job {cur[-1].get('job_id')} and ingest attempt {cur[-1]['k']}"
    if status == "submission unknown":
        return (f"reconcile attempt {cur[-1]['k']}: check the Higgsfield account, then `render-submitted --job ID` "
                f"or `render-ingest --failed`")
    if sid != first and scene_status(st, first) not in ("passed", "flagged"):
        return f"wait (scene {first} is the style reference and must pass first)"
    if status == "awaiting review":
        return f"review attempt {cur[-1]['k']}"
    if len(cur) >= MAX_ATTEMPTS:
        return "exhausted: no attempt passed review and none can be flagged"
    if status == "failed review":
        return f"render attempt {len(cur) + 1} of {MAX_ATTEMPTS} with the review fix"
    if status in ("render failed", "render stalled"):
        return f"render attempt {len(cur) + 1} of {MAX_ATTEMPTS}"
    return f"render attempt 1 of {MAX_ATTEMPTS}"


def select(out: Path, st: dict, sc: dict, att: dict, flagged: bool) -> None:
    """Make `att` the scene's image: copy it to scene_N_4k.png and build its segment masks."""
    sc["selected"], sc["flagged"] = att["k"], flagged
    shutil.copyfile(att["scene_4k"], out / f"scene_{sc['scene']['id']}_4k.png")
    build_regions(out, st, sc, att)


def exhaust(out: Path, st: dict, sc: dict) -> None:
    """After the round's last allowed attempt: select and flag the best reviewed attempt, if any."""
    cur = round_attempts(sc)
    if sc.get("selected") or len(cur) < MAX_ATTEMPTS:
        return
    reviewed = [x for x in cur if x.get("verdict")]
    if reviewed:
        select(out, st, sc, max(reviewed, key=flag_rank), flagged=True)


def flag_rank(att: dict) -> tuple[bool, float]:
    """Which failed attempt to keep. First, an attempt whose checklist passed (so its density was measured) beats
    any attempt whose checklist failed: a failed checklist (a missing element, a wrong label, ...) never reaches the
    density pass, and an unmeasured attempt must not look density-perfect. Then: review score minus 100 points per
    unit of density outside the band (a segment at 0.57 against a 0.75 floor costs 18). Density decides how the
    animation plays, so an image that draws evenly beats a prettier one that stalls."""
    v = att["verdict"]
    off = 0.0
    for r in v.get("density", []):
        if not r["ok"]:
            off += max(0.0, 0.75 - r["ratio"]) + max(0.0, r["ratio"] - 1.25)
    return bool(v.get("density")), v["score"] - 100 * off


def last_fix(sc: dict) -> str:
    """The fixes for the next attempt: the gate note that opened this round, then every review fix of the round in
    order (an earlier fix still applies: a later review checks only what is still wrong, so dropping the earlier fix
    lets a corrected part go wrong again). One fix is returned as is; several are numbered."""
    fixes = [sc.get("gate_fix", "")] + [x["verdict"].get("requiredFix", "") for x in round_attempts(sc)
                                        if x.get("verdict")]
    uniq = list(dict.fromkeys(f.strip() for f in fixes if f and f.strip()))
    if len(uniq) <= 1:
        return uniq[0] if uniq else ""
    return " ".join(f"({i}) {f}" for i, f in enumerate(uniq, 1)) + " Where two corrections differ, the later one wins."


def reference_image(st: dict, sid: int) -> Path | None:
    first = st["order"][0]
    if sid == first:
        return None
    sc = st["scenes"][str(first)]
    k = sc.get("selected")
    if not k:
        raise Fail(f"render: scene {first} has no selected attempt yet; it is the style reference")
    return Path(sc["attempts"][k - 1]["raw"])


# ------------------------------------------------------------------ engines

def codex(args: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run([str(PY), str(engine("chatgpt")["driver"]), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout} s"


def codex_check() -> list[str]:
    """Doctor: the ChatGPT driver's own `check` (Codex CLI installed and signed in). Prints the result line; returns
    the problem with its fix, or [] when the CLI is ready."""
    rc, text = codex(["check"], timeout=150)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    first = lines[0] if lines else f"no output (exit {rc})"
    if rc == 0 and first.startswith("LOGGED_IN"):
        say(f"doctor: Codex CLI ready: {first}")
        return []
    if first.startswith("NO_CODEX"):
        fix = "install it: `npm install -g @openai/codex` (needs Node.js), then sign in: `codex login`"
    elif first.startswith("NEED_LOGIN"):
        fix = "sign in once in a terminal: `codex login` (with a ChatGPT plan that includes image generation)"
    else:
        fix = "run the driver's `check` by hand to see why"
    return [f"Codex CLI not ready ({first}): {fix}; every scene image is rendered through it"]


def chatgpt(prompt: str, ref: Path | None, dest: Path, name: str, aspect: str,
            sheets: list[tuple[str, Path]] | None = None) -> tuple[Path | None, str]:
    """ChatGPT render. `ref` = scene 1's image as style reference; `sheets` = (name, character sheet) pairs.
    With any reference, Image 1 is always a blank canvas and every reference is attached after it: the edit
    call treats Image 1 as the picture to change, and with scene 1 there it redrew scene 1."""
    sheets = sheets or []
    reason = ""
    if ref is None and not sheets:
        args = ["render", "--prompt", prompt, "--aspect", aspect]
    else:
        lead = ("Image 1 is a blank white canvas: draw a completely new scene on it, filling the canvas. The other "
                "images are references only; never copy their content or layout. ")
        refs, i = [], 2
        if ref is not None:
            lead += (f"Image {i} is a finished scene from the same video, a STYLE reference only: match its line "
                     f"weight, lettering, colour use and drawing style, but draw none of its objects, characters' poses "
                     f"or layout. ")
            refs.append(ref)
            i += 1
        for who, s in sheets:
            lead += (f"Image {i} is the character sheet for {who}: draw {who} exactly as that sheet shows (face, "
                     f"glasses, hair, facial hair, clothing, proportions, colours) in the pose this scene asks for; do "
                     f"not copy the sheet's layout or repeat the character. ")
            refs.append(s)
            i += 1
        args = ["edit", "--image", str(blank_canvas(dest, aspect)), "--prompt", lead + prompt, "--raw"]
        for r in refs:
            args += ["--ref", str(r)]
    for attempt in range(1, CHATGPT_TRIES + 1):
        rc, text = codex(args + ["--dest", str(dest), "--name", name])
        m = re.search(r"RESULT_JSON (\[.*\])", text)
        if rc == 0 and m:
            f = Path(json.loads(m.group(1))[0]["file"])
            if f.is_file():
                return f, ""
        reason = text.strip().splitlines()[-1] if text.strip() else f"exit {rc}"
        say(f"chatgpt try {attempt} failed: {reason}")
    return None, reason


def blank_canvas(work: Path, aspect: str) -> Path:
    from PIL import Image
    a, b = (int(x) for x in aspect.split(":"))
    w = 1672
    p = work / f"_blank_{aspect.replace(':', 'x')}.png"
    if not p.is_file():
        Image.new("RGB", (w, round(w * b / a)), (255, 255, 255)).save(p)
    return p


def scene_characters(scene: dict) -> list[str]:
    """Ids of the declared characters the scene shows: every element's, then a chatgpt+insert scene's insert character."""
    ids = []
    for cid in [c for e in scene["elements"] for c in e.get("characters", [])] + \
               ([scene["insert"]["character"]] if scene.get("insert") else []):
        if cid not in ids:
            ids.append(cid)
    return ids


def scene_sheets(st: dict, out: Path, scene: dict, exclude: str | None = None) -> list[tuple[str, Path]]:
    """(name, sheet path) for every declared character the scene shows except `exclude`; fails if a sheet is missing."""
    chars = {c["id"]: c for c in st.get("characters", [])}
    sheeted = sheeted_ids(out.parent, st.get("characters", []))
    res = []
    for cid in scene_characters(scene):
        if cid == exclude or cid not in sheeted:   # no sheet: the prompt describes the character instead
            continue
        p = out.parent / "characters" / f"{cid}_sheet.png"
        if not p.is_file():
            raise Fail(f"scene {scene['id']} shows {cid}, but {p} does not exist; run characters.py build first")
        res.append((chars[cid]["name"], p))
    return res


def sheet_front_view(sheet: Path, dest: Path) -> Path:
    """The top-left panel of a characters.py sheet (two rows of three, no dividers; top row full body, front view
    first) as a single-view reference for Qwen, which reads a multi-panel sheet as several people: the sheet is split
    at its widest ink-free row gap and the top row at its two widest ink-free column gaps, then the first cell is
    cropped to its ink plus a margin."""
    import numpy as np
    from PIL import Image
    im = Image.open(sheet).convert("RGB")
    ink = (np.asarray(im) < INK_THRESHOLD).any(axis=2)

    def groups(profile: np.ndarray, n: int, what: str) -> list[tuple[int, int]]:
        on = profile > max(2, 0.002 * profile.size)             # a stray speck does not make a row or column inked
        edges = np.flatnonzero(np.diff(np.r_[0, on.astype(np.int8), 0]))
        runs = list(zip(edges[::2], edges[1::2]))             # [start, end) of each inked stretch
        if len(runs) < n:
            raise Fail(f"insert: {sheet} has {len(runs)} ink {what} groups, not the {n} of a characters.py sheet")
        cuts = sorted(sorted(range(1, len(runs)), key=lambda i: runs[i][0] - runs[i - 1][1])[-(n - 1):]) if n > 1 else []
        bounds = [0, *cuts, len(runs)]
        return [(runs[bounds[i]][0], runs[bounds[i + 1] - 1][1]) for i in range(n)]

    y0, y1 = groups(ink.sum(axis=1), 2, "row")[0]
    x0, x1 = groups(ink[y0:y1].sum(axis=0), 3, "column")[0]
    ys, xs = np.nonzero(ink[y0:y1, x0:x1])
    bx0, by0, bx1, by1 = x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1
    m = round(0.05 * max(bx1 - bx0, by1 - by0))
    im.crop((max(0, bx0 - m), max(0, by0 - m), min(im.width, bx1 + m), min(im.height, by1 + m))).save(dest)
    return dest


def qwen_run(cmd: list[str], prefix: str) -> Path:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    outs = [l.split("OUTPUT:", 1)[1].strip() for l in p.stdout.splitlines() if l.startswith("OUTPUT:")]
    if p.returncode != 0 or not outs or not Path(outs[-1]).is_file():
        raise Fail(f"qwen: {prefix} failed (exit {p.returncode}): {(p.stdout + p.stderr)[-600:]}")
    return Path(outs[-1])


def qwen(job: dict) -> Path:
    out_dir = Path(job["out_dir"])
    prefix = job["prefix"]
    if job["mode"] == "ref":
        w, h = QWEN_NATIVE[job["aspect"]]
        canvas = out_dir / f"_blank_{w}x{h}.png"
        if not canvas.is_file():
            from PIL import Image
            Image.new("RGB", (w, h), (255, 255, 255)).save(canvas)
        cmd = [str(PY), str(engine("qwen_ref")["driver"]), "--image", str(canvas), "--ref", job["ref"],
               "--prompt", REF_QWEN + job["prompt"], "--out", str(out_dir), "--seed", str(job["seed"]), "--prefix", prefix,
               "--megapixels", "0", "--keep-size"]
    else:
        t2i = engine("qwen_t2i")
        cmd = [str(PY), str(t2i["driver"]), "--variant", t2i["variant"], "--prompt", job["prompt"], "--aspect", job["aspect"],
               "--seed", str(job["seed"]), "--prefix", prefix, "--save-dir", str(out_dir)]
    return qwen_run(cmd, prefix)


def insert_mask(chat, drawn) -> tuple:
    """Which pixels of Qwen's edit (`drawn`) to paste onto ChatGPT's image (`chat`), both HxWx3 uint8 of one size:
    only NEW ink. New ink = paper-white in ChatGPT and ink in Qwen; it is grouped into connected components after a
    small close. A component is kept if it has at least INSERT_MIN_AREA of the image as new ink and at least
    INSERT_FAR_SHARE of that ink farther than INSERT_NEAR_PX from ChatGPT's ink: Qwen's redraws of existing lines,
    shifted by a pixel or three, lie along those lines and are dropped. The kept components grow by INSERT_GROW_PX
    and the paste stays on ChatGPT's paper and off Qwen's paper. Returns (bool paste mask, stats)."""
    import cv2
    import numpy as np
    from scipy import ndimage
    h, w = chat.shape[:2]
    paper = chat.min(axis=2) >= INSERT_WHITE
    new = paper & (drawn.min(axis=2) < INSERT_INK)
    dist = cv2.distanceTransform(paper.astype(np.uint8), cv2.DIST_L2, 3)   # to the nearest ChatGPT ink pixel

    def disk(r: int):
        return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    closed = cv2.morphologyEx(new.astype(np.uint8), cv2.MORPH_CLOSE, disk(INSERT_CLOSE_PX)) > 0
    lab, n = ndimage.label(closed, structure=np.ones((3, 3)))
    idx = np.arange(1, n + 1)
    area = np.asarray(ndimage.sum(new, lab, idx)) if n else np.zeros(0)
    far = np.asarray(ndimage.sum(new & (dist > INSERT_NEAR_PX), lab, idx)) if n else np.zeros(0)
    min_area = INSERT_MIN_AREA * h * w
    keep = (area >= min_area) & (far >= INSERT_FAR_SHARE * np.maximum(area, 1))
    boxes = ndimage.find_objects(lab)
    kept = [{"box": [boxes[i][1].start, boxes[i][0].start, boxes[i][1].stop, boxes[i][0].stop],
             "new_ink_px": int(area[i]), "far_share": round(float(far[i] / area[i]), 3)} for i in np.flatnonzero(keep)]
    grown = cv2.dilate(np.isin(lab, idx[keep]).astype(np.uint8), disk(INSERT_GROW_PX)) > 0
    paste = grown & paper & (drawn.min(axis=2) < INSERT_WHITE)
    stats = {"components": int(n), "kept": kept, "dropped": int(n - len(kept)), "pasted_px": int(paste.sum()),
             "pasted_share": round(float(paste.sum()) / (h * w), 5), "min_area_px": round(min_area),
             "near_px": INSERT_NEAR_PX, "far_share_min": INSERT_FAR_SHARE}
    return paste, stats


def insert(job: dict) -> dict:
    """Qwen-Image-2.1 draws the insert character into ChatGPT's empty spaces, one pass per spot, each pass editing the
    previous pass's composite; only each pass's new ink is composited (insert_mask). Fails (so the loop retries) when a
    pass keeps no component or its new ink covers over INSERT_MAX_AREA of the picture (Qwen redrew the scene)."""
    import numpy as np
    from PIL import Image
    cur = np.asarray(Image.open(job["image"]).convert("RGB"))
    src, union, passes = job["image"], np.zeros(cur.shape[:2], bool), []
    for i, prompt in enumerate(job["prompts"], 1):
        prefix = f"{job['prefix']}{i}"
        cmd = [str(PY), str(engine("qwen_ref")["driver"]), "--image", src, "--ref", job["ref"], "--prompt", prompt,
               "--out", job["out_dir"], "--seed", str(job["seed"]), "--prefix", prefix,
               "--megapixels", "0", "--keep-size", "--ref-megapixels", "0.5"]
        drawn_path = qwen_run(cmd, prefix)
        drawn = np.asarray(Image.open(drawn_path).convert("RGB"))
        if drawn.shape != cur.shape:
            raise Fail(f"insert: Qwen returned {drawn.shape[1]}x{drawn.shape[0]}, not the source's {cur.shape[1]}x{cur.shape[0]} "
                       f"({drawn_path})")
        paste, stats = insert_mask(cur, drawn)
        passes.append({"qwen_output": str(drawn_path), **stats})
        if not stats["kept"]:
            raise Fail(f"insert: pass {i} drew no new figure in its empty space (no new-ink component of "
                       f"{stats['min_area_px']}+ px mostly over {INSERT_NEAR_PX} px from the existing lines; "
                       f"{stats['components']} dropped); see {drawn_path}")
        if stats["pasted_share"] > INSERT_MAX_AREA:
            raise Fail(f"insert: pass {i}'s new ink covers {stats['pasted_share']:.0%} of the picture: Qwen redrew the scene "
                       f"instead of drawing into the empty space; see {drawn_path}")
        cur, union = np.where(paste[..., None], drawn, cur), union | paste
        Image.fromarray(cur).save(job["composite"])
        src = job["composite"]
    Image.fromarray((union * 255).astype(np.uint8)).save(job["mask"])
    return {"reference": job["ref"], "seed": job["seed"], "mask": job["mask"], "composite": job["composite"],
            "pasted_px": int(union.sum()), "passes": passes}


def gpu_lease(purpose: str, jobs_file: Path) -> None:
    """Run `gpu-jobs jobs_file` inside a GPU-queue lease (settings gpu_queue), or directly when no queue is set."""
    queue = cfg.path("gpu_queue")
    worker = [str(PY), str(Path(__file__).resolve()), "gpu-jobs", str(jobs_file)]
    if queue is None:
        say(f"gpu: {purpose} (no GPU queue set; running directly)")
        p = subprocess.run(worker, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    else:
        pwsh = cfg.powershell()
        if pwsh is None:
            raise Fail(f"gpu: {cfg.where('gpu_queue')} needs PowerShell; none on PATH, set powershell in settings")
        payload = f'& "{PY}" "{Path(__file__).resolve()}" gpu-jobs "{jobs_file}"'
        cmd = [str(pwsh), "-NoProfile", "-Command",
               f"& '{queue}' run -Agent scribeboard-render -Purpose '{purpose}' -Timeout 3600 -Payload '{payload}'"]
        say(f"gpu: waiting for a GPU-queue lease ({purpose})")
        p = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode != 0:
        results = Path(json.loads(jobs_file.read_text(encoding="utf-8"))["results"])
        err = json.loads(results.read_text(encoding="utf-8")).get("error") if results.is_file() else None
        if err:                                              # gpu-jobs failed with a reason (cmd_gpu_jobs)
            raise Fail(err)
        if queue is None:
            raise Fail(f"gpu: GPU job failed with exit {p.returncode}")
        log = cfg.path("gpu_queue_log")
        raise Fail(f"gpu: lease job failed with exit {p.returncode} (75 = no lease before timeout)"
                   + (f"; see {log}" if log else ""))


# ------------------------------------------------------------------ finishing

PAPER_FLOOR = 236     # a grey pixel this light or lighter is paper, not ink: it becomes pure white
PAPER_GREY = 8        # ...if its channels differ by at most this much (a light accent tint is kept)


def paper_white(im):
    """GPT Image sometimes lays a faint off-white panel (about 244/255) behind the drawing. That panel is darker than
    INK_THRESHOLD, so framing would crop to it and the drawing pass would draw it. Lift every light grey pixel to pure
    white. Returns (image, share of pixels lifted that were not already white)."""
    import numpy as np
    from PIL import Image
    a = np.asarray(im.convert("RGB")).copy()
    lo, hi = a.min(axis=2), a.max(axis=2)
    paper = (lo >= PAPER_FLOOR) & (hi - lo <= PAPER_GREY)
    changed = paper & (lo < 255)
    a[paper] = 255
    return Image.fromarray(a), float(changed.mean())


def flatten(src: Path):
    """The image as RGB on pure white: transparency (ChatGPT can return it) composited on white, then grey paper
    lifted to white (`paper_white`). Returns (image, share of pixels lifted)."""
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    white = Image.new("RGBA", im.size, (255, 255, 255, 255))
    white.alpha_composite(im)
    return paper_white(white.convert("RGB"))


def frame(src: Path, raw: Path, size: tuple[int, int]):
    """Flatten on white, crop to ink, centre on a white frame of the output aspect. Returns (framed, scale, share of
    pixels `paper_white` lifted)."""
    import numpy as np
    from PIL import Image
    flat, lifted = flatten(src)
    flat.save(raw)
    a = np.asarray(flat)
    ink = (a < INK_THRESHOLD).any(axis=2)
    if not ink.any():
        raise Fail(f"frame: {src} has no ink")
    ys, xs = np.where(ink)
    crop = flat.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    tw, th = size
    inner = 1 - 2 * FRAME_MARGIN
    fw = round(max(crop.width / inner, crop.height / inner * tw / th))
    fh = round(fw * th / tw)
    framed = Image.new("RGB", (fw, fh), (255, 255, 255))
    framed.paste(crop, ((fw - crop.width) // 2, (fh - crop.height) // 2))
    return framed, tw / fw, lifted


_GPU_INDEX: int | None = None


def realesrgan() -> Path:
    """realesrgan-ncnn-vulkan.exe in settings realesrgan_dir."""
    if REALESRGAN_DIR is None:
        raise Fail("upscale: realesrgan_dir is not set in settings (the folder with realesrgan-ncnn-vulkan.exe)")
    exe = REALESRGAN_DIR / REALESRGAN_EXE_NAME
    if not exe.is_file():
        raise Fail(f"upscale: {exe} not found ({cfg.where('realesrgan_dir')})")
    return exe


def realesrgan_gpu_index(work: Path) -> int:
    """ncnn lists every Vulkan device; pick the NVIDIA one (an integrated GPU can be device 0)."""
    global _GPU_INDEX
    if _GPU_INDEX is None:
        from PIL import Image
        probe = work / "_probe.png"
        Image.new("RGB", (16, 16), (255, 255, 255)).save(probe)
        p = subprocess.run([str(realesrgan()), "-i", str(probe), "-o", str(work / "_probe_out.png"), "-n", REALESRGAN_MODEL,
                            "-s", "4", "-m", str(REALESRGAN_DIR / "models")],
                           capture_output=True, text=True, cwd=REALESRGAN_DIR, errors="replace", creationflags=NO_WINDOW)
        found = re.findall(r"\[(\d+) ([^\]]*NVIDIA[^\]]*)\]", p.stdout + p.stderr)
        if not found:
            raise Fail(f"upscale: Real-ESRGAN lists no NVIDIA Vulkan device:\n{(p.stdout + p.stderr)[-800:]}")
        _GPU_INDEX = int(found[0][0])
    return _GPU_INDEX


def finish(job: dict) -> dict:
    """Frame and upscale one attempt to the exact scene size. Returns what was done."""
    from PIL import Image
    size = tuple(job["size"])
    work = Path(job["work_dir"])
    framed, scale, lifted = frame(Path(job["source"]), Path(job["raw"]), size)
    if scale > AI_UPSCALE_ABOVE:
        fpath, up = work / f"{job['stem']}_framed.png", work / f"{job['stem']}_x4.png"
        framed.save(fpath)
        dev = realesrgan_gpu_index(work)
        p = subprocess.run([str(realesrgan()), "-i", str(fpath), "-o", str(up), "-n", REALESRGAN_MODEL, "-s", "4",
                            "-m", str(REALESRGAN_DIR / "models"), "-g", str(dev)],
                           capture_output=True, text=True, cwd=REALESRGAN_DIR, errors="replace", creationflags=NO_WINDOW)
        if p.returncode != 0 or not up.is_file():
            raise Fail(f"upscale: Real-ESRGAN failed on {fpath}: {(p.stdout + p.stderr)[-600:]}")
        big, method = Image.open(up).convert("RGB"), f"{REALESRGAN_MODEL} 4x (device {dev}) + lanczos"
    else:
        big, method = framed, "lanczos"
    big.resize(size, Image.LANCZOS).save(job["scene_4k"])
    return {"framed": list(framed.size), "scale": round(scale, 3), "upscale": method,
            "paper_whitened": round(lifted, 4)}


def cmd_gpu_jobs(jobs_file: Path) -> int:
    """Run a jobs file inside the lease: `qwen` (fallback scene) or `insert` (chatgpt+insert), then `finish`. A failure
    is written to the results file as `error`, where gpu_lease reads it."""
    spec = json.loads(jobs_file.read_text(encoding="utf-8"))
    res = {}
    try:
        if spec.get("qwen"):
            res["qwen_source"] = str(qwen(spec["qwen"]))
            spec["finish"]["source"] = res["qwen_source"]
        if spec.get("insert"):
            res["insert"] = insert(spec["insert"])
            spec["finish"]["source"] = res["insert"]["composite"]
        res["finish"] = finish(spec["finish"])
    except Exception as e:
        Path(spec["results"]).write_text(json.dumps({**res, "error": str(e)}, indent=2), encoding="utf-8")
        raise
    Path(spec["results"]).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


def prompt_meta_dir() -> Path:
    d = cfg.path("render.prompt_meta_dir")
    if d is None:
        raise Fail("stamp: render.prompt_meta_dir is not set in settings (the folder with the /render skill's "
                   "prompt_meta.py, which stamps every image with its prompt)")
    if not (d / "prompt_meta.py").is_file():
        raise Fail(f"stamp: no prompt_meta.py in {d} ({cfg.where('render.prompt_meta_dir')})")
    return d


def stamp(path: Path, prompt: str, meta: dict) -> None:
    d = str(prompt_meta_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    import prompt_meta  # the /render skill's stamping module
    prompt_meta.stamp(str(path), prompt, meta, quiet=True)


# ------------------------------------------------------------------ commands

def cmd_validate(a) -> int:
    scenes = validate(json.loads(Path(a.scenes).read_text(encoding="utf-8")))
    n_seg = sum(len(s["segments"]) for s in scenes)
    words = sum(len(g["text"].split()) for s in scenes for g in s["segments"])
    print("VALIDATE_JSON " + json.dumps({"scenes": len(scenes), "segments": n_seg, "words": words,
                                         "est_minutes": round(words / 150, 1)}))
    return 0


def cmd_doctor(a) -> int:
    say("doctor: settings (value, and the file it came from)")
    for line in cfg.lines():
        say(f"  {line}")
    problems = cfg.missing()
    if hf.on("images"):
        say("doctor: images.engine = 'higgsfield': pictures come from the agent's Higgsfield MCP connection (not "
            "checked here; the agent calls `balance`); the Codex driver is not needed")
        try:
            ims = runsetup.image_models()
            say(f"doctor: image models: {', '.join(ims['models'])} (default {ims['default']}; each run chooses on "
                f"its setup page)")
        except runsetup.SetupError as e:
            problems.append(f"image models: {e}")
    for role in ([] if hf.on("images") else ENGINE_ROLES):
        try:
            engine(role)
        except Fail as e:
            problems.append(str(e))
            if role == "chatgpt":
                say("doctor: Codex CLI not checked (no ChatGPT driver resolved)")
            continue
        if role == "chatgpt":
            problems += codex_check()
    if QWEN_ON:
        try:
            urllib.request.urlopen(COMFY_HOST + "/system_stats", timeout=5).read()
        except Exception as e:
            problems.append(f"ComfyUI not reachable at {COMFY_HOST} ({e}); the Qwen fallback needs it")
    else:
        say(f"doctor: engines.qwen = false ({cfg.source('engines.qwen')}): no Qwen fallback, no 'qwen' or "
            f"'chatgpt+insert' scenes; ComfyUI not checked")
    for check in (realesrgan, prompt_meta_dir):
        try:
            check()
        except Fail as e:
            problems.append(str(e))
    if cfg.path("gpu_queue") is None:
        say("doctor: gpu_queue not set: GPU work (Qwen, upscale) runs directly, without a lease")
    elif cfg.powershell() is None:
        problems.append(f"{cfg.where('gpu_queue')} needs PowerShell: none on PATH, set powershell in settings")
    if a.style and runsetup.required():   # colours, voice and style are per run (setup page): check the catalogs
        try:
            if a.style not in runsetup.styles():
                problems.append(f"style {a.style!r} is not a complete style folder (style_guide.md, module.json, sample.png)")
            say(f"doctor: setup catalogs: {len(runsetup.styles())} styles, {len(runsetup.palettes())} palettes, "
                f"{len(runsetup.voices()['voices'])} voices (each run chooses on its setup page)")
        except runsetup.SetupError as e:
            problems.append(f"setup catalogs: {e}")
    elif a.style:
        try:
            style_block(a.style)
        except Fail as e:
            problems.append(str(e))
    if problems:
        raise Fail("doctor:\n  " + "\n  ".join(problems))
    say("doctor: all prerequisites present")
    return 0


def cmd_init(a) -> int:
    out = Path(a.out).resolve()
    if (out / "state.json").is_file() and not a.reset:
        raise Fail(f"init: {out / 'state.json'} exists; continue with `status`, or pass --reset to start over")
    scenes = validate(json.loads(Path(a.scenes).read_text(encoding="utf-8")))
    if a.aspect not in QWEN_NATIVE:
        raise Fail(f"init: aspect {a.aspect} not supported (choose {', '.join(QWEN_NATIVE)})")
    setup_style(out.parent, a.style)
    cast_approved(out.parent)
    chars = json.loads(Path(a.scenes).read_text(encoding="utf-8")).get("characters", [])
    sheet = sheet_info(out.parent, chars)
    check_insert_sheets(scenes, sheet)
    block = style_block(a.style, out.parent)
    budget = {}
    if a.alignment:
        module = load_module(a.style)
        budget = compute_budget(scenes, json.loads(Path(a.alignment).read_text(encoding="utf-8")), module)
    for d in ("prompts", "attempts", "work"):
        (out / d).mkdir(parents=True, exist_ok=True)
    st = {"style": a.style, "aspect": a.aspect, "scene_size": list(target_size(a.aspect)), "qwen_mode": a.qwen_mode,
          "scenes_file": str(Path(a.scenes).resolve()), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
          "alignment_file": str(Path(a.alignment).resolve()) if a.alignment else None,
          "order": [s["id"] for s in scenes], "scenes": {},
          "characters": chars, "sheeted": sorted(sheet["sheeted"])}
    for s in scenes:
        p = scene_prompt(s, block, budget.get(s["id"]), sheet)
        write_prompt_files(out, s, p)
        st["scenes"][str(s["id"])] = {"scene": s, "prompt": p, "budget": budget.get(s["id"]), "attempts": [],
                                      "selected": None, "flagged": False}
    if budget:
        say(f"budget: {write_budget(out, module, budget)}")
    save_state(out, st)
    log_event(out, "init", scenes=st["order"], style=a.style, aspect=a.aspect)
    write_summary(out, st)
    say(f"init: {len(scenes)} scenes validated; prompts in {out / 'prompts'}")
    return cmd_status(a)


def new_round(out: Path, sc: dict, reason: str, gate_fix: str = "") -> None:
    sc["round"] = sc.get("round", 1) + 1
    sc["round_start"] = len(sc["attempts"])
    sc["selected"], sc["flagged"], sc["gate_fix"] = None, False, gate_fix
    final = out / f"scene_{sc['scene']['id']}_4k.png"
    if final.is_file():
        final.unlink()
    log_event(out, "new_round", scene=sc["scene"]["id"], round=sc["round"], reason=reason, gate_fix=gate_fix)


def cmd_sync(a) -> int:
    """Re-read script.json (and alignment.json) after a Gate 1 round. A selected scene keeps its image unless what
    it must show changed (its elements, layout hint, engine or insert, with a new prompt): then it starts a new round.
    If only its segments changed it gets its masks rebuilt; a prompt that changed only in wording or budget is stored
    for any later round. A scene without a selected image starts a new round whenever its prompt changed."""
    out = Path(a.out).resolve()
    with locked(out):
        st = load_state(out)
        scenes = validate(json.loads(Path(a.scenes).read_text(encoding="utf-8")))
        alignment = a.alignment or st.get("alignment_file")
        budget = {}
        if alignment:
            module = load_module(st["style"])
            budget = compute_budget(scenes, json.loads(Path(alignment).read_text(encoding="utf-8")), module)
        setup_style(out.parent, st["style"])
        chars = json.loads(Path(a.scenes).read_text(encoding="utf-8")).get("characters", [])
        sheet = sheet_info(out.parent, chars)
        check_insert_sheets(scenes, sheet)
        st["sheeted"] = sorted(sheet["sheeted"])   # a sheet / no-sheet change at Gate 1 changes the prompts below
        block = style_block(st["style"], out.parent)
        changed, remasked, refreshed, added, removed = [], [], [], [], []
        for s in scenes:
            p = scene_prompt(s, block, budget.get(s["id"]), sheet)
            write_prompt_files(out, s, p)
            sc = st["scenes"].get(str(s["id"]))
            if sc is None:
                st["scenes"][str(s["id"])] = {"scene": s, "prompt": p, "budget": budget.get(s["id"]), "attempts": [],
                                              "selected": None, "flagged": False}
                added.append(s["id"])
                continue
            segs_changed = [(g["id"], g["reveals"]) for g in s["segments"]] != \
                           [(g["id"], g["reveals"]) for g in sc["scene"]["segments"]]
            picture_changed = ((s["elements"], s["layout_hint"], s.get("engine"), s.get("insert")) !=
                               (sc["scene"]["elements"], sc["scene"]["layout_hint"], sc["scene"].get("engine"),
                                sc["scene"].get("insert")))
            prompt_changed = p != sc["prompt"]
            sc["scene"], sc["budget"], sc["prompt"] = s, budget.get(s["id"]), p
            if prompt_changed and (picture_changed or not sc.get("selected")):
                new_round(out, sc, "script or budget changed")
                changed.append(s["id"])
                continue
            if segs_changed and sc.get("selected"):
                build_regions(out, st, sc, sc["attempts"][sc["selected"] - 1])
                remasked.append(s["id"])
            if prompt_changed:
                refreshed.append(s["id"])
        new_ids = [s["id"] for s in scenes]
        for sid in st["order"]:
            if sid not in new_ids:
                st.setdefault("removed", {})[str(sid)] = st["scenes"].pop(str(sid))
                removed.append(sid)
        st["order"], st["scenes_file"] = new_ids, str(Path(a.scenes).resolve())
        st["characters"] = json.loads(Path(a.scenes).read_text(encoding="utf-8")).get("characters", [])
        if alignment:
            st["alignment_file"] = str(Path(alignment).resolve())
            write_budget(out, module, budget)
        save_state(out, st)
        write_summary(out, st)
    log_event(out, "sync", changed=changed, remasked=remasked, prompt_refreshed=refreshed, added=added, removed=removed)
    print("SYNC_JSON " + json.dumps({"new_round": changed, "remasked": remasked, "prompt_refreshed": refreshed,
                                     "added": added, "removed": removed}))
    return 0


def cmd_reopen(a) -> int:
    """Gate 1 'edit' on a scene image: start a new round whose first render carries the note as its fix."""
    out = Path(a.out).resolve()
    with locked(out):
        st = load_state(out)
        sc = st["scenes"].get(str(a.scene))
        if sc is None:
            raise Fail(f"reopen: no scene {a.scene}")
        if not a.fix.strip():
            raise Fail("reopen: --fix must say what the next render should change")
        new_round(out, sc, "gate edit", gate_fix=a.fix.strip())
        save_state(out, st)
        write_summary(out, st)
    print("REOPEN_JSON " + json.dumps({"scene": a.scene, "round": sc["round"]}))
    return 0


def cmd_regions(a) -> int:
    out = Path(a.out).resolve()
    st = load_state(out)
    sc = st["scenes"][str(a.scene)]
    if not sc.get("selected"):
        raise Fail(f"regions: scene {a.scene} has no selected image")
    doc = build_regions(out, st, sc, sc["attempts"][sc["selected"] - 1])
    print("REGIONS_JSON " + json.dumps({"scene": a.scene, "boxes_from": doc["boxes_from"], "arrow_px": doc["arrows_mask"]["ink_px"],
                                        "residual_px": doc["residual"]["ink_px"],
                                        "segments": {s["id"]: s["ink_px"] for s in doc["segments"]},
                                        "missing_boxes": doc["missing_boxes"]}))
    return 0


def cmd_reflag(a) -> int:
    """Re-pick every flagged scene's kept attempt with the current flag_rank (after a ranking fix). A scene whose
    best reviewed attempt in its round differs from the selected one gets that attempt selected (image copied, masks
    rebuilt) and a `reflag` event. Passed scenes and scenes still in their loop are untouched."""
    out = Path(a.out).resolve()
    changed = []
    with locked(out):
        st = load_state(out)
        for sid in st["order"]:
            sc = st["scenes"][str(sid)]
            if not (sc.get("flagged") and sc.get("selected")):
                continue
            reviewed = [x for x in round_attempts(sc) if x.get("verdict")]
            best = max(reviewed, key=flag_rank)
            if best["k"] != sc["selected"]:
                old = sc["selected"]
                select(out, st, sc, best, flagged=True)
                log_event(out, "reflag", scene=sid, old=old, new=best["k"], rank=list(flag_rank(best)))
                changed.append({"scene": sid, "old": old, "new": best["k"]})
        if changed:
            save_state(out, st)
            write_summary(out, st)
    print("REFLAG_JSON " + json.dumps({"changed": changed}))
    return 0


def cmd_rebox(a) -> int:
    """Re-box a selected scene's image without re-rendering or re-scoring (element boxes without the connector
    arrows, every connector arrow boxed on its own). Without --file: write the box sheet and `boxes.json` for the
    review agent. With --file: check the agent's boxes, keep them on the attempt, rebuild masks and highlights."""
    out = Path(a.out).resolve()
    sid, agent = a.scene, a.agent.strip()
    if not agent:
        raise Fail("rebox: --agent must name the review agent")
    with locked(out):
        st = load_state(out)
        sc = st["scenes"].get(str(sid))
        if sc is None:
            raise Fail(f"rebox: no scene {sid}")
        if not sc.get("selected"):
            raise Fail(f"rebox: scene {sid} has no selected image; rebox only re-boxes a selected image")
        att = sc["attempts"][sc["selected"] - 1]
        if agent == att["rendered_by"].strip():
            raise Fail(f"rebox: '{agent}' rendered this attempt; use a fresh review agent")
        if sha256(Path(att["scene_4k"])) != att["sha256"]:
            raise Fail("rebox: the attempt's 4K image changed after rendering")
        rdir = out / "attempts" / f"scene_{sid}_a{att['k']}_rebox"
        tpath = rdir / "boxes.json"
        if not a.file:
            w, h = review_sheet(att, rdir)
            prev = att["rebox"]["elements"] if att.get("rebox") else \
                {e["id"]: e["box"] for e in att["verdict"]["checklist"]["elements"] if e.get("present") and e.get("box")}
            template = {"scene": sid, "attempt": att["k"], "sha256": att["sha256"], "reviewed_by": "",
                        "box_space": {"width": FULL_W, "height": round(FULL_W * h / w), "image": "full.png"},
                        "elements": [{"id": e["id"], "desc": e["desc"], "label": e.get("text", ""), "box": None,
                                      "previous_box": prev[e["id"]]} for e in sc["scene"]["elements"] if e["id"] in prev],
                        "arrows": []}
            tpath.write_text(json.dumps(template, indent=2), encoding="utf-8")
            log_event(out, "rebox_prep", scene=sid, attempt=att["k"], agent=agent)
            print("REBOX_JSON " + json.dumps({"step": "prep", "scene": sid, "attempt": att["k"], "review_dir": str(rdir),
                                              "full": str(rdir / "full.png"),
                                              "quadrants": [str(rdir / f"quadrant_{i}.png") for i in range(1, 5)],
                                              "boxes": str(tpath), "layout_hint": sc["scene"]["layout_hint"]}))
            return 0
        if not tpath.is_file():
            raise Fail(f"rebox: no {tpath}; run rebox without --file first")
        template = json.loads(tpath.read_text(encoding="utf-8"))
        c = json.loads(Path(a.file).read_text(encoding="utf-8"))
        errs = [f"{key} is {c.get(key)!r}, the selected attempt has {template[key]!r}"
                for key in ("scene", "attempt", "sha256") if c.get(key) != template[key]]
        if str(c.get("reviewed_by", "")).strip() != agent:
            errs.append(f"reviewed_by is {c.get('reviewed_by')!r}, expected --agent {agent!r}")
        bw, bh = template["box_space"]["width"], template["box_space"]["height"]
        if [e.get("id") for e in c.get("elements", [])] != [e["id"] for e in template["elements"]]:
            errs.append("elements list must keep every element id, in order")
        errs += [f"element {e.get('id')}: box {box_error(e.get('box'), bw, bh)}"
                 for e in c.get("elements", []) if box_error(e.get("box"), bw, bh)]
        if not isinstance(c.get("arrows"), list):
            errs.append("arrows must be a list of [x0, y0, x1, y1] boxes, one per connector arrow ([] when there is none)")
        else:
            errs += [f"arrows[{i}]: box {box_error(b, bw, bh)}" for i, b in enumerate(c["arrows"]) if box_error(b, bw, bh)]
        if errs:
            raise Fail("rebox: boxes rejected:\n  " + "\n  ".join(errs))
        att["rebox"] = {"elements": {e["id"]: e["box"] for e in c["elements"]}, "arrows": c["arrows"], "reviewed_by": agent,
                        "file": str(Path(a.file).resolve()), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        doc = build_regions(out, st, sc, att)
        save_state(out, st)
    log_event(out, "rebox", scene=sid, attempt=att["k"], reviewer=agent, arrows=len(c["arrows"]),
              arrow_px=doc["arrows_mask"]["ink_px"], residual_px=doc["residual"]["ink_px"])
    print("REBOX_JSON " + json.dumps({"step": "apply", "scene": sid, "attempt": att["k"], "arrows": len(c["arrows"]),
                                      "arrow_px": doc["arrows_mask"]["ink_px"], "residual_px": doc["residual"]["ink_px"],
                                      "segments": {s["id"]: s["ink_px"] for s in doc["segments"]},
                                      "highlights": [str(out / s["highlight"]) for s in doc["segments"]],
                                      "arrows_highlight": str(out / doc["arrows_mask"]["highlight"])}))
    return 0



def cmd_status(a) -> int:
    out = Path(a.out).resolve()
    st = load_state(out)
    rows = [{"scene": sid, "status": scene_status(st, sid), "attempts": len(round_attempts(st["scenes"][str(sid)])),
             "next": next_action(st, sid, out)} for sid in st["order"]]
    for r in rows:
        print(f"scene {r['scene']:>3}  {r['status']:<16} attempts {r['attempts']}/{MAX_ATTEMPTS}  NEXT: {r['next']}")
    done = all(r["next"] == "done" or r["next"].startswith("exhausted") for r in rows)
    print("NEXT_JSON " + json.dumps({"all_done": done, "scenes": rows}))
    if done:
        print("ALL SCENES DONE: run `gate`, then `report`.")
    return 0


def spawn_outside_job(command_line: str, cwd: Path, show_window: int = 0, what: str = "the render worker") -> int:
    """Start a process through WMI (Win32_Process.Create) with its window hidden (Win32_ProcessStartup
    ShowWindow=0), so no console appears or takes focus. WMI's host starts it, so it belongs to no agent
    shell's job object and survives when that agent's turn ends. (A child started with DETACHED_PROCESS +
    CREATE_BREAKAWAY_FROM_JOB still dies with the agent's job when its turn ends.) `show_window` is the
    SW_* value; only a process meant to show a window to the user (a gate page's Chrome tab) passes 1. Returns the pid."""
    ps = ("$si = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly "
          "-Property @{ShowWindow=[uint16]$env:SCRIBE_SHOW}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{CommandLine=$env:SCRIBE_CL; CurrentDirectory=$env:SCRIBE_CWD; ProcessStartupInformation=$si}; "
          "Write-Output \"$($r.ReturnValue) $($r.ProcessId)\"")
    p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, errors="replace",
                       env={**os.environ, "SCRIBE_CL": command_line, "SCRIBE_CWD": str(cwd),
                            "SCRIBE_SHOW": str(int(show_window))}, creationflags=NO_WINDOW)
    parts = p.stdout.strip().split()
    if p.returncode != 0 or len(parts) != 2 or parts[0] != "0":
        raise Fail(f"could not start {what} through WMI: {(p.stdout + p.stderr).strip()[-400:]}")
    return int(parts[1])


def cmd_render(a) -> int:
    """Reserve the next attempt and hand it to a detached worker, then wait for the worker's result.
    The worker survives if the calling agent's turn (and this process) ends; it records its own
    result in state.json, and `status` watches its pid."""
    out = Path(a.out).resolve()
    sid = a.scene
    with locked(out):
        st = load_state(out)
        if str(sid) not in st["scenes"]:
            raise Fail(f"render: no scene {sid}")
        action = next_action(st, sid)
        if not action.startswith("render"):
            raise Fail(f"render: scene {sid} next action is '{action}', not a render")
        if hf.on("images"):
            raise Fail("render: images.engine is 'higgsfield'; use `render-prep`, submit the request through the "
                       "Higgsfield MCP, then `render-submitted` and `render-ingest`")
        sc = st["scenes"][str(sid)]
        if sc["attempts"] and scene_status(st, sid) == "render stalled":
            sc["attempts"][-1].update(state="failed", error="stalled: the render process died or ran past the time limit")
            log_event(out, "render_failed", scene=sid, attempt=sc["attempts"][-1]["k"], error="stalled")
        fix = last_fix(sc)
        k = len(sc["attempts"]) + 1
        reference_image(st, sid)          # fails now, not in the worker, if scene 1 is not selected
        scene_sheets(st, out, sc["scene"])  # ...or if a character it shows has no sheet yet
        wlog = out / "work" / f"scene_{sid}_a{k}_worker.log"
        # WMI starts the worker without this process's environment: set SCRIBEBOARD_SETTINGS (or clear it) in its
        # cmd.exe line, so it reads exactly the settings files this process read.
        ov = cfg.override()
        pid = spawn_outside_job(
            f'cmd.exe /c "set "{cfg.ENV}={ov or ""}" && "{PY}" "{Path(__file__).resolve()}" render-worker --out "{out}" '
            f'--scene {sid} --attempt {k} > "{wlog}" 2>&1"', REPO)

        class _P:                                    # the waiting loop below only needs pid + poll()
            def __init__(self, pid): self.pid = pid
            def poll(self):
                global _RENDER_PROCS
                _RENDER_PROCS = None
                return None if any(p == self.pid for p, _ in render_processes()) else 0
        proc = _P(pid)
        sc["attempts"].append({"k": k, "state": "rendering", "rendered_by": a.agent, "fix": fix, "pid": proc.pid,
                               "worker_log": str(wlog), "started": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_state(out, st)
    log_event(out, "render_start", scene=sid, attempt=k, agent=a.agent, fix=fix, worker_pid=proc.pid)
    say(f"render: scene {sid} attempt {k} running in worker pid {proc.pid} (log {wlog})")
    deadline = time.time() + STALE_RENDER_MIN * 60
    while time.time() < deadline:
        time.sleep(5)
        att = load_state(out)["scenes"][str(sid)]["attempts"][k - 1]
        if att["state"] != "rendering":
            break
        if proc.poll() is not None:
            time.sleep(2)
            att = load_state(out)["scenes"][str(sid)]["attempts"][k - 1]
            break
    if att["state"] == "rendered":
        print("RENDER_JSON " + json.dumps({"scene": sid, "attempt": k, "engine": att["engine"], "scene_4k": att["scene_4k"]}))
        return 0
    raise Fail(f"render: scene {sid} attempt {k} {att['state']}: {att.get('error', f'worker ended without a result; see {wlog}')}")


def cmd_render_worker(a) -> int:
    out = Path(a.out).resolve()
    sid, k = a.scene, a.attempt
    with locked(out):
        st = load_state(out)
        sc = st["scenes"][str(sid)]
        att = sc["attempts"][k - 1]
        if att["state"] != "rendering":
            raise Fail(f"render-worker: scene {sid} attempt {k} is '{att['state']}', not rendering")
        fix, ref = att["fix"], reference_image(st, sid)
        scene = sc["scene"]
        ins = scene.get("insert")     # chatgpt+insert: ChatGPT never gets the insert character's sheet
        sheets = scene_sheets(st, out, scene, exclude=ins["character"] if ins else None)
    stem = f"scene_{sid}_a{k}"
    adir, work = out / "attempts", out / "work"
    size = tuple(st["scene_size"])
    prompt = hide_insert(with_fix(sc["prompt"], fix), scene)
    rec, meta_extra = {}, {}
    try:
        if (scene.get("engine") == "qwen" or ins) and not QWEN_ON:   # a run set up while engines.qwen was on
            raise Fail(f"engine {scene.get('engine')!r} needs the local Qwen-Image engines, which are off "
                       f"(engines.qwen = false in {cfg.source('engines.qwen')}); set the scene's engine to 'auto' and sync")
        if scene.get("engine") == "qwen":     # user decision per scene (e.g. content ChatGPT will not draw)
            src, why = None, "script.json sets this scene's engine to qwen"
        else:
            src, why = chatgpt(prompt, ref, work, f"{stem}_chatgpt.png", st["aspect"], sheets)
        fin_job = {"source": str(src) if src else "", "raw": str(adir / f"{stem}_raw.png"), "scene_4k": str(adir / f"{stem}_4k.png"),
                   "size": list(size), "work_dir": str(work), "stem": stem}
        if ins:
            if src is None:
                raise Fail(f"chatgpt+insert: ChatGPT failed ({why}); an insert scene has no Qwen fallback (Qwen cannot "
                           f"draw the whole scene)")
            flat, iprompts = work / f"{stem}_chatgpt_flat.png", insert_prompts(scene)
            flatten(src)[0].save(flat)
            iref = sheet_front_view(out.parent / "characters" / f"{ins['character']}_sheet.png", work / f"{stem}_insert_ref.png")
            seed = QWEN_SEED_BASE + 100 * sid + k
            jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
            jobs.write_text(json.dumps({"insert": {"image": str(flat), "ref": str(iref), "prompts": iprompts, "seed": seed,
                                                   "out_dir": str(work), "prefix": f"{stem}_insert",
                                                   "composite": str(work / f"{stem}_insert.png"),
                                                   "mask": str(adir / f"{stem}_insert_mask.png")},
                                        "finish": fin_job, "results": str(results)}), encoding="utf-8")
            gpu_lease(f"scribeboard scene {sid} attempt {k}: Qwen character insert + upscale", jobs)
            r = json.loads(results.read_text(encoding="utf-8"))
            rec.update(engine="chatgpt+qwen-insert", fallback_reason="", prompt_used=prompt, chatgpt_source=str(src),
                       insert_prompts=iprompts, insert=r["insert"], insert_mask=r["insert"]["mask"], **r["finish"])
            meta_extra = {"chatgpt_source": str(src), "insert_character": ins["character"], "insert_reference": str(iref),
                          "insert_seed": seed, "insert_mask": r["insert"]["mask"],
                          **{f"insert_prompt_{i}": p for i, p in enumerate(iprompts, 1)},
                          **{f"insert_output_{i}": p["qwen_output"] for i, p in enumerate(r["insert"]["passes"], 1)}}
            src = Path(r["insert"]["composite"])
        elif src is not None:
            rec.update(engine="chatgpt", fallback_reason="", prompt_used=prompt)
            _, scale, _ = frame(src, adir / f"{stem}_raw.png", size)
            if scale > AI_UPSCALE_ABOVE:
                jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
                jobs.write_text(json.dumps({"finish": fin_job, "results": str(results)}), encoding="utf-8")
                gpu_lease(f"scribeboard scene {sid} attempt {k}: upscale", jobs)
                rec.update(json.loads(results.read_text(encoding="utf-8"))["finish"])
            else:
                rec.update(finish(fin_job))
        else:
            if not QWEN_ON:
                raise Fail(f"chatgpt: ChatGPT failed {CHATGPT_TRIES} times ({why}); no Qwen fallback "
                           f"(engines.qwen = false in {cfg.source('engines.qwen')})")
            mode = "prompt" if ref is None else st["qwen_mode"]
            qprompt = for_qwen(prompt)
            log_event(out, "fallback", scene=sid, attempt=k, reason=why, engine="qwen-image-2.1", mode=mode)
            jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
            jobs.write_text(json.dumps({"qwen": {"mode": mode, "prompt": qprompt, "aspect": st["aspect"], "ref": str(ref or ""),
                                                 "seed": QWEN_SEED_BASE + 100 * sid + k, "out_dir": str(work), "prefix": f"{stem}_qwen"},
                                        "finish": fin_job, "results": str(results)}), encoding="utf-8")
            gpu_lease(f"scribeboard scene {sid} attempt {k}: Qwen fallback + upscale", jobs)
            r = json.loads(results.read_text(encoding="utf-8"))
            src = Path(r["qwen_source"])
            rec.update(engine="qwen-image-2.1", qwen_mode=mode, fallback_reason=why, prompt_used=qprompt, **r["finish"])
        meta = {"engine": rec["engine"], "style": st["style"], "scene": sid, "attempt": k, "source": str(src),
                "style_reference": str(ref) if ref else None, "fix": fix, "upscale": rec["upscale"],
                "driver": "scribeboard render_scenes.py", **meta_extra}
        for p in (adir / f"{stem}_raw.png", adir / f"{stem}_4k.png"):
            stamp(p, rec["prompt_used"], meta)
        rec.update(state="rendered", source=str(src), raw=str(adir / f"{stem}_raw.png"), scene_4k=str(adir / f"{stem}_4k.png"),
                   sha256=sha256(adir / f"{stem}_4k.png"), finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        rec.update(state="failed", error=str(e)[-800:])
    with locked(out):
        st = load_state(out)
        sc = st["scenes"][str(sid)]
        sc["attempts"][k - 1].update(rec)
        if rec["state"] == "failed":
            exhaust(out, st, sc)
        save_state(out, st)
        write_summary(out, st)
    log_event(out, "render_" + rec["state"], scene=sid, attempt=k, engine=rec.get("engine"), error=rec.get("error"),
              upscale=rec.get("upscale"), scene_4k=rec.get("scene_4k"))
    if rec["state"] == "failed":
        raise Fail(f"render: scene {sid} attempt {k} failed: {rec['error']}")
    print("RENDER_JSON " + json.dumps({"scene": sid, "attempt": k, "engine": rec["engine"], "scene_4k": rec["scene_4k"]}))
    return 0


# ------------------------------------------------------------------ Higgsfield handoff (images.engine 'higgsfield')


def sheet_job_ids(run: Path) -> dict:
    """characters/sheets.json: {sheet file name: {"job_id", ...}} written by characters.py ingest."""
    p = run / "characters" / "sheets.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def hf_refs(out: Path, st: dict, sid: int) -> tuple[list[dict], bool, list[str]]:
    """(medias, style reference attached, sheet names) for a scene: scene 1's selected job as the style reference,
    then each character sheet the scene shows, as earlier Higgsfield job ids."""
    refs, names = [], []
    first = st["order"][0]
    style = sid != first
    if style:
        fsc = st["scenes"][str(first)]
        k = fsc.get("selected")
        if not k:
            raise Fail(f"render-prep: scene {first} has no selected attempt yet; it is the style reference")
        job = fsc["attempts"][k - 1].get("job_id")
        if not job:
            raise Fail(f"render-prep: scene {first}'s selected attempt has no Higgsfield job id (it was not made "
                       f"through Higgsfield); re-render scene {first} with images.engine 'higgsfield'")
        refs.append({"value": job, "role": "image_references"})
    jobs = sheet_job_ids(out.parent)
    for name, path in scene_sheets(st, out, st["scenes"][str(sid)]["scene"]):
        job = (jobs.get(path.name) or {}).get("job_id")
        if not job:
            raise Fail(f"render-prep: the sheet {path.name} has no Higgsfield job id (characters/sheets.json); "
                       f"rebuild it with `characters.py prep --force`")
        refs.append({"value": job, "role": "image_references"})
        names.append(name)
    return refs, style, names


def cmd_render_prep(a) -> int:
    out = Path(a.out).resolve()
    sid = a.scene
    if not hf.on("images"):
        raise Fail(f"render-prep: images.engine is {cfg.get('images.engine')!r}, not 'higgsfield'; use `render`")
    with locked(out):
        st = load_state(out)
        if str(sid) not in st["scenes"]:
            raise Fail(f"render-prep: no scene {sid}")
        action = next_action(st, sid)
        if not action.startswith("render"):
            raise Fail(f"render-prep: scene {sid} next action is '{action}', not a render")
        sc = st["scenes"][str(sid)]
        scene = sc["scene"]
        if scene.get("engine", "auto") != "auto" or scene.get("insert"):
            raise Fail(f"render-prep: scene {sid} sets engine {scene.get('engine')!r}; with Higgsfield every scene is 'auto'")
        if sc["attempts"] and scene_status(st, sid) == "render stalled":
            sc["attempts"][-1].update(state="failed", error="stalled")
        cast_approved(out.parent)
        runsetup_ok(out.parent)
        now = sorted(sheeted_ids(out.parent, st.get("characters", [])))
        if now != st.get("sheeted", sorted(c["id"] for c in st.get("characters", []))):
            raise Fail(f"render-prep: which characters have a sheet changed since the prompts were made (now: "
                       f"{', '.join(now) or 'none'}); run `render_scenes.py sync` first (scribe.py status routes it)")
        if sid != 1 and runsetup.required():   # scene 1 is the style reference: the user approves it first
            import gate6
            why = gate6.style_problem(out.parent)
            if why:
                raise Fail(f"render-prep: scene {sid} waits: {why}")
        k = len(sc["attempts"]) + 1
        refs, style, names = hf_refs(out, st, sid)      # every other check first: a refusal reserves nothing
        fix = last_fix(sc)
        prompt = hf.ref_lead(style, names) + with_fix(sc["prompt"], fix)
        try:
            req = hf.image_request(prompt, out.parent, refs, st["aspect"])
            budget = hf.ledger_check(out.parent, "image", [f"scene{sid}/a{k}"],   # checks the setup, reserves
                                     req["arguments"]["params"].get("model"))
        except hf.HFError as e:
            raise Fail(str(e))
        rfile = out / "work" / f"scene_{sid}_a{k}_request.json"
        rfile.parent.mkdir(exist_ok=True)
        rfile.write_text(json.dumps(req, indent=2), encoding="utf-8")
        sc["attempts"].append({"k": k, "state": "requested", "rendered_by": a.agent, "fix": fix, "engine": "higgsfield",
                               "model": req["arguments"]["params"]["model"], "prompt_used": prompt, "request": str(rfile), "started": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_state(out, st)
    log_event(out, "render_requested", scene=sid, attempt=k, agent=a.agent, fix=fix, refs=[r["value"] for r in refs])
    hf.emit_request("image", f"scene{sid}/a{k}", req, {"scene": sid, "attempt": k, "credits": budget,
                                                         "request_file": str(rfile)}, run=out.parent)
    return 0


def request_model(att: dict) -> str:
    """The Higgsfield model id an attempt was sent to (recorded at render-prep; older attempts: their saved request
    file), for the picture's stamp. Fails rather than stamp a picture without its model."""
    if att.get("model"):
        return att["model"]
    try:
        return json.loads(Path(att["request"]).read_text(encoding="utf-8"))["arguments"]["params"]["model"]
    except (KeyError, TypeError, OSError, ValueError) as e:
        raise Fail(f"the attempt's model is unknown (no model recorded, request file {att.get('request')!r} "
                   f"unreadable: {e})") from e


def _hf_attempt(out: Path, st: dict, sid: int, k: int, states: tuple[str, ...]) -> tuple[dict, dict]:
    if str(sid) not in st["scenes"]:
        raise Fail(f"no scene {sid}")
    sc = st["scenes"][str(sid)]
    if not 1 <= k <= len(sc["attempts"]):
        raise Fail(f"scene {sid} has no attempt {k}")
    att = sc["attempts"][k - 1]
    if att["state"] not in states:
        raise Fail(f"scene {sid} attempt {k} is '{att['state']}', expected {' or '.join(states)}")
    return sc, att


def cmd_render_submitted(a) -> int:
    out = Path(a.out).resolve()
    ref = f"scene{a.scene}/a{a.attempt}"
    with locked(out):
        st = load_state(out)
        sc, att = _hf_attempt(out, st, a.scene, a.attempt, ("requested", "unknown"))
        if a.unknown:
            att.update(state="unknown", unknown=a.unknown)
            hf.ledger_record(out.parent, "image", ref, None, a.quoted, "unknown")
        else:
            if not a.job:
                raise Fail("render-submitted: give --job ID (or --unknown NOTE when the outcome is unclear)")
            att.update(state="submitted", job_id=a.job, quoted=a.quoted)
            hf.ledger_record(out.parent, "image", ref, a.job, a.quoted, "submitted")
        save_state(out, st)
    log_event(out, "render_submitted", scene=a.scene, attempt=a.attempt, job_id=a.job, quoted=a.quoted, unknown=a.unknown)
    print("SUBMITTED_JSON " + json.dumps({"scene": a.scene, "attempt": a.attempt, "state": att["state"],
                                          "job_id": att.get("job_id")}))
    return 0


def cmd_render_ingest(a) -> int:
    out = Path(a.out).resolve()
    sid, k = a.scene, a.attempt
    with locked(out):
        st = load_state(out)
        # --failed also closes a requested attempt that was never submitted (the user declined its price)
        sc, att = _hf_attempt(out, st, sid, k, ("requested", "submitted", "unknown") if a.failed else ("submitted",))
    stem = f"scene_{sid}_a{k}"
    adir, work = out / "attempts", out / "work"
    adir.mkdir(exist_ok=True)
    size = tuple(st["scene_size"])
    rec: dict = {}
    if a.failed:
        rec.update(state="failed", error=(f"closed before it was submitted: {a.failed}" if att["state"] == "requested"
                                          else f"Higgsfield job failed: {a.failed}"))   # the ledger is closed below
    else:
        try:
            model = request_model(att)   # before the download and upscale: a retry never repeats them for this
            src = hf.download(a.url, work / f"{stem}_higgsfield.png", att.get("job_id"))
            fin_job = {"source": str(src), "raw": str(adir / f"{stem}_raw.png"), "scene_4k": str(adir / f"{stem}_4k.png"),
                       "size": list(size), "work_dir": str(work), "stem": stem}
            _, scale, _ = frame(src, adir / f"{stem}_raw.png", size)
            if scale > AI_UPSCALE_ABOVE:
                jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
                jobs.write_text(json.dumps({"finish": fin_job, "results": str(results)}), encoding="utf-8")
                gpu_lease(f"scribeboard scene {sid} attempt {k}: upscale", jobs)
                rec.update(json.loads(results.read_text(encoding="utf-8"))["finish"])
            else:
                rec.update(finish(fin_job))
            meta = {"engine": "higgsfield", "model": model, "job_id": att["job_id"],
                    "style": st["style"], "scene": sid, "attempt": k, "source": a.url, "fix": att.get("fix", ""),
                    "upscale": rec["upscale"], "driver": "scribeboard render_scenes.py"}
            for p in (adir / f"{stem}_raw.png", adir / f"{stem}_4k.png"):
                stamp(p, att["prompt_used"], meta)
            rec.update(state="rendered", source=str(src), source_url=a.url, raw=str(adir / f"{stem}_raw.png"),
                       scene_4k=str(adir / f"{stem}_4k.png"), sha256=sha256(adir / f"{stem}_4k.png"),
                       finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        except (Fail, hf.HFError, OSError) as e:
            # the picture exists on Higgsfield and was paid for: keep the attempt `submitted` so ingest can be retried
            raise Fail(f"render-ingest: scene {sid} attempt {k}: {e}; the job is paid, retry `render-ingest` (do not "
                       f"resubmit)")
    with locked(out):
        st = load_state(out)
        sc = st["scenes"][str(sid)]
        now = sc["attempts"][k - 1]["state"]
        if now != att["state"]:   # another command changed the attempt meanwhile (a `submitted` landed): re-check
            raise Fail(f"render-ingest: scene {sid} attempt {k} changed from '{att['state']}' to '{now}' meanwhile; "
                       f"run `status` and ingest again")
        if rec["state"] == "failed":
            try:
                hf.ledger_close(out.parent, f"scene{sid}/a{k}", now)
            except hf.HFError as e:
                raise Fail(f"render-ingest: {e}")
        sc["attempts"][k - 1].update(rec)
        if rec["state"] == "failed":
            exhaust(out, st, sc)
        save_state(out, st)
        write_summary(out, st)
    log_event(out, "render_" + rec["state"], scene=sid, attempt=k, engine="higgsfield", error=rec.get("error"),
              upscale=rec.get("upscale"), scene_4k=rec.get("scene_4k"))
    if rec["state"] == "failed":
        raise Fail(f"render-ingest: scene {sid} attempt {k} failed: {rec['error']}"
                   + (" (never submitted: its reservation is released, no credits)" if att["state"] == "requested"
                      else " (Higgsfield refunds failed jobs)"))
    print("RENDER_JSON " + json.dumps({"scene": sid, "attempt": k, "engine": "higgsfield", "scene_4k": rec["scene_4k"],
                                       "upscale": rec["upscale"], "paper_whitened": rec.get("paper_whitened")}))
    return 0


def review_sheet(att: dict, rdir: Path) -> tuple[int, int]:
    """full.png (1600 px wide, the box space) and the four full-resolution quadrants of an attempt's 4K image."""
    from PIL import Image
    rdir.mkdir(exist_ok=True)
    im = Image.open(att["scene_4k"]).convert("RGB")
    w, h = im.size
    im.resize((FULL_W, round(FULL_W * h / w))).save(rdir / "full.png")
    for i, (x, y) in enumerate([(0, 0), (w // 2, 0), (0, h // 2), (w // 2, h // 2)], 1):
        im.crop((x, y, x + w // 2, y + h // 2)).save(rdir / f"quadrant_{i}.png")
    return w, h


def cmd_review_prep(a) -> int:
    from PIL import Image
    out = Path(a.out).resolve()
    st = load_state(out)
    sid = a.scene
    sc = st["scenes"][str(sid)]
    if not next_action(st, sid).startswith("review"):
        raise Fail(f"review-prep: scene {sid} next action is '{next_action(st, sid)}', not a review")
    att = sc["attempts"][-1]
    k = att["k"]
    rdir = out / "attempts" / f"scene_{sid}_a{k}_review"
    w, h = review_sheet(att, rdir)
    ref = None
    if sid != st["order"][0]:
        ref = reference_image(st, sid)
        r = Image.open(ref).convert("RGB")
        r.resize((1600, round(1600 * r.height / r.width))).save(rdir / "style_reference.png")
    scene = sc["scene"]
    names = {c["id"]: c["name"] for c in st.get("characters", [])}
    checklist = {
        "scene": sid, "attempt": k, "sha256": att["sha256"], "reviewed_by": "",
        "elements": [{"id": e["id"], "desc": e["desc"], "present": None, "box": None, "note": ""} for e in scene["elements"]],
        "arrows": [],
        "box_space": {"width": FULL_W, "height": round(FULL_W * h / w), "image": "full.png"},
        "labels": [{"element": e["id"], "expected": e["text"], "read": None, "correct": None}
                   for e in scene["elements"] if e.get("text")],
        "extra_text": [], "extra_objects": [],
        "style": {k2: None for k2 in STYLE_KEYS},
        "elements_touching": None, "layout_followed": None,
        "matches_reference_style": None if ref else "n/a",
        "characters": [{"id": cid, "name": names[cid], "matches_sheet": None, "note": ""} for cid in scene_characters(scene)
                       if cid in sheeted_ids(out.parent, st.get("characters", []))],   # no sheet: nothing to match
        "worstIssue": "", "requiredFix": "",
    }
    (rdir / "checklist.json").write_text(json.dumps(checklist, indent=2), encoding="utf-8")
    sheet_paths = {}
    for name, p in scene_sheets(st, out, scene):
        cid = next(c["id"] for c in st.get("characters", []) if c["name"] == name)
        dst = rdir / f"character_{cid}.png"
        Image.open(p).convert("RGB").save(dst)
        sheet_paths[cid] = str(dst)
    info = {"review_dir": str(rdir), "full": str(rdir / "full.png"), "quadrants": [str(rdir / f"quadrant_{i}.png") for i in range(1, 5)],
            "style_reference": str(rdir / "style_reference.png") if ref else None, "checklist": str(rdir / "checklist.json"),
            "character_sheets": sheet_paths,
            "layout_hint": scene["layout_hint"], "engine": att["engine"], "fix_applied": att.get("fix", "")}
    if scene.get("insert"):   # chatgpt+insert: the element descs leave spaces; the insert character stands in them
        info["insert"] = {"character": scene["insert"]["character"], "where": scene["insert"]["where"],
                          "mask": att.get("insert_mask")}
    log_event(out, "review_prep", scene=sid, attempt=k)
    print("REVIEW_JSON " + json.dumps(info))
    return 0


def score_checklist(c: dict) -> tuple[int, bool, list[str]]:
    ded, keyfails = [], []
    for e in c["elements"]:
        if not e["present"]:
            ded.append(("missing_element", e["id"]))
            keyfails.append(f"element {e['id']} missing")
    for l in c["labels"]:
        if not l["correct"]:
            ded.append(("bad_label", f"{l['expected']!r} read as {l['read']!r}"))
            keyfails.append(f"label {l['expected']!r} wrong")
    ded += [("extra_text", t) for t in c["extra_text"]]
    ded += [("extra_object", t) for t in c["extra_objects"]]
    for k2 in STYLE_KEYS:
        if c["style"][k2]:
            ded.append((k2, ""))
            if k2 in KEY_STYLE:
                keyfails.append(k2)
    if c["elements_touching"]:
        ded.append(("elements_touching", ""))
    if not c["layout_followed"]:
        ded.append(("layout_not_followed", ""))
    for ch in c.get("characters", []):
        if not ch["matches_sheet"]:
            ded.append(("character_mismatch", ch["id"]))
            keyfails.append(f"{ch['id']} does not match the character sheet")
    if c["matches_reference_style"] is False:
        ded.append(("style_differs_from_reference", ""))
    score = max(0, 100 - sum(DEDUCT[d] for d, _ in ded))
    return score, score >= PASS_SCORE and not keyfails, [f"-{DEDUCT[d]} {d} {x}".strip() for d, x in ded]


def validate_checklist(c: dict, template: dict) -> None:
    errs = []
    for key in ("scene", "attempt", "sha256"):
        if c.get(key) != template[key]:
            errs.append(f"{key} is {c.get(key)!r}, the attempt under review has {template[key]!r}")
    if not str(c.get("reviewed_by", "")).strip():
        errs.append("reviewed_by is empty")
    if [e["id"] for e in c.get("elements", [])] != [e["id"] for e in template["elements"]]:
        errs.append("elements list must keep every element id, in order")
    if [l["expected"] for l in c.get("labels", [])] != [l["expected"] for l in template["labels"]]:
        errs.append("labels list must keep every expected label, in order")
    bw, bh = template["box_space"]["width"], template["box_space"]["height"]
    for e in c.get("elements", []):
        if not isinstance(e.get("present"), bool):
            errs.append(f"element {e.get('id')}: present must be true or false")
        elif e["present"] and box_error(e.get("box"), bw, bh):
            errs.append(f"element {e['id']}: box {box_error(e.get('box'), bw, bh)}")
    if not isinstance(c.get("arrows"), list):
        errs.append("arrows must be a list of [x0, y0, x1, y1] boxes, one per connector arrow ([] when there is none)")
    else:
        errs += [f"arrows[{i}]: box {box_error(b, bw, bh)}" for i, b in enumerate(c["arrows"]) if box_error(b, bw, bh)]
    for l in c.get("labels", []):
        if not isinstance(l.get("correct"), bool) or l.get("read") is None:
            errs.append(f"label {l.get('expected')!r}: fill read and correct")
    for k2 in STYLE_KEYS:
        if not isinstance(c.get("style", {}).get(k2), bool):
            errs.append(f"style.{k2} must be true or false")
    for k2 in ("elements_touching", "layout_followed"):
        if not isinstance(c.get(k2), bool):
            errs.append(f"{k2} must be true or false")
    if template["matches_reference_style"] == "n/a":
        if c.get("matches_reference_style") != "n/a":
            errs.append("matches_reference_style must stay 'n/a' for the style-reference scene")
    elif not isinstance(c.get("matches_reference_style"), bool):
        errs.append("matches_reference_style must be true or false")
    if [x["id"] for x in c.get("characters", [])] != [x["id"] for x in template.get("characters", [])]:
        errs.append("characters list must keep every character id, in order")
    for x in c.get("characters", []):
        if not isinstance(x.get("matches_sheet"), bool):
            errs.append(f"character {x.get('id')}: matches_sheet must be true or false")
    for k2 in ("extra_text", "extra_objects"):
        if not isinstance(c.get(k2), list):
            errs.append(f"{k2} must be a list")
    if errs:
        raise Fail("verdict: checklist rejected:\n  " + "\n  ".join(errs))


def density_check(out: Path, st: dict, sc: dict, att: dict) -> tuple[bool, list[dict], str]:
    """Does each segment's drawing fit its sentence at the constant drawing speed? Uses reveal.py's own
    stroke/fill analysis on this attempt's segment masks. ratio = drawing time / available time."""
    import importlib.util
    build_regions(out, st, sc, att)
    final = out / f"scene_{sc['scene']['id']}_4k.png"   # reveal.py reads this name: point it at this attempt
    shutil.copyfile(att["scene_4k"], final)
    spec = importlib.util.spec_from_file_location("reveal", REPO / "scripts" / "reveal.py")
    rv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rv)
    run = out.parent
    mod, al = rv.load_module(st["style"]), rv.load_alignment(run)
    try:
        _, _, anal = rv.prepare(run, sc["scene"]["id"], mod, al)
    except rv.Fail as e:
        raise Fail(f"density: reveal.py could not analyse scene {sc['scene']['id']}: {e}")
    lo, hi = load_module(st["style"]).get("density_band", [0.75, 1.25])
    segs = sc["scene"]["segments"]
    times = {g["id"]: g for g in al["segments"]}
    draw = {x["id"]: x["draw_s"] for x in anal}
    rows, fixes = [], []
    for i, g in enumerate(segs):
        t = times[g["id"]]
        # one pen runs the whole story (D59): only the story's first sentence waits for the lead-in, and the camera
        # rests camera.dwell_s on every finished segment
        avail = (t["end_s"] - t["start_s"] - (mod["lead_in_s"] if i == 0 and sc["scene"]["id"] == st["order"][0] else 0)
                 - mod["camera"]["dwell_s"])
        r = draw[g["id"]] / max(avail, 0.1)
        ok = lo <= r <= hi
        rows.append({"id": g["id"], "draw_s": round(draw[g["id"]], 2), "available_s": round(avail, 2), "ratio": round(r, 3), "ok": ok})
        if not ok:
            els = "; ".join(e["desc"] for e in sc["scene"]["elements"] if e["id"] in g["reveals"])
            if r > hi:
                fixes.append(f"The drawing for the sentence \"{g['text']}\" ({els}) takes {draw[g['id']]:.1f} s to draw but the "
                             f"sentence lasts only {avail:.1f} s: cut its line work by about {round((1 - 1 / r) * 100)}% - "
                             f"draw it simpler and smaller, with fewer lines and no hatching.")
            elif draw[g["id"]] <= 0:
                fixes.append(f"The drawing for the sentence \"{g['text']}\" ({els}) has no line work of its own in its box: "
                             f"draw it clearly as its own group, apart from the other drawings, sized to fill about "
                             f"{avail:.1f} s of drawing.")
            else:
                fixes.append(f"The drawing for the sentence \"{g['text']}\" ({els}) takes only {draw[g['id']]:.1f} s to draw but "
                             f"the sentence lasts {avail:.1f} s: it needs about {round((1 / r - 1) * 100)}% more line work - "
                             f"draw it clearly larger, with more interior detail and hatching, or add a small related detail.")
    fix = " ".join(fixes) + (" Keep every other element as it is." if fixes else "")
    log_event(out, "density", scene=sc["scene"]["id"], attempt=att["k"], band=[lo, hi], rows=rows)
    if fixes:
        final.unlink()          # only a selected attempt may sit at scene_N_4k.png
    return not fixes, rows, fix


def cmd_verdict(a) -> int:
    out = Path(a.out).resolve()
    sid = a.scene
    c = json.loads(Path(a.file).read_text(encoding="utf-8"))
    with locked(out):
        st = load_state(out)
        sc = st["scenes"][str(sid)]
        if not next_action(st, sid).startswith("review"):
            raise Fail(f"verdict: scene {sid} next action is '{next_action(st, sid)}', not a review")
        att = sc["attempts"][-1]
        rdir = out / "attempts" / f"scene_{sid}_a{att['k']}_review"
        template = json.loads((rdir / "checklist.json").read_text(encoding="utf-8"))
        validate_checklist(c, template)
        if c["reviewed_by"].strip() == att["rendered_by"].strip():
            raise Fail(f"verdict: reviewer '{c['reviewed_by']}' also rendered this attempt; use a fresh review agent")
        if sha256(Path(att["scene_4k"])) != att["sha256"]:
            raise Fail("verdict: the attempt's 4K image changed after rendering")
        score, passed, deductions = score_checklist(c)
        if not passed and not str(c.get("requiredFix", "")).strip():
            raise Fail(f"verdict: score {score} fails; requiredFix must say what the next render should change")
        v = {"score": score, "pass": passed, "deductions": deductions, "worstIssue": c.get("worstIssue", ""),
             "requiredFix": c.get("requiredFix", ""), "reviewed_by": c["reviewed_by"], "checklist": c,
             "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        att["verdict"] = v
        (rdir / "verdict.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
        if passed and st.get("alignment_file"):
            ok, rows, dfix = density_check(out, st, sc, att)
            v["density"] = rows
            if not ok:
                bad = [r for r in rows if not r["ok"]]
                notes = [f"density {r['id']} {r['ratio']:.2f}" for r in bad]
                if load_module(st["style"]).get("density_mode", "warn") == "fail":
                    passed = v["pass"] = False
                    deductions = v["deductions"] = deductions + notes
                    v["worstIssue"] = f"drawing time out of step with the narration in segment(s) {', '.join(r['id'] for r in bad)}"
                    v["requiredFix"] = f"{str(c.get('requiredFix', '')).strip()} {dfix}".strip()
                else:
                    # D77: density is a warning - the board video absorbs it with max_speedup and short pauses
                    v["density_warnings"] = notes
            (rdir / "verdict.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
        if passed:
            select(out, st, sc, att, flagged=False)
        else:
            exhaust(out, st, sc)
        save_state(out, st)
        write_summary(out, st)
    log_event(out, "verdict", scene=sid, attempt=att["k"], score=score, passed=passed, deductions=deductions,
              reviewer=c["reviewed_by"], fix=v["requiredFix"])
    if sc.get("flagged"):
        log_event(out, "flagged", scene=sid, selected=sc["selected"], best_score=sc["attempts"][sc["selected"] - 1]["verdict"]["score"])
    print("VERDICT_JSON " + json.dumps({"scene": sid, "attempt": att["k"], "score": score, "pass": passed,
                                        "flagged": sc.get("flagged", False), "deductions": deductions,
                                        **({"density_warnings": v["density_warnings"]} if v.get("density_warnings") else {})}))
    return 0


def cmd_gate(a) -> int:
    from PIL import Image
    out = Path(a.out).resolve()
    st = load_state(out)
    bad = []
    for sid in st["order"]:
        sc = st["scenes"][str(sid)]
        k = sc.get("selected")
        if not k:
            bad.append(f"scene {sid}: no selected attempt ({scene_status(st, sid)})")
            continue
        att = sc["attempts"][k - 1]
        v = att.get("verdict")
        if not v or not (v["pass"] or sc.get("flagged")):
            bad.append(f"scene {sid}: selected attempt {k} has no passing verdict and is not flagged")
            continue
        if v["checklist"]["sha256"] != att["sha256"]:
            bad.append(f"scene {sid}: verdict reviewed a different image than attempt {k}")
        if v["reviewed_by"] == att["rendered_by"]:
            bad.append(f"scene {sid}: attempt {k} was reviewed by its own renderer")
        final = out / f"scene_{sid}_4k.png"
        if not final.is_file():
            bad.append(f"scene {sid}: missing {final}")
        else:
            if sha256(final) != att["sha256"]:
                bad.append(f"scene {sid}: {final.name} is not the selected attempt")
            if list(Image.open(final).size) != st["scene_size"]:
                bad.append(f"scene {sid}: {final.name} is {Image.open(final).size}, expected {st['scene_size']}")
        for p in (Path(att["raw"]), Path(att["scene_4k"])):
            if not p.with_name(p.stem + ".prompt.txt").is_file():
                bad.append(f"scene {sid}: no prompt sidecar for {p.name}")
        segf = out / f"scene_{sid}_segments.json"
        if not segf.is_file():
            bad.append(f"scene {sid}: no {segf.name}; run `regions --scene {sid}`")
            continue
        seg = json.loads(segf.read_text(encoding="utf-8"))
        if seg.get("sha256") != att["sha256"]:
            bad.append(f"scene {sid}: {segf.name} was built from a different image; run `regions --scene {sid}`")
        if "arrows_mask" not in seg:
            bad.append(f"scene {sid}: {segf.name} predates the connector-arrow split; run `rebox --scene {sid}` with a review agent")
        elif not (out / seg["arrows_mask"]["mask"]).is_file():
            bad.append(f"scene {sid}: missing {seg['arrows_mask']['mask']}")
        elif seg.get("boxes_from") != ("rebox" if att.get("rebox") else "review"):
            bad.append(f"scene {sid}: {segf.name} was built from older boxes; run `regions --scene {sid}`")
        if [s["id"] for s in seg["segments"]] != [g["id"] for g in sc["scene"]["segments"]]:
            bad.append(f"scene {sid}: {segf.name} segments differ from the script; run `regions --scene {sid}`")
        for s in seg["segments"]:
            for f in (s["mask"], s["highlight"]):
                if not (out / f).is_file():
                    bad.append(f"scene {sid}: missing {f}")
            if s["ink_px"] == 0:
                bad.append(f"scene {sid}: segment {s['id']} has no ink (element boxes missing or wrong)")
    log_event(out, "gate", passed=not bad, problems=bad)
    if bad:
        raise Fail("gate:\n  " + "\n  ".join(bad))
    print("GATE PASS: every scene selected, verified, and at the exact scene size")
    return 0


def cmd_report(a) -> int:
    from PIL import Image
    out = Path(a.out).resolve()
    st = load_state(out)
    cmd_gate(a)
    finals = [out / f"scene_{sid}_4k.png" for sid in st["order"]]
    w, rows = 1600, []
    for p in finals:
        im = Image.open(p).convert("RGB")
        rows.append(im.resize((w, round(im.height * w / im.width))))
    sheet = Image.new("RGB", (w, sum(r.height for r in rows) + 10 * (len(rows) - 1)), (160, 160, 160))
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height + 10
    sheet.save(out / "_contact.png")
    text = write_summary(out, st)
    log_event(out, "report")
    print(text)
    print("REPORT_JSON " + json.dumps({"out": str(out), "summary": str(out / "run_summary.md"), "log": str(out / "run_log.jsonl"),
                                       "contact_sheet": str(out / "_contact.png"), "scenes": [str(p) for p in finals],
                                       "flagged": [sid for sid in st["order"] if st["scenes"][str(sid)].get("flagged")]}))
    if not a.no_open:
        for p in finals:
            os.startfile(str(p))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--style", default="")
    va = sub.add_parser("validate", help="check a script.json against the script rules, write nothing")
    va.add_argument("--scenes", required=True)
    i = sub.add_parser("init")
    i.add_argument("--scenes", required=True)
    i.add_argument("--style", required=True)
    i.add_argument("--out", required=True)
    i.add_argument("--aspect", default="16:9")
    i.add_argument("--qwen-mode", choices=["prompt", "ref"], default="prompt",
                   help="Qwen fallback for scenes after scene 1: 'prompt' = text only (default), "
                        "'ref' = scene 1 as style reference via qwen21_edit.py")
    i.add_argument("--reset", action="store_true")
    i.add_argument("--alignment", default="", help="alignment.json from narrate.py: budgets each scene by its duration")
    sy = sub.add_parser("sync", help="re-read script.json/alignment.json after a gate round")
    sy.add_argument("--out", required=True)
    sy.add_argument("--scenes", required=True)
    sy.add_argument("--alignment", default="")
    ro = sub.add_parser("reopen", help="start a new round for a scene with a Gate 1 note as its fix")
    ro.add_argument("--out", required=True)
    ro.add_argument("--scene", type=int, required=True)
    ro.add_argument("--fix", required=True)
    rg = sub.add_parser("regions", help="rebuild a selected scene's segment/arrow/residual masks and highlight images")
    rg.add_argument("--out", required=True)
    rg.add_argument("--scene", type=int, required=True)
    rf = sub.add_parser("reflag", help="re-pick each flagged scene's kept attempt with the current ranking")
    rf.add_argument("--out", required=True)
    rb = sub.add_parser("rebox", help="re-box a selected scene's image (no re-render, no re-score): "
                                      "without --file write the box sheet, with --file apply the agent's boxes")
    rb.add_argument("--out", required=True)
    rb.add_argument("--scene", type=int, required=True)
    rb.add_argument("--agent", required=True, help="name of the review agent (must differ from the renderer)")
    rb.add_argument("--file", default="", help="the review agent's filled-in boxes JSON")
    for name in ("status", "gate"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", required=True)
    r.add_argument("--scene", type=int, required=True)
    r.add_argument("--agent", required=True, help="name of the render agent (must differ from the reviewer)")
    rw = sub.add_parser("render-worker", help="internal: the detached process that performs one render attempt")
    rw.add_argument("--out", required=True)
    rw.add_argument("--scene", type=int, required=True)
    rw.add_argument("--attempt", type=int, required=True)
    rp = sub.add_parser("review-prep")
    rp.add_argument("--out", required=True)
    rp.add_argument("--scene", type=int, required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--out", required=True)
    v.add_argument("--scene", type=int, required=True)
    v.add_argument("--file", required=True, help="the reviewer's filled-in checklist")
    rep = sub.add_parser("report")
    rep.add_argument("--out", required=True)
    rep.add_argument("--no-open", action="store_true")
    g = sub.add_parser("gpu-jobs")
    g.add_argument("jobs")
    hp = sub.add_parser("render-prep", help="Higgsfield: reserve an attempt and print its generate_image arguments")
    hp.add_argument("--out", required=True)
    hp.add_argument("--scene", type=int, required=True)
    hp.add_argument("--agent", required=True, help="name of the agent that submits it (must differ from the reviewer)")
    hs = sub.add_parser("render-submitted", help="Higgsfield: record the submitted job (or an unclear outcome)")
    hs.add_argument("--out", required=True)
    hs.add_argument("--scene", type=int, required=True)
    hs.add_argument("--attempt", type=int, required=True)
    hs.add_argument("--job", default="")
    hs.add_argument("--quoted", type=float, default=None, help="the get_cost quote in credits")
    hs.add_argument("--unknown", default="", help="the submission's outcome is unclear: say what happened")
    hi = sub.add_parser("render-ingest", help="Higgsfield: download the finished picture and frame it, or record a failed job")
    hi.add_argument("--out", required=True)
    hi.add_argument("--scene", type=int, required=True)
    hi.add_argument("--attempt", type=int, required=True)
    hx = hi.add_mutually_exclusive_group(required=True)
    hx.add_argument("--url")
    hx.add_argument("--failed", help="the job failed on Higgsfield's side (it is refunded): the reason")
    a = ap.parse_args(argv)
    fn = {"doctor": cmd_doctor, "validate": cmd_validate, "init": cmd_init, "status": cmd_status, "render": cmd_render,
          "render-worker": cmd_render_worker, "render-prep": cmd_render_prep, "render-submitted": cmd_render_submitted,
          "render-ingest": cmd_render_ingest,
          "review-prep": cmd_review_prep, "verdict": cmd_verdict, "gate": cmd_gate, "report": cmd_report,
          "sync": cmd_sync, "reopen": cmd_reopen, "regions": cmd_regions, "rebox": cmd_rebox, "reflag": cmd_reflag}
    try:
        return cmd_gpu_jobs(Path(a.jobs)) if a.cmd == "gpu-jobs" else fn[a.cmd](a)
    except Fail as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
