#!/usr/bin/env python
r"""characters.py - character sheets for Scribeboard runs (contract v1.1).

Every character declared in RUN/script.json (with a setup: every one the setup gave a sheet; sheets are optional)
gets RUN/characters/<id>_sheet.png: one text-free,
multi-view sheet in the run's style, rendered by ChatGPT through the /render skill's Codex driver (resolved like
render_scenes.py's: settings render.drivers.chatgpt, else render.engine_registry).
A character with a `reference` photo is redrawn from it (edit mode); otherwise it is drawn from its
description. The sheets are then attached to every scene render that shows the character.

  build  --run RUN [--style rsa_animate] [--only ID[,ID]] [--force]  render missing sheets (images.engine 'chatgpt')
  status --run RUN                                                   which sheets exist (and, with Higgsfield, are
                                                                     made through it and passed their sheet review)

images.engine 'higgsfield' (scripts/hf.py handoff; state in RUN/characters/sheets.json, keyed by sheet file name):
  upload    --run RUN --id ID                 print the media_upload arguments for the character's reference photo(s)
  upload    --run RUN --id ID --response FILE PUT each photo to its presigned URL (curl) from the saved media_upload
                                              result, record the media ids, print the media_confirm arguments
  prep      --run RUN --id ID [--style S] [--force] [--fix NOTE]
                                              reserve the sheet job and print its generate_image arguments (the
                                              confirmed photo media ids as references)
  submitted --run RUN --id ID --job ID --quoted N   (or --unknown NOTE)
  ingest    --run RUN --id ID --url URL           (or --failed REASON): download to RUN/characters/<id>_sheet.png
  restore   --run RUN --id ID --job JOB --url URL --reason TEXT [--model ID]
                                              make an earlier paid attempt current again (the user's choice): the job
                                              must be this character's paid sheet job in RUN/credits.json; downloaded
                                              again; then a fresh review like any new sheet (no credits)
  review    --run RUN --id ID --reviewer NAME (--pass | --fail NOTE)
                                              a fresh review agent (not the one that prepared the sheet) records
                                              its check: exactly six separate figures (any clear arrangement; the
                                              prompt asks for two rows of three), no lettering except what the
                                              character wears, matches the photo/description, the run's colours only.
                                              A fail needs `prep --force --fix`

Sheets carry no text, no boxes, no story action (asset-sheet rule): a sheet is consumed
by an image model, and lettering on an attached reference bleeds into the images it is attached to.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import hf                # scripts/hf.py: the Higgsfield handoff (images.engine 'higgsfield')
import oslib             # scripts/oslib.py: curl and OS differences
import palette           # scripts/palette.py: the run's colours fill the style's prompt block
import runsetup          # scripts/runsetup.py: the setup page's style and photos, required with Higgsfield
import settings as cfg   # scripts/settings.py: python, styles_dir
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

PY = str(cfg.python())
STYLES = cfg.path("styles_dir")

VIEWS = ("Draw the same character exactly six times on one sheet, no more and no fewer, in two rows of three, evenly "
         "spaced with generous white space between them: top row full body standing - front view, three-quarter view, "
         "side view; bottom row head and shoulders - smiling, surprised, thoughtful (one figure each). Identical face, "
         "hair, facial hair, clothing and proportions in every view, and the same accessories (only those the "
         "character has).")
RULES = ("This is a wordless reference sheet for an illustrator: NO text of any kind anywhere on it - no view names, no "
         "expression names, no character name, no title, no labels, no numbers, no captions, no letters - and no "
         "boxes, frames, panels or dividing lines, no background scenery and no props except what the character "
         "always wears. Pure flat white background.")
# Scene-only sentences of the style block: lettering and labels (a sheet has none; they made the model label each
# view in a Higgsfield test) and the arrangement of a scene's groups.
SCENE_ONLY = (re.compile(r",? and short hand-lettered [^.,]*labels"),
              re.compile(r"Lettering is [^.]*\.\s*"),
              re.compile(r"The groups of drawings are arranged.*?(?:one flow|another)\.\s*"))


class Fail(Exception):
    pass


def driver() -> Path:
    """The ChatGPT driver, from render_scenes.engine (never a second resolver)."""
    spec = importlib.util.spec_from_file_location("render_scenes", Path(__file__).with_name("render_scenes.py"))
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    try:
        return rs.engine("chatgpt")["driver"]
    except rs.Fail as e:
        raise Fail(str(e)) from e


def style_block(style: str, run: Path) -> str:
    try:
        block = palette.prompt_block(STYLES, style, "image-prompt", run)   # the run's palette
    except palette.PaletteError as e:
        raise Fail(str(e)) from e
    for pattern in SCENE_ONLY:   # scene-only sentences do not apply to a sheet
        block = pattern.sub("", block)
    return block


def sheet_prompt(ch: dict, style: str, from_photo: bool, run: Path) -> str:
    who = ("Image 1 is a photo reference of a real person. Redraw THIS person as a cartoon character, keeping every "
           "identifying feature the photo shows (face shape, hair, facial hair, clothing, and glasses or headwear only "
           "if worn on the head in the photo). Add nothing the photo does not show. " if from_photo else "")
    return (f"{who}A character design sheet for {ch['name']}: {ch['desc']}. {VIEWS} {RULES} {style_block(style, run)}")


def run_style(run: Path, given: str) -> str:
    """The style a sheet is drawn in: with setup required, the setup page's choice (a different --style is refused)."""
    if not runsetup.required():
        return given
    try:
        chosen = runsetup.style(run)
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    if given and given != chosen:
        raise Fail(f"--style {given!r} is not the run's style: it chose {chosen!r} (RUN/setup.json or start.json)")
    return chosen


def flatten_white(p: Path) -> None:
    """ChatGPT sometimes returns an RGBA cut-out; a sheet must be on the pure white background its prompt asks for.
    Always re-saved as a real PNG (another model may return JPEG or WebP bytes under the .png name)."""
    from PIL import Image
    with Image.open(p) as src:
        im = src.convert("RGBA") if src.mode in ("RGBA", "LA", "P") else src.convert("RGB")
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    im.save(p, format="PNG")


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    if hf.on("images"):
        raise Fail("build: images.engine is 'higgsfield'; make each sheet with `prep`, submit it through the "
                   "Higgsfield MCP, then `submitted`, `ingest` and a fresh agent's `review`")
    doc = json.loads((run / "script.json").read_text(encoding="utf-8"))
    chars = doc.get("characters") or []
    if not chars:
        raise Fail("script.json declares no characters")
    for cid in (a.only.split(",") if a.only else []):
        no_sheet_refused(run, cid)
    if runsetup.required():   # only the characters the setup gave a sheet (sheets are optional)
        try:
            want = set(runsetup.sheet_ids(run))
        except runsetup.SetupError as e:
            raise Fail(str(e)) from e
        chars = [c for c in chars if c["id"] in want]
    out = run / "characters"
    out.mkdir(exist_ok=True)
    done = []
    for ch in chars:
        if a.only and ch["id"] not in a.only.split(","):
            continue
        dest = out / f"{ch['id']}_sheet.png"
        if dest.is_file() and not a.force:
            continue
        photos = photo_paths(ch, run)   # with setup: the setup page's photos (never silently dropped)
        if len(photos) > 1:
            raise Fail(f"character {ch['id']}: `build` (ChatGPT) draws from one photo; {len(photos)} were given")
        ref = str(photos[0]) if photos else None
        prompt = sheet_prompt(ch, run_style(run, a.style), bool(ref), run)
        (out / f"{ch['id']}_sheet.prompt.txt").write_text(prompt, encoding="utf-8")
        args = (["edit", "--image", str(Path(ref).resolve()), "--prompt", prompt, "--raw"] if ref
                else ["render", "--prompt", prompt, "--aspect", "16:9"])
        p = subprocess.run([PY, str(driver()), *args, "--dest", str(out), "--name", dest.name],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, creationflags=NO_WINDOW)
        m = re.search(r"RESULT_JSON (\[.*\])", p.stdout)
        if p.returncode != 0 or not m or not dest.is_file():
            raise Fail(f"character {ch['id']}: ChatGPT render failed: {(p.stdout + p.stderr).strip()[-600:]}")
        flatten_white(dest)
        done.append(str(dest))
        print(f"[characters] {ch['id']}: {dest}", flush=True)
    print("CHARACTERS_JSON " + json.dumps({"built": done, "sheets": {c["id"]: str(out / f"{c['id']}_sheet.png") for c in chars}}))
    return 0


def sheets_path(run: Path) -> Path:
    return run / "characters" / "sheets.json"


def load_sheets(run: Path) -> dict:
    p = sheets_path(run)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_sheets(run: Path, doc: dict) -> None:
    p = sheets_path(run)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def sheet_ready(run: Path, cid: str) -> bool:
    """The sheet exists and, with Higgsfield, was made through it (it has a job id later scenes reference) and
    passed its sheet review."""
    f = run / "characters" / f"{cid}_sheet.png"
    if not f.is_file():
        return False
    if not hf.on("images"):
        return True
    rec = load_sheets(run).get(f.name) or {}
    if runsetup.required() and rec.get("cast") != setup_cast(run, cid, strict=False):
        return False    # drawn for another setup answer (description vs photos, other photos): draw it again
    return rec.get("state") == "ingested" and bool(rec.get("job_id")) and (
        (rec.get("review") or {}).get("pass") is True or bool(rec.get("flagged")))


def setup_cast(run: Path, cid: str, strict: bool = True) -> dict | None:
    """What a sheet is drawn for, from the setup page: the character's answer (mode, photos and the content hash of
    each, look) plus the run's style and colours. A sheet drawn for anything else is drawn again. None without setup;
    strict=False returns None instead of raising when the setup is missing or invalid (a status read)."""
    if not runsetup.required():
        return None
    try:
        su = runsetup.choices(run)   # the start page's (sheets are drawn before Gate 1)
    except runsetup.SetupError:
        if strict:
            raise
        return None
    c = su["cast"].get(cid) or {}
    photos = [str(p) for p in c.get("photos") or []]
    try:
        ch = character(run, cid)            # the sheet prompt uses script.json's name and desc
    except (Fail, OSError, ValueError):
        if strict:
            raise
        return None
    return {"mode": c.get("mode"), "photos": photos, "photo_sha256": [file_sha(Path(p)) for p in photos],
            "look": c.get("look"), "name": ch.get("name"), "desc": ch.get("desc"), "style": su.get("style"),
            "palette": su.get("palette"), "image_model": su.get("image_model")}


def file_sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def sheet_next(run: Path, cid: str) -> str:
    f = run / "characters" / f"{cid}_sheet.png"
    rec = load_sheets(run).get(f.name) or {}
    st = rec.get("state")
    if st == "requested":
        note = hf.pending_note(run, f"sheet/{cid}/a{rec.get('attempt')}")
        return note or f"submit the sheet request of {cid}"
    if st == "submitted":
        return f"wait for job {rec.get('job_id')} and ingest the sheet of {cid}"
    if st == "unknown":
        return f"reconcile the sheet submission of {cid} (check the Higgsfield account)"
    if st == "ingested" and runsetup.required() and rec.get("cast") != setup_cast(run, cid, strict=False):
        st = None       # the setup answer for this character changed since the sheet was drawn: start over below
    if st == "ingested":
        rv = rec.get("review")
        if not rv:
            return f"review the sheet of {cid} (fresh review agent)"
        if rv.get("pass") is False and rec.get("flagged"):
            return "done"   # flagged after MAX_SHEET_CHAIN failed reviews: Gate 1 shows it for the user
        if rv.get("pass") is False:
            return f"prep the sheet of {cid} again with the review fix"
        return "done"
    photos = photo_paths(character(run, cid), run)
    if photos and not photos_uploaded(load_sheets(run), cid, photos):
        return f"upload the photos of {cid}"
    return f"prep the sheet of {cid}"


def photos_uploaded(doc: dict, cid: str, photos: list[Path]) -> bool:
    """The uploaded media ids belong to exactly these photo files (same paths and contents, same order)."""
    return (len(doc.get("_photos", {}).get(cid, [])) == len(photos)
            and doc.get("_photos_src", {}).get(cid) == [[str(p), file_sha(p)] for p in photos])


def character(run: Path, cid: str) -> dict:
    """The character as script.json declares it; with setup required always as the start page asked about it,
    {"id", "name", "desc": its look} (its sheet is drawn before script.json exists and keyed to these, so a pass 2
    wording never makes an approved sheet stale; scribe.py also holds pass 2 to them)."""
    if runsetup.required():
        try:
            c = runsetup.choices(run)["cast"].get(cid)
        except runsetup.SetupError as e:
            raise Fail(str(e)) from e
        if c is None:
            raise Fail(f"the start page asked about no character {cid!r}")
        return {"id": cid, "name": c["name"], "desc": c["look"]}
    doc = json.loads((run / "script.json").read_text(encoding="utf-8"))
    for ch in doc.get("characters") or []:
        if ch["id"] == cid:
            return ch
    raise Fail(f"script.json declares no character {cid!r}")


def declared_ids(run: Path) -> list[str]:
    """The run's characters: script.json's, else (sheets before Gate 1) narration.json's."""
    for name in ("script.json", "narration.json"):
        p = run / name
        if p.is_file():
            return [c["id"] for c in json.loads(p.read_text(encoding="utf-8")).get("characters") or []]
    raise Fail(f"{run} has neither script.json nor narration.json")


def photo_paths(ch: dict, run: Path | None = None) -> list[Path]:
    """The character's photos: with setup required, exactly those chosen on the setup page (script.json `reference` is
    refused there: photos and their consent belong to the setup); otherwise script.json `reference`."""
    if runsetup.required():
        if ch.get("reference"):
            raise Fail(f"character {ch['id']}: script.json may not set `reference` in this skill; photos come from the "
                       f"setup page (RUN/setup.json cast). Remove it from script.json")
        try:
            paths = runsetup.photos(run, ch["id"])
        except runsetup.SetupError as e:
            raise Fail(str(e)) from e
    else:
        refs = ch.get("reference") or []
        refs = [refs] if isinstance(refs, str) else refs
        paths = [Path(r) for r in refs]
    for p in paths:
        if not p.is_file():
            raise Fail(f"character {ch['id']}: reference {p} not found")
    return paths


MAX_SHEET_CHAIN = 3   # automatic sheet attempts in a row after failed reviews; then Gate 1 shows the newest, flagged
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def no_sheet_refused(run: Path, cid: str) -> None:
    """With setup required: refuse a sheet job for a character the setup page gave no sheet (mode "none")."""
    if not runsetup.required():
        return
    try:
        want = runsetup.sheet_ids(run)
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    if cid not in want:
        raise Fail(f"character {cid} has no character sheet (the setup page chose \"no character sheet\"); nothing "
                   f"to draw or upload. Characters with a sheet: {', '.join(want) or 'none'}")


def cmd_upload(a) -> int:
    run = Path(a.run).resolve()
    no_sheet_refused(run, a.id)
    ch = character(run, a.id)
    photos = photo_paths(ch, run)
    if not photos:
        raise Fail(f"character {a.id} has no reference photo; its sheet is drawn from the description")
    for p in photos:
        if p.suffix.lower() not in CONTENT_TYPES:
            raise Fail(f"{p}: only {', '.join(CONTENT_TYPES)} photos can be uploaded")
    if not a.response:
        print("REQUEST_JSON " + json.dumps({"kind": "upload", "ref": f"photos/{a.id}", "tool": "media_upload",
                                            "arguments": {"files": [{"filename": p.name,
                                                                     "content_type": CONTENT_TYPES[p.suffix.lower()]}
                                                                    for p in photos]},
                                            "next": "save the tool's raw JSON result to a file and run `characters.py "
                                                    f"upload --run RUN --id {a.id} --response FILE`"}))
        return 0
    resp = json.loads(Path(a.response).read_text(encoding="utf-8"))
    ups = resp.get("uploads") or []
    curl = oslib.which("curl")
    if curl is None:
        raise Fail("upload: curl not found")
    if len(ups) != len(photos):
        raise Fail(f"upload: the media_upload result has {len(ups)} upload(s) for {len(photos)} photo(s); request it again")
    ids = []
    for src, u in zip(photos, ups):    # media_upload answers in the order the files were listed
        p = subprocess.run([str(curl), "-s", "-o", "-", "-w", "%{http_code}", "-X", "PUT", "-H",
                            f"Content-Type: {u['content_type']}", "--data-binary", f"@{src}", u["upload_url"]],
                           capture_output=True, text=True, creationflags=NO_WINDOW, timeout=600)
        if not p.stdout.strip().endswith("200"):
            raise Fail(f"upload: PUT of {src} returned {p.stdout.strip()[-200:]} {p.stderr.strip()[-200:]}")
        ids.append((u["media_id"], src.name))
    doc = load_sheets(run)
    doc.setdefault("_photos", {})[a.id] = [i for i, _ in ids]
    doc.setdefault("_photos_src", {})[a.id] = [[str(p), file_sha(p)] for p in photos]
    save_sheets(run, doc)
    print("REQUEST_JSON " + json.dumps({"kind": "confirm", "ref": f"photos/{a.id}", "tool": "media_confirm",
                                        "arguments": {"media_ids": [i for i, _ in ids], "type": "image"},
                                        "photos": {i: n for i, n in ids}}))
    return 0


def cmd_prep(a) -> int:
    run = Path(a.run).resolve()
    if not hf.on("images"):
        raise Fail(f"prep: images.engine is {cfg.get('images.engine')!r}, not 'higgsfield'; use `build`")
    no_sheet_refused(run, a.id)
    ch = character(run, a.id)
    out = run / "characters"
    out.mkdir(exist_ok=True)
    fname = f"{a.id}_sheet.png"
    doc = load_sheets(run)
    rec = doc.get(fname) or {}
    if rec.get("state") in ("requested", "submitted", "unknown"):
        raise Fail(f"prep: the sheet of {a.id} is '{rec['state']}': {sheet_next(run, a.id)}")
    if (out / fname).is_file() and not a.force and sheet_ready(run, a.id):
        raise Fail(f"prep: the sheet of {a.id} is done; pass --force to redraw it")
    n = rec.get("attempt", 0) + 1
    try:   # refuses first when the run's choices are unanswered (the start page)
        runsetup.require_pictures(run, [f"sheet/{a.id}/a{n}"])
    except runsetup.SetupError as e:
        raise Fail(f"no paid job before the run's choices are answered: {e}") from e
    style = run_style(run, a.style)
    photos = photo_paths(ch, run)
    media = doc.get("_photos", {}).get(a.id, []) if photos else []
    if photos and not photos_uploaded(doc, a.id, photos):
        raise Fail(f"prep: upload the {len(photos)} photo(s) of {a.id} chosen on the setup page first "
                   f"(`characters.py upload --run RUN --id {a.id}`)")
    prompt = sheet_prompt(ch, style, bool(photos), run)   # every other check first: a refusal reserves nothing
    if photos:
        prompt = prompt.replace("Image 1 is a photo reference", "The reference image" + ("s are photos" if len(photos) > 1
                                                                                         else " is a photo") + " reference", 1)
    review_failed = (rec.get("review") or {}).get("pass") is False
    fix = a.fix or ((rec.get("review") or {}).get("note") if review_failed else "")
    # automatic retries after a failed sheet review are capped; a user's edit (--fix, from Gate 1) starts a new chain
    chain = rec.get("chain", 1) + 1 if (review_failed and not a.fix) else 1
    if chain > MAX_SHEET_CHAIN:
        raise Fail(f"prep: {MAX_SHEET_CHAIN} sheets of {a.id} in a row failed their review; the newest one goes to "
                   f"Gate 1 flagged, for the user to approve or edit (characters.py review flags it)")
    if fix:
        prompt = f"{prompt} Correction from the previous sheet's review: {fix.strip()}"
    cast_rec = setup_cast(run, a.id)
    if a.model and not a.model_reason.strip():
        raise Fail("prep: --model draws this sheet with another model than the run's; give --model-reason with the "
                   "user's own words asking for it (a Gate 1 note); never choose another model yourself")
    try:
        req = hf.image_request(prompt, run, [{"value": m, "role": "image_references"} for m in media], "16:9",
                               model_id=a.model or None)
        budget = hf.ledger_check(run, "image", [f"sheet/{a.id}/a{n}"],   # last: reserves the sheet's price
                                 req["arguments"]["params"].get("model"))
    except hf.HFError as e:
        raise Fail(str(e))
    (out / f"{a.id}_sheet.prompt.txt").write_text(prompt, encoding="utf-8")
    doc[fname] = {"id": a.id, "state": "requested", "attempt": n, "prompt": prompt, "fix": fix, "photos": media,
                  "cast": cast_rec, "model": req["arguments"]["params"]["model"], "chain": chain}
    if a.model:   # the user asked for this sheet to be drawn with another model (scenes keep the run's model)
        doc[fname]["model_override"] = {"model": a.model, "reason": a.model_reason.strip()}
    save_sheets(run, doc)
    hf.emit_request("image", f"sheet/{a.id}/a{n}", req, {"character": a.id, "credits": budget}, run=run)
    return 0


def cmd_submitted(a) -> int:
    run = Path(a.run).resolve()
    fname = f"{a.id}_sheet.png"
    doc = load_sheets(run)
    rec = doc.get(fname) or {}
    if rec.get("state") not in ("requested", "unknown"):
        raise Fail(f"submitted: the sheet of {a.id} is '{rec.get('state')}', expected requested or unknown")
    ref = f"sheet/{a.id}/a{rec['attempt']}"
    if a.unknown:
        rec.update(state="unknown", unknown=a.unknown)
        hf.ledger_record(run, "image", ref, None, a.quoted, "unknown")
    else:
        if not a.job:
            raise Fail("submitted: give --job ID (or --unknown NOTE)")
        rec.update(state="submitted", job_id=a.job, quoted=a.quoted)
        hf.ledger_record(run, "image", ref, a.job, a.quoted, "submitted")
    doc[fname] = rec
    save_sheets(run, doc)
    print("SUBMITTED_JSON " + json.dumps({"character": a.id, "state": rec["state"], "job_id": rec.get("job_id")}))
    return 0


def cmd_ingest(a) -> int:
    run = Path(a.run).resolve()
    fname = f"{a.id}_sheet.png"
    doc = load_sheets(run)
    rec = doc.get(fname) or {}
    if rec.get("state") not in (("requested", "submitted", "unknown") if a.failed else ("submitted",)):
        raise Fail(f"ingest: the sheet of {a.id} is '{rec.get('state')}', expected submitted")
    if a.failed:
        try:   # refunds a submitted sheet; a never-submitted one only releases its reservation (sheets.lock held)
            hf.ledger_close(run, f"sheet/{a.id}/a{rec['attempt']}", rec["state"])
        except hf.HFError as e:
            raise Fail(f"ingest: {e}")
        rec.update(state="failed", error=a.failed)
    else:
        dest = run / "characters" / fname
        try:
            hf.download(a.url, dest, rec.get("job_id"))
        except hf.HFError as e:
            raise Fail(f"ingest: {e}; the job is paid, retry `ingest` (do not resubmit)")
        flatten_white(dest)
        rec.update(state="ingested", source_url=a.url, review=None)
    doc[fname] = rec
    save_sheets(run, doc)
    print("CHARACTERS_JSON " + json.dumps({"character": a.id, "state": rec["state"], "sheet": str(run / "characters" / fname),
                                           "next": sheet_next(run, a.id)}))
    return 0


def cmd_restore(a) -> int:
    """Make an earlier paid sheet attempt current again (the user preferred it): its job must be one of this
    character's sheet jobs in RUN/credits.json, submitted (paid); the picture is downloaded again from Higgsfield and
    goes through a fresh sheet review like any new sheet. No new job, no credits."""
    run = Path(a.run).resolve()
    no_sheet_refused(run, a.id)
    if not a.reason.strip():
        raise Fail("restore: give --reason with the user's own words choosing this earlier sheet")
    fname = f"{a.id}_sheet.png"
    doc = load_sheets(run)
    rec = doc.get(fname) or {}
    if rec.get("state") in ("requested", "submitted", "unknown"):
        raise Fail(f"restore: the sheet of {a.id} has an open job ('{rec['state']}'): {sheet_next(run, a.id)}")
    line = hf.ledger_job(run, a.job)
    prefix = f"sheet/{a.id}/a"
    if not line or not str(line.get("ref", "")).startswith(prefix) or line.get("state") != "submitted":
        raise Fail(f"restore: job {a.job} is not a paid sheet job of {a.id} in {hf.ledger_path(run)} (its line: "
                   f"{line!r})")
    attempt = int(line["ref"][len(prefix):])
    dest = run / "characters" / fname
    try:
        hf.download(a.url, dest, a.job)
    except hf.HFError as e:
        raise Fail(f"restore: {e}") from e
    flatten_white(dest)
    model = a.model or runsetup.image_model(run)["params"]["model"]
    import gate4   # the newest Gate 1 round that answered the sheets: this restore answers its edit
    g1 = gate4.gate1_cast_record(run)
    doc[fname] = {"id": a.id, "state": "ingested", "attempt": attempt, "job_id": a.job, "source_url": a.url,
                  "cast": setup_cast(run, a.id), "model": model, "review": None, "photos": rec.get("photos"),
                  "restored": {"reason": a.reason.strip(), "replaced_attempt": rec.get("attempt"),
                               "replaced_job": rec.get("job_id"), "after_gate1_round": g1[0] if g1 else None}}
    save_sheets(run, doc)
    print("CHARACTERS_JSON " + json.dumps({"character": a.id, "state": "ingested", "restored_attempt": attempt,
                                           "job_id": a.job, "sheet": str(dest), "next": sheet_next(run, a.id)}))
    return 0


def cmd_review(a) -> int:
    run = Path(a.run).resolve()
    fname = f"{a.id}_sheet.png"
    doc = load_sheets(run)
    rec = doc.get(fname) or {}
    if rec.get("state") != "ingested":
        raise Fail(f"review: the sheet of {a.id} is '{rec.get('state')}', not ingested")
    if not a.reviewer.strip():
        raise Fail("review: --reviewer must name the review agent")
    if a.fail is not None and not a.fail.strip():
        raise Fail("review: --fail needs a note saying what the next sheet must change")
    rec["review"] = {"by": a.reviewer.strip(), "pass": a.fail is None, "note": (a.fail or "").strip()}
    rec["flagged"] = a.fail is not None and rec.get("chain", 1) >= MAX_SHEET_CHAIN   # Gate 1 shows it, marked
    doc[fname] = rec
    save_sheets(run, doc)
    print("CHARACTERS_JSON " + json.dumps({"character": a.id, "review": rec["review"], "next": sheet_next(run, a.id)}))
    return 0


def cmd_status(a) -> int:
    run = Path(a.run).resolve()
    ids = declared_ids(run)
    if runsetup.required():   # only the characters the setup page gave a sheet (a sheet is optional)
        try:
            want = set(runsetup.sheet_ids(run))
        except runsetup.SetupError as e:
            raise Fail(str(e)) from e
        ids = [k for k in ids if k in want]
    rows = {k: sheet_ready(run, k) for k in ids}
    nxt = {k: ("done" if rows[k] else sheet_next(run, k) if hf.on("images") else "build") for k in ids}
    for k, v in rows.items():
        print(f"{k:<16} {'sheet ready' if v else nxt[k]}")
    print("CHARACTERS_JSON " + json.dumps({"ready": all(rows.values()), "sheets": rows, "next": nxt}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--run", required=True)
    b.add_argument("--style", default="rsa_animate")
    b.add_argument("--only", default="")
    b.add_argument("--force", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    u = sub.add_parser("upload")
    u.add_argument("--run", required=True)
    u.add_argument("--id", required=True)
    u.add_argument("--response", default="", help="file with media_upload's raw JSON result")
    p = sub.add_parser("prep")
    p.add_argument("--run", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--style", default="", help="must match the run's setup (RUN/setup.json); omit to use it")
    p.add_argument("--force", action="store_true")
    p.add_argument("--fix", default="")
    p.add_argument("--model", default="", help="draw this sheet with another catalog image model than the run's "
                                               "(only when the user asked for it; scenes keep the run's model)")
    p.add_argument("--model-reason", default="", help="the user's words asking for --model (recorded)")
    sb = sub.add_parser("submitted")
    sb.add_argument("--run", required=True)
    sb.add_argument("--id", required=True)
    sb.add_argument("--job", default="")
    sb.add_argument("--quoted", type=float, default=None)
    sb.add_argument("--unknown", default="")
    ig = sub.add_parser("ingest")
    ig.add_argument("--run", required=True)
    ig.add_argument("--id", required=True)
    ix = ig.add_mutually_exclusive_group(required=True)
    ix.add_argument("--url")
    ix.add_argument("--failed")
    rv = sub.add_parser("review")
    rv.add_argument("--run", required=True)
    rv.add_argument("--id", required=True)
    rv.add_argument("--reviewer", required=True)
    rx = rv.add_mutually_exclusive_group(required=True)
    rx.add_argument("--pass", dest="passed", action="store_true")
    rx.add_argument("--fail", default=None)
    rs_ = sub.add_parser("restore", help="make an earlier paid sheet attempt current again (the user chose it); it "
                                          "is downloaded again and reviewed afresh")
    rs_.add_argument("--run", required=True)
    rs_.add_argument("--id", required=True)
    rs_.add_argument("--job", required=True, help="the earlier attempt's Higgsfield job id (RUN/credits.json)")
    rs_.add_argument("--url", required=True, help="that job's result URL (jobs_wait / show_generation_by_ids)")
    rs_.add_argument("--reason", required=True, help="the user's words choosing it (recorded)")
    rs_.add_argument("--model", default="", help="the model id that drew it (default: the run's)")
    a = ap.parse_args(argv)
    fn = {"build": cmd_build, "status": cmd_status, "upload": cmd_upload, "prep": cmd_prep, "submitted": cmd_submitted,
          "ingest": cmd_ingest, "review": cmd_review, "restore": cmd_restore}
    try:
        if a.cmd in ("upload", "prep", "submitted", "ingest", "review", "restore"):
            # every read-change-write of characters/sheets.json runs under one cross-process lock, so two commands
            # for two characters at the same moment can never overwrite each other's record
            with hf.file_lock(Path(a.run).resolve() / "characters" / "sheets.lock", "character sheets"):
                return fn[a.cmd](a)
        return fn[a.cmd](a)
    except hf.HFError as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1
    except Fail as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
