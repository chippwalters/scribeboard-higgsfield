r"""runsetup.py - the run's answers before anything is paid for, in two records:

  RUN/story_cast.json  before Gate 0: the story's recurring characters (at most 3, {"id", "name", "look"}), listed by
                   a writer agent (pass 0) and stamped by `cast-check` with the story's path and sha256.
  RUN/start.json   Gate 0, the first page of a run (gate5.py): the story file (chosen in the file dialog of
                   `scribe.py new`; its folder is the run folder), whether to keep its words or rewrite them as
                   narration, the narrator voice, the title, the image model, the drawing style, the colours, each
                   character's sheet (from the description, from 1-3 image files with consent, or none) and the
                   run's credit budget (version START_VERSION, the only one read). Narration is recorded and the
                   character sheets are drawn from it, before Gate 1. narration.json must list exactly the start's
                   characters and carry its title.
  RUN/setup.json   Gate 1, the setup page (gate0.py): the script in its paragraphs with the recorded voice and each
                   character sheet to approve; it asks only the refined budget and copies the start's choices.
                   Scene pictures need it.

Setup is REQUIRED whenever Higgsfield makes the pictures or the narration (settings images.engine or narration.engine
'higgsfield': the scribe-higgsfield skill). Someone who installs the skill has no settings file to edit, so every choice
that shapes the video is asked on the start page (Gate 0, gate5.py) and read from here. There are no defaults and no
fallbacks:
  - without a valid start.json, scribe.py routes nothing past Gate 0, and hf.ledger_check refuses every paid job;
  - narration takes and character sheets need only start.json (require_start, require_pictures with sheet refs);
  - scene and title pictures also need setup.json (Gate 1 answered).
Each refusal names what is missing. Not required (render-scribe's own engines): every function here is unused, and
`required()` is False.

setup.json (written only by gate0.py apply, from a round that approved the narration and answered every setup question;
never by hand):
  {"version": 2, "round": R, "narration_fingerprint": "<voice fingerprint the round approved>",
   "image_model": "<image-models/higgsfield.json model id>",
   "style": "<styles_dir subfolder>", "palette": "<palettes/<id>.json stem>",
   "cast": {"<character id>": {"name", "look", "mode": "description" | "photos" | "none", "photos": [absolute paths]}},
   (mode "none": no character sheet; each scene draws the character from the script's description, no reference)
   "cast_fingerprint": "<sha256 of narration.json characters>", "consent": true | false,
   "budget": {"cap": <credits>, "estimate": {"lowest", "expected", "ceiling", ...}}}
image_model, style, palette, cast and consent are copied from start.json (choices() reads them from there): they never
change once the start page is answered (one run, one look).

The cast comes from the start page: writer pass 0 lists the story's characters (story_cast.json), Gate 0 asks about
each, and start.json holds them. narration.json "characters" must copy them exactly (voice.validate_narration refuses
any difference at the narration step), and the setup holds only while its cast_fingerprint matches narration.json.

Catalogs, shipped with the skill (no machine paths):
  styles_dir/<style>/   style_guide.md, module.json and sample.png (the picture the style question shows)
  palettes/<id>.json    2, 3 or 4 colours (roles ink, main, second, third), with its card palettes/<id>.png
  voices/higgsfield.json  the narrator voices: Higgsfield preset ids with their free preview clips
  image-models/higgsfield.json  the picture models: each one's generate_image params and price per picture

CLI:  python scripts/runsetup.py check --run RUN     prints SETUP_JSON {"ok", "problems", "setup"}; exit 1 when not ok
      python scripts/runsetup.py cast-check --run RUN [--story FILE]
                                                   checks RUN/story_cast.json (writer pass 0: {"written_by",
                                                   "characters"}), stamps it with the story; prints CAST_JSON
      python scripts/runsetup.py catalog            prints CATALOG_JSON (styles, palettes, voices) after checking each
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

import settings as cfg

REPO = Path(__file__).resolve().parents[1]
PALETTES = REPO / "palettes"
VOICES = REPO / "voices" / "higgsfield.json"
IMAGE_MODELS = REPO / "image-models" / "higgsfield.json"
SETUP_VERSION = 2                  # setup.json (the only version read)
SETUP = "setup.json"
START = "start.json"
SOURCE = "source.md"          # the script as the user gave it on the start page, copied into the run folder
STORY_CAST = "story_cast.json"   # the story's recurring characters, listed by a writer agent (pass 0) before Gate 0
START_VERSION = 3             # start.json (the only version read): it holds every choice, so the character sheets are
#                               drawn before Gate 1
TITLE_MAX = 120               # characters in the video's title (the title card and the narration's title)
WORDS = {"keep": "Keep my words exactly (only split into paragraphs and sentences)",
         "rewrite": "Rewrite them as narration (the writer retells the story; a reviewer checks it)"}
START_GATE = 5                # gates/gate5-rN: the start page's round folders
VERSION = 1
ROLES = ("ink", "main", "second", "third")       # a named palette's roles, in this order: 2, 3 or 4 of them
MAX_PHOTOS = 3
MAX_CHARACTERS = 3   # recurring characters per video
CAST_MODES = ("description", "photos", "none")   # a sheet drawn from the description / from photos, or no sheet
PHOTO_TYPES = (".jpg", ".jpeg", ".png", ".webp")
CHAR_ID = re.compile(r"^(?=.{1,32}$)[a-z][a-z0-9]*(_[a-z0-9]+)*$")   # no "__" or trailing "_": qid() stays unique

# Credit estimate (the setup page's range). Measured 2026-11-09 with get_cost (.orchestrator/higgsfield-quotes-
# 2026-11-09.txt): a seed_audio take is 0.5 credits at 11 words plus about 0.035 per extra word; a picture costs
# settings higgsfield.image_credits. Attempts per job: expected = earlier runs' first-round average (1.45 per scene
# and title), ceiling = the limits of one round (5 per scene and title, 3 per sheet, 3 takes per sentence).
AUDIO_BASE, AUDIO_BASE_WORDS, AUDIO_PER_WORD = 0.5, 11, 0.035
EXPECTED_ATTEMPTS, CEILING_ATTEMPTS, CEILING_SHEETS, CEILING_TAKES = 1.45, 5, 3, 3


class SetupError(Exception):
    pass


def required() -> bool:
    """Is a setup page required? Yes whenever Higgsfield makes the pictures or the narration."""
    return cfg.get("images.engine") == "higgsfield" or cfg.get("narration.engine") == "higgsfield"


def _read(p: Path, what: str):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetupError(f"{what} not found: {p}")
    except (OSError, ValueError) as e:
        raise SetupError(f"{what} {p} cannot be read: {e}")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------ catalogs

def styles() -> dict[str, dict]:
    """{style id: {"label", "sample"}} for every complete style folder (style guide, module.json, sample.png)."""
    root = cfg.path("styles_dir")
    out = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root and root.is_dir() else []:
        guide, sample = d / "style_guide.md", d / "sample.png"
        if not (guide.is_file() and (d / "module.json").is_file() and sample.is_file()):
            continue
        head = next((ln for ln in guide.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), "")
        out[d.name] = {"label": head.split(":", 1)[-1].strip() or d.name, "sample": sample}
    return out


def ink_threshold(style: str | None = None) -> float | None:
    """The style's module.json ink_threshold; without a style, the lowest of all styles (a palette must suit each)."""
    root = cfg.path("styles_dir")
    names = [style] if style else list(styles())
    vals = [_read(root / s / "module.json", "module.json").get("ink_threshold") for s in names]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return min(vals) if vals else None


def palettes() -> dict[str, dict]:
    """{palette id: {"label", "description", "card", "roles", "path"}}, each checked (palette.load_file)."""
    import palette   # palette imports this module: import here, not at the top
    out = {}
    thr = ink_threshold()
    for p in sorted(PALETTES.glob("*.json")) if PALETTES.is_dir() else []:
        try:
            pal = palette.load_file(p, thr, "the styles' lowest ink_threshold")
        except palette.PaletteError as e:
            raise SetupError(f"palette {p.stem}: {e}") from e
        roles = tuple(pal["colours"])
        if roles != ROLES[:len(roles)] or len(roles) < 2:
            raise SetupError(f"palette {p.stem}: roles must be {', '.join(ROLES[:2])} (+ second, third) in that order, "
                             f"not {', '.join(roles)}")
        card = p.with_suffix(".png")
        if not card.is_file():
            raise SetupError(f"palette {p.stem}: its card {card} is missing (scripts/palette_cards.py draws it)")
        out[p.stem] = {"label": pal["label"] or p.stem, "description": pal["description"] or "", "card": card,
                       "roles": roles, "path": p, "colours": pal["colours"]}
    return out


def voices() -> dict:
    """voices/higgsfield.json, checked: {"model", "default", "voices": {id: {...}}}."""
    doc = _read(VOICES, "the voice catalog")
    vs = {}
    for v in doc.get("voices") or []:
        if not all(isinstance(v.get(k), str) and v[k] for k in ("id", "label", "voice_id", "voice_type", "preview_url")):
            raise SetupError(f"{VOICES}: every voice needs id, label, voice_id, voice_type and preview_url: {v!r}")
        if not v["preview_url"].startswith("https://"):
            raise SetupError(f"{VOICES}: voice {v['id']}: preview_url must be https")
        vs[v["id"]] = v
    if not vs or not isinstance(doc.get("model"), str):
        raise SetupError(f"{VOICES}: needs a model and at least one voice")
    if doc.get("default") not in vs:
        raise SetupError(f"{VOICES}: default {doc.get('default')!r} is not one of its voices")
    return {"model": doc["model"], "default": doc["default"], "voices": vs}


IMAGE_PARAM_RESERVED = ("prompt", "aspect_ratio", "count", "use_unlim", "medias", "get_cost")


def image_models() -> dict:
    """image-models/higgsfield.json, checked: {"default", "models": {id: {"label", "description", "params", "credits"}}}
    in the file's order."""
    doc = _read(IMAGE_MODELS, "the image model catalog")
    ms = {}
    for m in doc.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if not (isinstance(mid, str) and mid and isinstance(m.get("label"), str) and m["label"]):
            raise SetupError(f"{IMAGE_MODELS}: every model needs an id and a label: {m!r}")
        if mid in ms:
            raise SetupError(f"{IMAGE_MODELS}: model id {mid} is used twice")
        params = m.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("model"), str) or not params["model"]:
            raise SetupError(f"{IMAGE_MODELS}: model {mid}: params needs the Higgsfield model id (params.model)")
        clash = [k for k in IMAGE_PARAM_RESERVED if k in params]
        if clash:
            raise SetupError(f"{IMAGE_MODELS}: model {mid}: params may not set {', '.join(clash)} (the pipeline does)")
        cr = m.get("credits")
        if not isinstance(cr, (int, float)) or isinstance(cr, bool) or not math.isfinite(cr) or cr <= 0:
            raise SetupError(f"{IMAGE_MODELS}: model {mid}: credits must be a positive number (one picture's price)")
        ms[mid] = {"label": m["label"], "description": m.get("description") or "", "params": dict(params),
                   "credits": float(cr)}
    if not ms:
        raise SetupError(f"{IMAGE_MODELS}: needs at least one model")
    if doc.get("default") not in ms:
        raise SetupError(f"{IMAGE_MODELS}: default {doc.get('default')!r} is not one of its models")
    return {"default": doc["default"], "models": ms}


# ------------------------------------------------------------------ start.json (Gate 0, the first page)

STORY_TYPES = (".md", ".txt")


def runs_home() -> Path:
    """Where the story dialog opens: settings runs_home, else Videos/Scribeboard in the user's home folder."""
    return cfg.path("runs_home") or (Path.home() / "Videos" / "Scribeboard")


# Every name the pipeline writes into a run folder (compared without case: Windows and macOS ignore it). A story
# folder that already holds one of them, before its video started, belongs to something else.
RUN_NAMES = {SOURCE, START, SETUP, STORY_CAST, "narration.json", "script.json", "credits.json", "credits.lock", "alignment.json",
             "budget.json", "narration", "scenes", "characters", "title", "videos", "gates", "voice"}
# (pronunciations.json is the user's own file, only read by the pipeline: it may sit beside the story)


def story_problem(story: Path) -> str | None:
    """Why `story` cannot start a video, else None. The video's files go into the story's own folder, so before its
    video starts that folder may hold nothing the pipeline writes (no user file is ever overwritten)."""
    if not story.is_absolute():
        return f"the story file must be given by its full path, not {story}"
    if story.suffix.lower() not in STORY_TYPES:
        return f"the story must be a .md or .txt file, not {story.name}"
    if not story.is_file():
        return f"the story file was not found: {story}"
    if story.name.lower() in {n.lower() for n in RUN_NAMES}:
        return f"{story.name} is a name the run uses for its own files; rename the story file"
    folder = story.parent
    if REPO.resolve() == folder.resolve() or REPO.resolve() in folder.resolve().parents:
        return f"{story} is inside the skill's own folder; put the story in a folder of its own"
    try:
        story.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"{story.name} is not UTF-8 text; save it as UTF-8 (in Notepad: Save as, Encoding UTF-8) and start again"
    earlier = previous_start(folder)
    if earlier.get("story") and Path(earlier["story"]).resolve() != story.resolve():
        return (f"{folder} already holds the video of {earlier['story']}; every video's files go into its "
                f"story's folder, so put this story in a folder of its own")
    if not earlier:   # not started here yet: nothing the pipeline writes may already be in the folder
        names = {n.lower() for n in RUN_NAMES}
        taken = []
        for p in folder.iterdir():
            if p.name.lower() not in names:
                continue
            if p.name.lower() == "gates" and p.is_dir() and all(
                    c.name.startswith(f"gate{START_GATE}-r") or c.name == "gates.js" for c in p.iterdir()):
                continue   # this video's own unanswered start page
            if p.name.lower() == STORY_CAST.lower():
                continue   # the characters listed before the start page (story_cast checks it names this story)
            taken.append(p.name)
        if taken:
            return (f"the story's folder {folder} already holds {', '.join(sorted(taken))}, which this video would "
                    f"write over; put the story in a folder of its own")
    return None


def folder_story(run: Path) -> Path | None:
    """The story of a run folder: the one the start page recorded, else the folder's only .md / .txt file."""
    earlier = previous_start(run).get("story")
    if earlier:
        return Path(earlier)
    found = [p for p in run.glob("*") if p.is_file() and p.suffix.lower() in STORY_TYPES
             and p.name.lower() != SOURCE.lower()]
    return found[0] if len(found) == 1 else None


def start_path(run: Path) -> Path:
    return run / START


def _cap_problem(cap) -> str | None:
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not math.isfinite(cap) or cap <= 0:
        return f"budget.cap must be a positive, finite number of credits, not {cap!r}"
    return None


def look_problems(doc: dict) -> list[str]:
    """The title, style and palette of a start record."""
    out = []
    t = doc.get("title")
    if not (isinstance(t, str) and t.strip()) or len(t) > TITLE_MAX or "\n" in t:
        out.append(f"title must be one line of 1 to {TITLE_MAX} characters, not {t!r}")
    sty = styles()
    if doc.get("style") not in sty:
        out.append(f"style {doc.get('style')!r} is not one of {', '.join(sty) or '(no complete style folder)'}")
    try:
        pals = palettes()
        if doc.get("palette") not in pals:
            out.append(f"palette {doc.get('palette')!r} is not one of {', '.join(pals)}")
    except SetupError as e:
        out.append(str(e))
    return out


def story_title(text: str) -> str:
    """The story's first Markdown heading (the title question's default), else ""."""
    m = next((re.match(r"^\s*#{1,6}\s+(.+?)(?:\s+#+)?\s*$", ln) for ln in text.splitlines()
              if re.match(r"^\s*#{1,6}\s", ln)), None)
    return m.group(1)[:TITLE_MAX] if m else ""


def style_question(prev_style, prompt_help: str) -> dict:
    sty = styles()
    if not sty:
        raise SetupError(f"no complete style folder in {cfg.path('styles_dir')} (style_guide.md, module.json, sample.png)")
    return {"id": "style", "type": "single", "prompt": "Drawing style", "required": True, "allowNote": False,
            "help": prompt_help,
            "options": [{"value": s, "label": v["label"], "selected": prev_style == s,
                         "media": [{"type": "image", "path": str(v["sample"]), "alt": f"{v['label']} sample"}]}
                        for s, v in sty.items()]}


def palette_question(prev_palette) -> dict:
    pals = palettes()
    if not pals:
        raise SetupError(f"no palettes in {PALETTES}")
    first = "toolmaker" if "toolmaker" in pals else next(iter(pals))
    return {"id": "palette", "type": "single", "prompt": "Colours", "required": True, "allowNote": False,
            "help": "The marker colours of every picture, the character sheets and the board's flow arrows. The board "
                    "is always white.",
            "options": [{"value": p, "label": v["label"], "description": v["description"],
                         "selected": (prev_palette or first) == p,
                         "media": [{"type": "image", "path": str(v["card"]), "alt": f"{v['label']} palette card"}]}
                        for p, v in pals.items()]}


def story_cast_path(run: Path) -> Path:
    return run / STORY_CAST


def cast_check(run: Path, story: Path) -> list[dict]:
    """Check RUN/story_cast.json as the cast finder (writer agent, pass 0) wrote it, {"written_by", "characters"},
    and stamp it with the story it lists (path and sha256): the start page asks about exactly these characters.
    Returns the characters; raises SetupError naming every problem."""
    p = story_cast_path(run)
    if not p.is_file():
        raise SetupError(f"{p} is missing: before the start page, a writer agent reads the story and lists its "
                         f"recurring characters there (SCRIPT-WRITER.md, pass 0)")
    doc = _read(p, STORY_CAST)
    if not isinstance(doc, dict):
        raise SetupError(f"{p} must be a JSON object {{\"written_by\", \"characters\"}}")
    errs = []
    if set(doc) - {"version", "written_by", "characters", "story", "story_sha256"}:
        errs.append(f"unknown fields {', '.join(sorted(set(doc) - {'version', 'written_by', 'characters', 'story', 'story_sha256'}))}")
    if not (isinstance(doc.get("written_by"), str) and doc["written_by"].strip()):
        errs.append("written_by must be the cast finder's agent name")
    try:
        chars = narration_cast(doc)
    except SetupError as e:
        errs.append(str(e).replace("narration.json characters", "characters"))
        chars = []
    if errs:
        raise SetupError(f"{p}: " + "; ".join(errs))
    doc.update(version=1, story=str(story), story_sha256=hashlib.sha256(story.read_bytes()).hexdigest())
    write_json_atomic(p, doc)
    return chars


def story_cast(run: Path, story: Path) -> list[dict]:
    """The characters the cast finder listed for `story`, as `cast_check` stamped them; raises SetupError when the
    file is missing, unchecked, for another story or for an older version of this one."""
    p = story_cast_path(run)
    if not p.is_file():
        raise SetupError(f"{p} is missing: before the start page, a writer agent reads the story and lists its "
                         f"recurring characters there (SCRIPT-WRITER.md, pass 0), then `runsetup.py cast-check`")
    doc = _read(p, STORY_CAST)
    if not isinstance(doc, dict) or doc.get("version") != 1 or not doc.get("story_sha256"):
        raise SetupError(f"{p} has not been checked yet: run `runsetup.py cast-check --run {run.as_posix()}`")
    if Path(str(doc.get("story"))).resolve() != story.resolve():
        raise SetupError(f"{p} lists the characters of {doc.get('story')}, not of {story}; list them again")
    if doc["story_sha256"] != hashlib.sha256(story.read_bytes()).hexdigest():
        raise SetupError(f"{story.name} changed since its characters were listed in {p}; list them again (pass 0) "
                         f"and run `runsetup.py cast-check`")
    return narration_cast(doc)


def start_round_meta(story: Path, chars: list[dict]) -> dict:
    """What a start page round was built from (stored in its gate.json): the story's checksum and its cast."""
    return {"story_sha256": hashlib.sha256(story.read_bytes()).hexdigest(), "cast_fingerprint": cast_fingerprint(chars)}


def start_round_problem(run: Path, pointer: dict) -> str | None:
    """Why an unanswered start page round can no longer be applied (the story or its listed characters changed since
    it was built), else None. The round is then superseded by the next one."""
    story = Path(str(pointer.get("story") or ""))
    if not story.is_file():
        return f"its story file {story} is gone"
    try:
        chars = story_cast(run, story)
    except SetupError as e:
        return str(e)
    if start_round_meta(story, chars) != {k: pointer.get(k) for k in ("story_sha256", "cast_fingerprint")}:
        return "the story or its listed characters changed since it was built"
    return None


def start_title(run: Path) -> str:
    """The video's title the user gave on the start page."""
    return load_start(run)["title"]


def start_cast(run: Path) -> list[dict]:
    """The characters the start page asked about ([{"id", "name", "look"}]), which narration.json must list exactly."""
    return [{"id": cid, "name": c["name"], "look": c["look"]} for cid, c in load_start(run)["cast"].items()]


def start_problems(run: Path, doc: dict) -> list[str]:
    out = []
    if doc.get("version") != START_VERSION:
        out.append(f"version must be {START_VERSION}")
    else:
        out += look_problems(doc)
        try:
            ims = image_models()["models"]
            if doc.get("image_model") not in ims:
                out.append(f"image_model {doc.get('image_model')!r} is not one of {', '.join(ims)}")
        except SetupError as e:
            out.append(str(e))
        cast = doc.get("cast")
        out += cast_problems(cast, doc.get("consent"), check_files=False)   # files were checked when answered
        if isinstance(cast, dict):
            if len(cast) > MAX_CHARACTERS:
                out.append(f"cast: at most {MAX_CHARACTERS} characters")
            listed = [{"id": k, "name": str((v or {}).get("name", "")), "look": str((v or {}).get("look", ""))}
                      for k, v in cast.items()]
            if doc.get("cast_fingerprint") != cast_fingerprint(listed):
                out.append("cast_fingerprint does not match the cast")
    if doc.get("words") not in WORDS:
        out.append(f"words must be one of {', '.join(WORDS)}, not {doc.get('words')!r}")
    try:
        if doc.get("voice") not in voices()["voices"]:
            out.append(f"voice {doc.get('voice')!r} is not one of the catalog's voices")
    except SetupError as e:
        out.append(str(e))
    src = run / SOURCE
    if not src.is_file():
        out.append(f"the script file {src} is missing")
    elif hashlib.sha256(src.read_bytes()).hexdigest() != doc.get("source_sha256"):
        out.append(f"{src} changed since the start page was answered (answer the start page again for a new script)")
    cap_err = _cap_problem((doc.get("budget") or {}).get("cap"))
    if cap_err:
        out.append(cap_err)
    return out


def load_start(run: Path) -> dict:
    """The run's start.json, checked, and traced to the approving start-page round; raises SetupError."""
    p = start_path(run)
    if not p.is_file():
        raise SetupError(f"this run has not been started yet ({p} is missing): Gate 0 asks for the script, the narrator "
                         f"voice and the credit budget. Follow `scribe.py status --run {run.as_posix()}`; nothing is "
                         f"paid for before it is answered")
    doc = _read(p, "start.json")
    if not isinstance(doc, dict):
        raise SetupError(f"{p} is not a start record (it must be a JSON object written by gate5.py apply)")
    errs = start_problems(run, doc)
    r = doc.get("round")
    ch_p = run / "gates" / f"gate{START_GATE}-r{r}" / "changes.json"
    try:
        ch = json.loads(ch_p.read_text(encoding="utf-8")) if ch_p.is_file() else None
    except (OSError, ValueError):
        ch = None
    if not isinstance(ch, dict) or ch.get("approved") is not True or not isinstance(ch.get("start"), dict) \
            or _core(ch["start"]) != _core(doc):
        errs.append(f"it does not match what start-page round {r} recorded (start.json is written only by gate5.py "
                    f"apply)")
    if errs:
        raise SetupError(f"{p} is not a complete start: " + "; ".join(errs))
    return doc


def source_text(run: Path) -> str:
    return (run / SOURCE).read_text(encoding="utf-8")


def default_image_credits() -> float:
    """One picture's price at the default image model (image-models/higgsfield.json)."""
    cat = image_models()
    return cat["models"][cat["default"]]["credits"]


def story_estimate(text: str, image: float | None = None, sheets: int = 0) -> dict:
    """The credit estimate for a story before it is split: its word count (Markdown headings left out), about one
    sentence per 11 words and one scene per two sentences, plus `sheets` character sheets, at `image` credits a
    picture (default: the default image model's). Gate 1 shows the exact one."""
    body = "\n".join(ln for ln in text.splitlines() if not re.match(r"^\s*#{1,6}\s", ln))
    words = len(_words(body))
    image = default_image_credits() if image is None else image
    sentences = max(1, round(words / AUDIO_BASE_WORDS))
    scenes = max(1, round(sentences / 2))
    audio = sentences * AUDIO_BASE
    low = (scenes + 1 + sheets) * image + audio
    exp = EXPECTED_ATTEMPTS * (scenes + 1) * image + sheets * image + audio
    ceil_ = CEILING_ATTEMPTS * (scenes + 1) * image + CEILING_SHEETS * sheets * image + CEILING_TAKES * audio
    return {"words": words, "sentences": sentences, "scenes": scenes, "sheets": sheets, "image_credits": image,
            "audio": round(audio, 1), "lowest": round(low, 1), "expected": round(exp, 1), "ceiling": round(ceil_, 1),
            "default_cap": float(math.ceil(exp))}


def start_questions(run: Path, prev: dict, story_text: str, chars: list[dict]) -> list[dict]:
    """The start page's questions. The story itself was chosen in the file dialog (scribe.py new); the page names it,
    and the budget question shows its word count and the estimate for it. chars: the story's characters as the cast
    finder listed them (story_cast); the page asks each character's sheet."""
    prev = prev or {}
    vcat = voices()
    icat = image_models()
    pick = prev.get("image_model") if prev.get("image_model") in icat["models"] else icat["default"]
    n = len(chars)
    ests = {mid: story_estimate(story_text, m["credits"], n) for mid, m in icat["models"].items()}
    est = ests[pick]
    extra = [{"id": "title", "type": "text", "prompt": "Title of this video", "required": True,
              "help": f"The title card at the start of the video shows it, and the narration carries it. One line, at "
                      f"most {TITLE_MAX} characters.",
              "default": prev.get("title") or story_title(story_text),
              "placeholder": "for example: The Tanner's Son Who Saw the Invisible"},
             {"id": "image-model", "type": "single", "prompt": "Image model", "required": True, "allowNote": False,
              "help": "Which model draws every picture of this video (scenes, title card, character sheets).",
              "options": [{"value": mid, "label": f"{m['label']}, {m['credits']:g} credits a picture",
                           "description": m["description"], "selected": pick == mid}
                          for mid, m in icat["models"].items()]},
             style_question(prev.get("style"), "How every picture is drawn, the character sheets included. The "
                                               "colours are chosen separately below."),
             palette_question(prev.get("palette"))]
    extra += cast_questions(chars, prev.get("cast") or {}, prev.get("consent"))
    per_model = "\n".join(f"- **{icat['models'][mid]['label']}**: lowest {e['lowest']}, expected {e['expected']}, "
                          f"ceiling {e['ceiling']}" for mid, e in ests.items())
    sheets_txt = (f"{n} character sheet(s) if every character gets one (no sheet costs nothing)" if n else
                  "no characters")
    budget_q = {"id": "budget", "type": "text", "prompt": "Credit budget for this run", "required": True,
                "default": str((prev.get("budget") or {}).get("cap") or int(est["default_cap"])),
                "placeholder": "credits, for example 40",
                "body": (f"**Your story has {est['words']} words.** That is about {est['sentences']} narration "
                         f"sentences and {est['scenes']} scenes plus the title card, and {sheets_txt}. By image "
                         f"model (lowest: every picture and take right first time; ceiling: every limit reached "
                         f"once):\n\n{per_model}\n\nNarration is about {est['audio']} credits of that. These "
                         f"figures count **your words as written**; with **Rewrite** the narration may be shorter "
                         f"or longer. Before Gate 1 the only spend is the narration; Gate 1 shows the exact "
                         f"figures and you set the budget again there, before any picture is made.\n\nThe run stops "
                         f"and asks before any job that would pass the budget.")}
    head = [
        {"id": "words", "type": "single", "prompt": "Your words", "required": True, "allowNote": False,
         "options": [{"value": k, "label": v, "selected": prev.get("words") == k} for k, v in WORDS.items()]},
        {"id": "voice", "type": "single", "prompt": "Narrator voice", "required": True, "allowNote": False,
         "help": "Play the samples. The narration is recorded in this voice and played to you on Gate 1.",
         "options": [{"value": i, "label": v["label"], "description": v.get("gender", ""),
                      "selected": prev.get("voice", vcat["default"]) == i,
                      "media": [{"type": "audio", "path": v["preview_url"], "alt": f"{v['label']} sample"}]}
                     for i, v in vcat["voices"].items()]}]
    return head + extra + [budget_q]


def start_from_answers(run: Path, responses: dict, round_: int, story: Path) -> tuple[dict, str, list[str]]:
    """(start doc, story text, problems) from the start page's answers. The story is the file chosen in the dialog;
    its folder is the run folder."""
    def val(q: str):
        r = responses.get(q)
        return None if r is None else r.get("value")

    errs = []
    text = ""
    why = story_problem(story)
    if why:
        errs.append(why)
    elif story.parent.resolve() != run.resolve():
        errs.append(f"the story {story} is not in this video's folder {run}")
    else:
        text = story.read_text(encoding="utf-8-sig")
        if not text.strip():
            errs.append(f"the story file {story} is empty")
    budget_raw = str(val("budget") or "").strip().replace(",", "")
    try:
        cap_ = float(budget_raw)
    except ValueError:
        cap_ = None
        errs.append(f"the credit budget must be a number, not {budget_raw!r}")
    if cap_ is not None and _cap_problem(cap_):
        errs.append(_cap_problem(cap_))
    if val("words") not in WORDS:
        errs.append("choose whether to keep your words or rewrite them")
    try:
        if val("voice") not in voices()["voices"]:
            errs.append("choose a narrator voice")
    except SetupError as e:
        errs.append(str(e))
    chars: list[dict] = []
    if text:
        try:
            chars = story_cast(run, story)
        except SetupError as e:
            errs.append(str(e))
    model = val("image-model")
    if not (isinstance(model, str) and model):
        errs.append("choose the image model")
        model = None
    cast, consent, cerrs = cast_from_answers(chars, val)
    errs += cerrs
    title = str(val("title") or "").strip()
    doc = {"version": START_VERSION, "round": round_, "story": str(story),
           "source": SOURCE, "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
           "words": val("words"), "voice": val("voice"), "title": title, "image_model": model,
           "style": val("style"), "palette": val("palette"), "cast": cast,
           "cast_fingerprint": cast_fingerprint(chars), "consent": consent, "budget": {"cap": cap_}}
    errs += look_problems(doc)
    if model is not None:
        try:
            if model not in image_models()["models"]:
                errs.append(f"image_model {model!r} is not one of {', '.join(image_models()['models'])}")
        except SetupError as e:
            errs.append(str(e))
    errs += cast_problems(cast, consent)
    before = previous_start(run)
    started = (run / "narration.json").is_file() or (run / "narration").is_dir() or (run / "characters").is_dir()
    if started:
        for k, what in (("source_sha256", "version of the story"), ("voice", "voice"), ("words", "keep-or-rewrite "
                                                                                                 "choice"),
                        ("title", "title"), ("image_model", "image model"), ("style", "drawing style"),
                        ("palette", "colours"), ("cast", "characters' answers")):
            if before.get(k) not in (None, doc.get(k)):
                errs.append(f"this video's narration was made with another {what}; a change needs a new video (the "
                            f"story in a folder of its own)")
    return doc, text, errs


def previous_start(run: Path) -> dict:
    """start.json, else the newest approving start-page round's record (a deleted start.json still counts)."""
    p = start_path(run)
    try:
        if p.is_file():
            doc = _read(p, "start.json")
            if isinstance(doc, dict):
                return doc
    except SetupError:
        pass
    gates = run / "gates"
    for r in sorted((int(d.name.split("-r")[1]) for d in gates.glob(f"gate{START_GATE}-r*") if d.is_dir()),
                    reverse=True) if gates.is_dir() else []:
        ch = gates / f"gate{START_GATE}-r{r}" / "changes.json"
        try:
            doc = json.loads(ch.read_text(encoding="utf-8")) if ch.is_file() else {}
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("approved") and isinstance(doc.get("start"), dict):
            return doc["start"]
    return {}


def write_start(run: Path, doc: dict, text: str) -> None:
    """Writes RUN/source.md (the script) and RUN/start.json. Only gate5.py apply calls this."""
    src = run / SOURCE
    data = text.encode("utf-8")              # bytes as hashed in start.json (no newline translation)
    if not src.is_file() or src.read_bytes() != data:
        src.write_bytes(data)
    write_json_atomic(start_path(run), doc)


def write_json_atomic(p: Path, doc: dict) -> None:
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def words_mode(run: Path) -> str:
    return load_start(run)["words"]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text.lower().replace("’", "'").replace("‘", "'"))


def verbatim_problems(narration: dict, source: str) -> list[str]:
    """"Keep my words": the narration's sentences, in order, must be the script's words exactly. Punctuation and
    capitals may change (a long sentence may be split at a comma); Markdown heading lines (a title) are left out.
    Returns the first difference with its context, or []."""
    body = "\n".join(ln for ln in source.splitlines() if not re.match(r"^\s*#{1,6}\s", ln))   # Markdown headings
    want = _words(body)
    paras = sorted((p for p in narration.get("paragraphs") or [] if isinstance(p, dict)),
                   key=lambda p: p.get("id") if isinstance(p.get("id"), int) else 0)
    have = _words(" ".join(str(g.get("text", "")) for p in paras for g in p.get("sentences") or []))
    if have == want:
        return []
    i = next((k for k in range(min(len(have), len(want))) if have[k] != want[k]), min(len(have), len(want)))
    return [f"keep my words: the narration must be the script word for word; it differs at word {i + 1}: script "
            f"'{' '.join(want[max(0, i - 4):i + 6])}', narration '{' '.join(have[max(0, i - 4):i + 6])}' (split into "
            f"paragraphs and sentences only; change no word)"]


# ------------------------------------------------------------------ the cast (narration.json, writer pass 1)

def narration_cast(doc: dict) -> list[dict]:
    """narration.json "characters", checked: [{"id", "name", "look"}] (may be empty). Raises SetupError."""
    chars = doc.get("characters") if isinstance(doc, dict) else None
    if not isinstance(chars, list):
        raise SetupError('narration.json needs "characters": the recurring characters of the story, each '
                         '{"id", "name", "look"} (an empty list when there are none); the setup page asks about each '
                         '(writer pass 1, SCRIPT-WRITER.md)')
    errs, seen = [], set()
    if len(chars) > MAX_CHARACTERS:
        errs.append(f"{len(chars)} characters listed; a video has at most {MAX_CHARACTERS} (keep the ones in the "
                    f"most scenes, SCRIPT-WRITER.md)")
    for c in chars:
        if not isinstance(c, dict) or set(c) - {"id", "name", "look"}:
            errs.append(f"character {c!r}: only id, name and look")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not CHAR_ID.fullmatch(cid):
            errs.append(f"character id {cid!r}: lowercase letters and digits, starting with a letter, words joined "
                        f"by a single _ (not at the end), at most 32 characters")
        elif cid in seen:
            errs.append(f"character id {cid} is used twice")
        seen.add(cid)
        for k in ("name", "look"):
            if not (isinstance(c.get(k), str) and c[k].strip()):
                errs.append(f"character {cid}: {k} must be a non-empty string")
    if errs:
        raise SetupError("narration.json characters: " + "; ".join(errs))
    return chars


def cast_fingerprint(chars: list[dict]) -> str:
    return _sha([[c["id"], c["name"].strip(), c["look"].strip()] for c in chars])


# ------------------------------------------------------------------ the estimate

def audio_price(words: int) -> float:
    return AUDIO_BASE + AUDIO_PER_WORD * max(0, words - AUDIO_BASE_WORDS)


def estimate(doc: dict, image: float | None = None) -> dict:
    """The credit range for a narration.json: lowest (everything passes first time), expected, ceiling (one round at
    every limit). Counts: N scenes (paragraphs) + the title, C characters, S sentences. image: one picture's credits
    (the image model's catalog price); None = settings higgsfield.image_credits."""
    if image is None:
        image = cfg.get("higgsfield.image_credits")
        if not isinstance(image, (int, float)) or image <= 0:
            raise SetupError(f"{cfg.where('higgsfield.image_credits')} must be a positive number (one picture's "
                             f"credits)")
    paras = doc.get("paragraphs") or []
    words = [len(str(g.get("text", "")).split()) for p in paras for g in p.get("sentences", [])]
    n, c, s = len(paras), len(doc.get("characters") or []), len(words)
    audio = sum(audio_price(w) for w in words)
    pictures = n + 1
    low = pictures * image + c * image + audio
    exp = EXPECTED_ATTEMPTS * pictures * image + c * image + audio
    ceil_ = CEILING_ATTEMPTS * pictures * image + CEILING_SHEETS * c * image + CEILING_TAKES * audio
    return {"scenes": n, "characters": c, "sentences": s, "image_credits": image, "audio_credits": round(audio, 2),
            "lowest": round(low, 1), "expected": round(exp, 1), "ceiling": round(ceil_, 1),
            "default_cap": float(math.ceil(exp))}


# ------------------------------------------------------------------ setup.json

def path(run: Path) -> Path:
    return run / SETUP


def cast_problems(cast, consent, check_files: bool = True) -> list[str]:
    """Every reason a cast answer ({id: {"name", "look", "mode", "photos"}}) is not valid, with its consent box.
    check_files: the photos must exist (when the answer is given); a stored record is checked for its shape only, so a
    photo moved or deleted after its sheet was made never breaks the run."""
    out = []
    if not isinstance(cast, dict):
        return ["cast must be an object (one entry per character, {} for none)"]
    any_photos = False
    for cid, c in cast.items():
        if not isinstance(c, dict):
            out.append(f"character {cid}: must be an object with mode and photos")
            continue
        mode = c.get("mode")
        if mode not in CAST_MODES:
            out.append(f"character {cid}: mode must be one of {', '.join(CAST_MODES)}, not {mode!r}")
            continue
        photos = c.get("photos") or []
        if mode in ("description", "none") and photos:
            out.append(f"character {cid}: {'drawn from the description' if mode == 'description' else 'no sheet'}, "
                       f"so no photos")
        if mode == "photos":
            any_photos = True
            if not 1 <= len(photos) <= MAX_PHOTOS:
                out.append(f"character {cid}: give 1 to {MAX_PHOTOS} photos (the first one a front view), not {len(photos)}")
            for ph in photos:
                p = Path(ph)
                if not p.is_absolute():
                    out.append(f"character {cid}: give the photo's full path (with its drive or root), not {ph}")
                elif check_files and not p.is_file():
                    out.append(f"character {cid}: photo not found: {ph}")
                elif p.suffix.lower() not in PHOTO_TYPES:
                    out.append(f"character {cid}: {p.name} is not a {'/'.join(PHOTO_TYPES)} photo")
    if any_photos and consent is not True:
        out.append("photos of real people need the consent box ticked (every person shown agreed to be drawn)")
    return out


def cast_questions(chars: list[dict], pcast: dict, pconsent) -> list[dict]:
    """One question per character (a sheet from the description, a sheet from photos, or no sheet), its photo paths,
    and the consent box. Previous answers are preselected; nothing else is."""
    qs = []
    for c in chars:
        mode_q, photo_q = qid("cast", c["id"]), qid("photos", c["id"])
        was = pcast.get(c["id"]) or {}
        qs.append({"id": mode_q, "type": "single", "prompt": f"Character: {c['name']}", "required": True,
                   "allowNote": False, "body": f"The writer describes {c['name']} as: {c['look']}",
                   "help": "A character sheet is one extra picture that every scene showing the character copies, so "
                           "the character looks the same throughout. It is optional.",
                   "options": [{"value": "description", "label": "Character sheet, drawn from the description",
                                "selected": was.get("mode") == "description"},
                               {"value": "photos", "label": f"Character sheet, drawn from image files of a real "
                                                           f"person (1 to {MAX_PHOTOS}, give the paths below)",
                                "selected": was.get("mode") == "photos"},
                               {"value": "none", "label": "No character sheet (each scene draws the character from "
                                                          "the description; the look may vary between scenes)",
                                "selected": was.get("mode") == "none"}]})
        qs.append({"id": photo_q, "type": "text", "prompt": f"Image files of {c['name']}",
                   "help": f"Only for \"drawn from image files\": 1 to {MAX_PHOTOS} full paths to .jpg, .png or .webp "
                           f"files, one per line, the first a front view. Leave empty otherwise.",
                   "default": "\n".join(was.get("photos") or []),
                   "placeholder": "C:\\photos\\front.jpg"})
    if chars:
        qs.append({"id": "consent", "type": "multiple", "prompt": "Consent", "allowNote": False,
                   "help": "Needed only when a character is drawn from image files of a real person.",
                   "options": [{"value": "yes", "label": "Every real person in these image files agreed to be drawn "
                                                         "in this video", "selected": pconsent is True}]})
    return qs


def cast_from_answers(chars: list[dict], val) -> tuple[dict, bool, list[str]]:
    """(cast, consent, problems) from the answers to cast_questions; val(question id) -> the answer's value."""
    errs, cast = [], {}
    for c in chars:
        mode = val(qid("cast", c["id"]))
        raw = val(qid("photos", c["id"])) or ""
        paths = [ln.strip().strip('"').strip("'") for ln in str(raw).splitlines() if ln.strip()]
        if mode == "photos":
            cast[c["id"]] = {"name": c["name"], "look": c["look"], "mode": "photos",
                             "photos": [str(Path(p).expanduser()) for p in paths]}   # never resolved against a cwd
        elif mode in ("description", "none"):
            if paths:
                errs.append(f"{c['name']}: image file paths were given, but "
                            f"{'a sheet from the description' if mode == 'description' else 'no character sheet'} "
                            f"is chosen")
            cast[c["id"]] = {"name": c["name"], "look": c["look"], "mode": mode, "photos": []}
        else:
            errs.append(f"{c['name']}: choose a sheet from the description, a sheet from image files, or no sheet")
    consent = val("consent")
    return cast, isinstance(consent, list) and "yes" in consent, errs


def problems(run: Path, doc: dict, narration: dict | None = None, check_files: bool = False) -> list[str]:
    """Every reason `doc` is not a complete, valid setup for `run` (empty = valid). narration: the run's narration.json
    (read from the run when None and present), whose cast the setup must match. check_files: the photos must exist
    (from_answers); a stored setup is checked for its shape only."""
    out = []
    if doc.get("version") != SETUP_VERSION:
        out.append(f"version must be {SETUP_VERSION}")
    try:
        ims = image_models()["models"]
        if doc.get("image_model") not in ims:
            out.append(f"image_model {doc.get('image_model')!r} is not one of {', '.join(ims)}")
    except SetupError as e:
        out.append(str(e))
    sty = styles()
    if doc.get("style") not in sty:
        out.append(f"style {doc.get('style')!r} is not one of {', '.join(sty) or '(no complete style folder)'}")
    try:
        pals = palettes()
        if doc.get("palette") not in pals:
            out.append(f"palette {doc.get('palette')!r} is not one of {', '.join(pals)}")
    except SetupError as e:
        out.append(str(e))
    cast = doc.get("cast")
    out += cast_problems(cast, doc.get("consent"), check_files)
    if not isinstance(cast, dict):
        cast = {}
    cap = (doc.get("budget") or {}).get("cap")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not math.isfinite(cap) or cap <= 0:
        out.append(f"budget.cap must be a positive, finite number of credits, not {cap!r}")
    if narration is None and (run / "narration.json").is_file():
        narration = _read(run / "narration.json", "narration.json")
    if narration is not None:
        try:
            chars = narration_cast(narration)
        except SetupError as e:
            out.append(str(e))
        else:
            if doc.get("cast_fingerprint") != cast_fingerprint(chars) or set(cast) != {c["id"] for c in chars}:
                out.append("the characters in narration.json changed since the setup page was answered "
                           f"(now: {', '.join(c['id'] for c in chars) or 'none'}; setup: {', '.join(cast) or 'none'})")
    return out


def load(run: Path) -> dict:
    """The run's setup.json, checked; raises SetupError naming every problem."""
    p = path(run)
    if not p.is_file():
        raise SetupError(f"this run has no setup yet ({p} is missing): the setup page (Gate 1) plays the narration, "
                         f"shows the character sheets and asks the credit budget. Follow `scribe.py status --run "
                         f"{run.as_posix()}`; no scene is paid for before the setup page is answered")
    doc = _read(p, "setup.json")
    errs = problems(run, doc) + provenance(run, doc)
    if errs:
        raise SetupError(f"{p} is not a complete setup: " + "; ".join(errs) + ". Answer the setup page again "
                         f"(`scribe.py status --run {run.as_posix()}` builds the next Gate 1 round)")
    return doc


def _core(doc: dict) -> dict:
    """The setup without its budget (the budget may be raised later with `hf.py cap`, with the user's answer)."""
    return {k: v for k, v in doc.items() if k != "budget"}


def provenance(run: Path, doc: dict) -> list[str]:
    """setup.json must be exactly what a Gate 0 round that approved the narration wrote: that round's changes.json is
    approved and holds the same setup (budget aside). A file written by hand or copied from another run is refused."""
    r = doc.get("round")
    ch_p = run / "gates" / f"gate0-r{r}" / "changes.json"
    if not isinstance(r, int) or not ch_p.is_file():
        return [f"it does not come from a Gate 0 round of this run (no {ch_p.as_posix()})"]
    try:
        ch = json.loads(ch_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{ch_p} cannot be read: {e}"]
    if ch.get("approved") is not True or not isinstance(ch.get("setup"), dict):
        return [f"Gate 0 round {r} did not approve the narration with a setup"]
    if _core(ch["setup"]) != _core(doc):
        return [f"it differs from what Gate 0 round {r} recorded (setup.json is written only by gate0.py apply)"]
    return []


def require(run: Path) -> dict | None:
    """The full setup (start page + setup page) when setup is required, else None: every picture job needs it."""
    if not required():
        return None
    load_start(run)
    return load(run)


def require_start(run: Path) -> dict | None:
    """The start page's answers when setup is required, else None: narration takes need only these."""
    return load_start(run) if required() else None


CHOICE_KEYS = ("image_model", "style", "palette", "cast", "consent")


def choices(run: Path) -> dict:
    """The run's picture choices {"image_model", "style", "palette", "cast", "consent"}. The start record holds all of
    them (Gate 0 asked them; setup.json only copies them), so they come from it, before and after Gate 1, and the
    character sheets can be drawn before Gate 1. Raises SetupError naming what is missing."""
    st = load_start(run)
    return {k: st[k] for k in CHOICE_KEYS}


def style(run: Path) -> str:
    return choices(run)["style"]


def image_model(run: Path) -> dict:
    """{"id", "label", "params", "credits"} of the picture model chosen for the run (choices)."""
    mid = choices(run)["image_model"]
    m = image_models()["models"][mid]
    return {"id": mid, "label": m["label"], "params": dict(m["params"]), "credits": m["credits"]}


def palette_file(run: Path) -> Path:
    return palettes()[choices(run)["palette"]]["path"]


def require_pictures(run: Path, refs: list[str]) -> None:
    """A paid picture job needs the full setup (setup.json), except character sheets: Gate 0 chose their model, style,
    colours and cast, so they are drawn before Gate 1 shows them."""
    if not required():
        return
    load_start(run)
    if path(run).is_file() or not all(r.startswith("sheet/") for r in refs):
        load(run)
    else:
        choices(run)


def voice(run: Path) -> dict:
    """{"model", "voice_id", "voice_type", "id", "label"} of the run's narrator (chosen on the start page)."""
    cat = voices()
    v = cat["voices"][load_start(run)["voice"]]
    return {"model": cat["model"], "voice_id": v["voice_id"], "voice_type": v["voice_type"], "id": v["id"],
            "label": v["label"]}


def cap(run: Path) -> float:
    """The run's credit budget: the setup page's (Gate 1) once it exists, else the start page's (Gate 0)."""
    if path(run).is_file():
        return float(load(run)["budget"]["cap"])
    return float(load_start(run)["budget"]["cap"])


def set_cap(run: Path, value: float) -> dict:
    """Raise (or lower) the budget, only with the user's answer: in setup.json once it exists, else in start.json."""
    if not math.isfinite(value) or value <= 0:
        raise SetupError("the cap must be a positive, finite number of credits")
    if path(run).is_file():
        doc = load(run)
        doc["budget"]["cap"] = float(value)
        doc["budget"]["raised"] = True
        write(run, doc)
        return doc
    doc = load_start(run)
    doc["budget"] = {**doc["budget"], "cap": float(value), "raised": True}
    write_json_atomic(start_path(run), doc)
    return doc


def sheet_ids(run: Path) -> list[str]:
    """The characters that get a character sheet (start cast mode description or photos), in the start's order.
    A character with mode "none" has no sheet: no sheet job, no Gate 1 sheet question, no reference on its scenes."""
    return [cid for cid, c in choices(run)["cast"].items() if c.get("mode") != "none"]


def photos(run: Path, cid: str) -> list[Path]:
    """The photos chosen for character `cid` on the start page (choices; [] = no photos)."""
    cast = choices(run)["cast"]
    c = cast.get(cid)
    if c is None:
        raise SetupError(f"character {cid!r} was not asked about (the run's cast: {', '.join(cast) or 'none'}); the "
                         f"script may not add characters")
    return [Path(p) for p in c.get("photos") or []]


def write(run: Path, doc: dict) -> None:
    p = path(run)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def previous(run: Path) -> dict:
    """The answers to show preselected on the next page: setup.json when present, else {} (the page then preselects
    each catalog's default where it has one and leaves the rest unanswered)."""
    p = path(run)
    if p.is_file():
        try:
            return _read(p, "setup.json")
        except SetupError:
            return {}
    return {}


# ------------------------------------------------------------------ the setup questions (gate0.py)

def qid(prefix: str, cid: str) -> str:
    return f"{prefix}-" + re.sub(r"[^a-z0-9]+", "-", cid.lower()).strip("-")


def questions(run: Path, narration: dict, prev: dict | None = None) -> tuple[list[dict], dict]:
    """The setup questions for Gate 1 (only the budget: every other choice was made on the start page, and gate0.py
    shows it) and the meta gate0.py stores to map the answers back. The previous budget (setup.json or the last round's)
    is preselected; on the first round, the start page's budget raised to the expected figure when lower. Raises
    SetupError without a valid start record."""
    prev = prev or {}
    chars = narration_cast(narration)
    st = load_start(run)
    model = image_models()["models"][st["image_model"]]
    sheeted = [c for c in chars if (st["cast"].get(c["id"]) or {}).get("mode") != "none"]
    est = estimate({**narration, "characters": sheeted}, model["credits"])
    pcap = (prev.get("budget") or {}).get("cap")
    if pcap is None:   # first setup round: the start page's budget, raised to the expected figure when lower
        pcap = max(float(st["budget"]["cap"]), est["default_cap"])
    qs = [{"id": "budget", "type": "text", "prompt": "Credit budget for this run",
           "body": (f"Higgsfield credits this run is likely to use with the image model chosen on Gate 0 (lowest: "
                    f"every picture and take right first time; ceiling: every limit reached once):\n\n"
                    f"- **{model['label']}** ({est['image_credits']:g} credits a picture): lowest {est['lowest']}, "
                    f"expected {est['expected']}, ceiling {est['ceiling']}\n\n"
                    f"Counted from {est['scenes']} scenes plus the title card, {est['characters']} character sheet(s) "
                    f"(as chosen on Gate 0) and {est['sentences']} narration sentences (about "
                    f"{est['audio_credits']} credits for the whole narration). The run stops and asks before any job "
                    f"that would pass the budget."),
           "default": str(int(pcap) if isinstance(pcap, (int, float)) and float(pcap).is_integer() else pcap),
           "placeholder": "credits, for example 60"}]
    return qs, {"estimate": est, "cast_fingerprint": cast_fingerprint(chars)}


def from_answers(run: Path, responses: dict, meta: dict, narration: dict, round_: int, fp: str) -> tuple[dict, list[str]]:
    """(setup doc, problems) from a Gate 1 page's answers: the budget, plus the image model, style, palette, cast and
    consent copied from the start record. problems are the user's to fix (ask them, never guess). Raises SetupError
    without a valid start record."""
    def val(q: str):
        r = responses.get(q)
        return None if r is None else r.get("value")

    errs = []
    chars = narration_cast(narration)
    st = load_start(run)
    cast = {cid: dict(c) for cid, c in st["cast"].items()}
    budget_raw = str(val("budget") or "").strip().replace(",", "")
    try:
        cap_ = float(budget_raw)
    except ValueError:
        cap_ = None
        errs.append(f"the credit budget must be a number, not {budget_raw!r}")
    doc = {"version": SETUP_VERSION, "round": round_, "narration_fingerprint": fp, "image_model": st["image_model"],
           "style": st["style"], "palette": st["palette"], "cast": cast,
           "cast_fingerprint": meta.get("cast_fingerprint"), "consent": st["consent"],
           "budget": {"cap": cap_, "estimate": None}}
    # the stored estimate counts only the sheets asked for, at the model's price
    sheeted = {**narration, "characters": [c for c in chars if (cast.get(c["id"]) or {}).get("mode") != "none"]}
    doc["budget"]["estimate"] = estimate(sheeted, image_models()["models"][st["image_model"]]["credits"])
    errs += [e for e in problems(run, doc, narration) if not e.startswith("budget.cap") or cap_ is not None]
    return doc, errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--run", required=True)
    sub.add_parser("catalog")
    cc = sub.add_parser("cast-check", help="check RUN/story_cast.json (writer pass 0) and stamp it with the story")
    cc.add_argument("--run", required=True)
    cc.add_argument("--story", default=None, help="the story file (default: the run folder's only .md / .txt)")
    a = ap.parse_args(argv)
    if a.cmd == "cast-check":
        run = Path(a.run).resolve()
        story = Path(a.story).resolve() if a.story else folder_story(run)
        if story is None or not story.is_file() or story.parent.resolve() != run:
            print(f"FAILED cast-check: no story file in {run} (give --story FILE, a .md or .txt in that folder)",
                  file=sys.stderr)
            return 1
        try:
            chars = cast_check(run, story)
        except SetupError as e:
            print(f"FAILED cast-check: {e}", file=sys.stderr)
            return 1
        print("CAST_JSON " + json.dumps({"ok": True, "story": str(story), "characters": chars}, ensure_ascii=False))
        return 0
    if a.cmd == "catalog":
        try:
            out = {"required": required(), "styles": {k: str(v["sample"]) for k, v in styles().items()},
                   "palettes": {k: {"label": v["label"], "roles": v["roles"]} for k, v in palettes().items()},
                   "voices": list(voices()["voices"])}
        except SetupError as e:
            print(f"FAILED catalog: {e}", file=sys.stderr)
            return 1
        print("CATALOG_JSON " + json.dumps(out))
        return 0
    run = Path(a.run).resolve()
    try:
        doc = load(run)
    except SetupError as e:
        print("SETUP_JSON " + json.dumps({"ok": False, "required": required(), "problems": [str(e)]}))
        return 1
    print("SETUP_JSON " + json.dumps({"ok": True, "required": required(), "problems": [], "setup": doc},
                                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
