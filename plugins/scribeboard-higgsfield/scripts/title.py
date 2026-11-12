#!/usr/bin/env python
r"""title.py - the hand-lettered title card: render, scored review loop, Gate 1 hand-off.

The title is one image in the scene style: the words and at most a small underline or flourish, on
white. It is rendered with render_scenes.py's ChatGPT path (blank canvas as Image 1, scene 1's selected
image as the style reference, no character sheets), framed and upscaled to 3840x2160 by render_scenes'
own finishing, reviewed with render_scenes' checklist scorer, and approved at Gate 1 (gate1.py). The
board packs it and draws it first.

  build        --run RUN [--text TEXT]
               create RUN/title/title.json (text from --text, else script.json `title_text`, else the stored text); a changed
               text, or a changed prompt (the style's title-colour block or palette.json), starts a new round.
               Needs scene 1's image selected (it is the style reference).
  status       --run RUN     status and NEXT action; TITLE_JSON (also when title.json does not exist yet;
               prompt_current false = the style colours changed: run build; render refuses until then)
  render       --run RUN --agent NAME
               one attempt, in the foreground: ChatGPT (2 tries), automatic checks on the flattened
               image (ink present, no ink in the outer 2% band = nothing cut off), then frame + upscale
               to 3840x2160 (Real-ESRGAN through a GPU-queue lease when the scale is above 1.5).
               A failed automatic check records the attempt as `auto-failed` with a fix for the next one.
  prep / submitted / ingest
               images.engine 'higgsfield' (scripts/hf.py): prep --run RUN --agent NAME reserves an attempt and prints
               its generate_image arguments (scene 1's selected job as the style reference); submitted --run RUN
               --attempt K --job ID --quoted N (or --unknown NOTE); ingest --run RUN --attempt K --url URL (or --failed
               REASON) downloads it, runs the automatic checks and frames it like `render`
  review-prep  --run RUN     review sheet (full, lettering close-up, style reference) + checklist
  verdict      --run RUN --file FILLED
               validate the reviewer's checklist, score it with render_scenes.score_checklist; a pass
               selects the attempt (title/title_4k.png). Any deduction other than layout fails the title
               (it must be only the correctly spelled title in the reference style). After the round's
               5th attempt the best reviewed attempt is selected and flagged.
  reopen       --run RUN --fix NOTE [--gate-round R]
               Gate 1 edit: new round (fresh 5 attempts) whose first render carries NOTE as its fix;
               drops the approval and title_4k.png. With --gate-round it runs once per Gate 1 round.

title.json (RUN/title/, owned by this script; read-only for others):
  {"text", "status": "not rendered"|"rendering"|"render stalled"|"render failed"|"auto-failed"|
   "awaiting review"|"failed review"|"passed"|"flagged"|"exhausted", "selected_attempt": k|null,
   "flagged", "approved", "approved_round", "approved_sha256", "round", "round_start", "gate_fix",
   "prompt", "attempts": [{"k", "state", "rendered_by", "fix", "engine", "prompt_used", "style_reference",
   "raw", "title_4k", "sha256", "auto": {...}, "verdict": {...}}]}
Gate 1 approval (gate1.py apply -> approve()) sets approved/approved_round/approved_sha256; a new round clears them.
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

import hf        # scripts/hf.py: the Higgsfield handoff (images.engine 'higgsfield')
import palette   # scripts/palette.py: the style's title-colour block, filled from its palette.json

REPO = Path(__file__).resolve().parents[1]
EDGE_BAND = 0.02          # ink in the outer 2% of the raw render = lettering cut off or crammed to the edge
EDGE_INK_MAX = 25         # stray pixels tolerated in that band
MIN_INK_FRAC = 0.005      # below 0.5% ink the canvas is effectively blank
TOLERATED = {"layout_not_followed"}   # the only review deduction a title may pass with (framing re-centres it)
SIZE = (3840, 2160)
ASPECT = "16:9"

_RS = None


def rs():
    """render_scenes.py as a module: its engine path, finishing, lock and scorer are reused, never copied."""
    global _RS
    if _RS is None:
        spec = importlib.util.spec_from_file_location("render_scenes", REPO / "scripts" / "render_scenes.py")
        _RS = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RS)
    return _RS


class Fail(Exception):
    pass


def say(msg: str) -> None:
    print(f"[title] {msg}", flush=True)


def tdir(run: Path) -> Path:
    return run / "title"


def load(run: Path) -> dict:
    p = tdir(run) / "title.json"
    if not p.is_file():
        raise Fail(f"no {p}; run `title.py build --run {run.as_posix()}` first")
    return json.loads(p.read_text(encoding="utf-8"))


def save(run: Path, doc: dict) -> None:
    doc["status"] = status_of(doc)
    tmp = tdir(run) / "title.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, tdir(run) / "title.json")


def log(run: Path, event: str, **fields) -> None:
    rs().log_event(tdir(run), event, **fields)


def prompt_for(text: str, style: str, run: Path) -> str:
    try:
        colour = palette.prompt_block(rs().STYLES, style, "title-colour", run)   # the run's palette
    except palette.PaletteError as e:
        raise Fail(f"prompt: {e}")
    return (f"A title card for an RSA Animate style whiteboard explainer video. Hand-letter exactly the words "
            f"\"{text}\" (spelled exactly like that, every letter, nothing added) in bold hand-drawn chisel-tip "
            f"whiteboard marker capitals, like the lettering in the style reference: {colour} The lettering is large "
            f"and centred, spanning about two thirds of the canvas width, with generous plain white margins on every "
            f"side; no letter comes near the edge. At most one small hand-drawn underline or flourish under the "
            f"words. Nothing else: no drawings, icons, characters, rabbits, arrows, boxes, frames, borders, banners "
            f"or extra words, no hand or pen, no shadows, gradients or grey wash. Plain white background.")


def style_reference(run: Path) -> Path:
    """Scene 1's selected raw image, the same style reference every later scene is rendered with."""
    st = rs().load_state(run / "scenes")
    try:
        return rs().reference_image(st, None)   # any id other than the first scene's -> the first scene's image
    except rs().Fail as e:
        raise Fail(f"style reference: {e}")


def round_attempts(doc: dict) -> list[dict]:
    return rs().round_attempts(doc)


def pid_alive(pid: int) -> bool:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259                  # STILL_ACTIVE


def status_of(doc: dict) -> str:
    if doc.get("flagged"):
        return "flagged"
    if doc.get("selected_attempt"):
        return "passed"
    cur = round_attempts(doc)
    if not cur:
        return "not rendered"
    last = cur[-1]
    if last["state"] in ("requested", "submitted", "unknown"):
        return {"requested": "awaiting submission", "submitted": "generating", "unknown": "submission unknown"}[last["state"]]
    if last["state"] == "rendering":
        return "rendering" if pid_alive(last["pid"]) else "render stalled"
    if last["state"] == "failed":
        return "exhausted" if len(cur) >= rs().MAX_ATTEMPTS else "render failed"
    if last["state"] == "auto-failed":
        return "exhausted" if len(cur) >= rs().MAX_ATTEMPTS else "auto-failed"
    if not last.get("verdict"):
        return "awaiting review"
    return "exhausted" if len(cur) >= rs().MAX_ATTEMPTS else "failed review"


def next_action(doc: dict, run: Path | None = None) -> str:
    s = status_of(doc)
    n = len(round_attempts(doc))
    if s in ("passed", "flagged"):
        return "done" if doc.get("approved") else "gate1 (approve the title at Gate 1)"
    if s == "rendering":
        return "wait (render in progress)"
    if s == "awaiting submission":
        k = round_attempts(doc)[-1]['k']
        note = hf.pending_note(run, f"title/a{k}") if run is not None else None
        return note or f"submit attempt {k}"
    if s == "generating":
        return f"wait for job {round_attempts(doc)[-1].get('job_id')} and ingest attempt {round_attempts(doc)[-1]['k']}"
    if s == "submission unknown":
        return f"reconcile attempt {round_attempts(doc)[-1]['k']} (check the Higgsfield account)"
    if s == "awaiting review":
        return f"review attempt {round_attempts(doc)[-1]['k']}"
    if s == "exhausted":
        return "exhausted: no attempt passed review and none can be flagged"
    if s in ("failed review", "auto-failed"):
        return f"render attempt {n + 1} of {rs().MAX_ATTEMPTS} with the review fix"
    return f"render attempt {n + 1} of {rs().MAX_ATTEMPTS}"


def last_fix(doc: dict) -> str:
    """Fixes for the next attempt: the gate note that opened this round, then every review or automatic-check fix of
    the round in order (an earlier fix still applies); several are numbered (render_scenes.last_fix)."""
    fixes = [doc.get("gate_fix", "")]
    for x in round_attempts(doc):
        if x.get("verdict"):
            fixes.append(x["verdict"].get("requiredFix", ""))
        elif x["state"] == "auto-failed":
            fixes.append(x["auto"].get("fix", ""))
    uniq = list(dict.fromkeys(f.strip() for f in fixes if f and f.strip()))
    if len(uniq) <= 1:
        return uniq[0] if uniq else ""
    return " ".join(f"({i}) {f}" for i, f in enumerate(uniq, 1)) + " Where two corrections differ, the later one wins."


def select(run: Path, doc: dict, att: dict, flagged: bool) -> None:
    doc["selected_attempt"], doc["flagged"] = att["k"], flagged
    shutil.copyfile(att["title_4k"], tdir(run) / "title_4k.png")


def exhaust(run: Path, doc: dict) -> None:
    cur = round_attempts(doc)
    if doc.get("selected_attempt") or len(cur) < rs().MAX_ATTEMPTS:
        return
    reviewed = [x for x in cur if x.get("verdict")]
    if reviewed:
        best = max(reviewed, key=lambda x: x["verdict"]["score"])
        select(run, doc, best, flagged=True)
        log(run, "flagged", selected=best["k"], score=best["verdict"]["score"])


def new_round(run: Path, doc: dict, reason: str, gate_fix: str = "") -> None:
    doc["round"] = doc.get("round", 1) + 1
    doc["round_start"] = len(doc["attempts"])
    doc.update(selected_attempt=None, flagged=False, gate_fix=gate_fix, approved=False, approved_round=None,
               approved_sha256=None)
    (tdir(run) / "title_4k.png").unlink(missing_ok=True)
    log(run, "new_round", round=doc["round"], reason=reason, gate_fix=gate_fix)


def selected(doc: dict) -> dict | None:
    k = doc.get("selected_attempt")
    return doc["attempts"][k - 1] if k else None


# ------------------------------------------------------------------ automatic checks

def auto_checks(raw: Path) -> dict:
    """Deterministic checks on the flattened render (render_scenes' ink threshold)."""
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(raw).convert("RGB"))
    ink = (a < rs().INK_THRESHOLD).any(axis=2)
    h, w = ink.shape
    bx, by = max(1, round(w * EDGE_BAND)), max(1, round(h * EDGE_BAND))
    band = ink.copy()
    band[by:h - by, bx:w - bx] = False
    frac, edge = float(ink.mean()), int(band.sum())
    problems, fixes = [], []
    if frac < MIN_INK_FRAC:
        problems.append(f"only {frac:.2%} of the canvas is ink")
        fixes.append("Letter the title large and bold; the last attempt was nearly blank.")
    if edge > EDGE_INK_MAX:
        problems.append(f"{edge} ink px in the outer {EDGE_BAND:.0%} band")
        fixes.append("Keep every letter and flourish well inside the canvas with wide white margins on all four "
                     "sides; the last attempt ran into the edge.")
    return {"ink_frac": round(frac, 4), "edge_ink_px": edge, "pass": not problems, "problems": problems,
            "fix": " ".join(fixes)}


# ------------------------------------------------------------------ commands

def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    script = json.loads((run / "script.json").read_text(encoding="utf-8"))
    existing = tdir(run) / "title.json"
    stored = json.loads(existing.read_text(encoding="utf-8")).get("text") if existing.is_file() else None
    text = (a.text or script.get("title_text") or stored or "").strip()
    if not text:
        raise Fail("build: no title text; pass --text or set script.json title_text")
    ref = style_reference(run)
    tdir(run).mkdir(exist_ok=True)
    p = tdir(run) / "title.json"
    style = rs().load_state(run / "scenes")["style"]
    prompt = prompt_for(text, style, run)
    with rs().locked(tdir(run)):
        if p.is_file():
            doc = json.loads(p.read_text(encoding="utf-8"))
            changed = doc["text"] != text or doc.get("prompt") != prompt
            if changed:
                why = "title text changed" if doc["text"] != text else "title prompt changed (style colours)"
                doc.update(text=text, prompt=prompt)
                new_round(run, doc, why)
        else:
            changed = True
            doc = {"text": text, "status": "not rendered", "selected_attempt": None, "flagged": False,
                   "approved": False, "approved_round": None, "approved_sha256": None, "round": 1, "round_start": 0,
                   "gate_fix": "", "prompt": prompt, "attempts": []}
        save(run, doc)
    if changed:
        log(run, "build", text=text)
    print("TITLE_JSON " + json.dumps({"text": text, "changed": changed, "style_reference": str(ref),
                                      "status": status_of(doc), "next": next_action(doc, run)}))
    return 0


def cmd_status(a) -> int:
    run = Path(a.run).resolve()
    p = tdir(run) / "title.json"
    if not p.is_file():
        print("TITLE_JSON " + json.dumps({"exists": False, "next": "build"}))
        return 0
    doc = json.loads(p.read_text(encoding="utf-8"))
    s, nxt = status_of(doc), next_action(doc, run)
    current = doc.get("prompt") == prompt_for(doc["text"], rs().load_state(run / "scenes")["style"], run)
    sel = selected(doc)
    print(f"title {doc['text']!r}  {s}  round {doc.get('round', 1)}  attempts {len(round_attempts(doc))}/"
          f"{rs().MAX_ATTEMPTS}  approved {bool(doc.get('approved'))}  NEXT: {nxt}")
    print("TITLE_JSON " + json.dumps({"exists": True, "text": doc["text"], "status": s, "next": nxt,
                                      "round": doc.get("round", 1), "attempts": len(round_attempts(doc)),
                                      "selected_attempt": doc.get("selected_attempt"), "flagged": doc.get("flagged", False),
                                      "approved": bool(doc.get("approved")), "prompt_current": current,
                                      "ready_for_gate1": s in ("passed", "flagged") and current,
                                      "title_4k": str(tdir(run) / "title_4k.png") if sel else None}))
    return 0


def cmd_render(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    out = tdir(run)
    adir, work = out / "attempts", out / "work"
    adir.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    with R.locked(out):
        doc = load(run)
        action = next_action(doc)
        if not action.startswith("render"):
            raise Fail(f"render: the title's next action is '{action}', not a render")
        if hf.on("images"):
            raise Fail("render: images.engine is 'higgsfield'; use `title.py prep`, submit through the Higgsfield MCP, "
                       "then `title.py submitted` and `title.py ingest`")
        if doc.get("prompt") != prompt_for(doc["text"], R.load_state(run / "scenes")["style"], run):
            raise Fail(f"render: the title prompt is out of date (style colours changed); run "
                       f"`title.py build --run {run.as_posix()}` first")
        if status_of(doc) == "render stalled":
            doc["attempts"][-1].update(state="failed", error="stalled: the render process died")
        ref = style_reference(run)
        fix = last_fix(doc)
        k = len(doc["attempts"]) + 1
        doc["attempts"].append({"k": k, "state": "rendering", "rendered_by": a.agent, "fix": fix, "pid": os.getpid(),
                                "style_reference": str(ref), "started": time.strftime("%Y-%m-%d %H:%M:%S")})
        save(run, doc)
        prompt = R.with_fix(doc["prompt"], fix)
    log(run, "render_start", attempt=k, agent=a.agent, fix=fix)
    stem = f"title_a{k}"
    raw, big = adir / f"{stem}_raw.png", adir / f"{stem}_4k.png"
    rec: dict = {}
    try:
        say(f"attempt {k}: ChatGPT render (style reference {ref})")
        src, why = R.chatgpt(prompt, ref, work, f"{stem}_chatgpt.png", ASPECT)
        if src is None:
            raise Fail(f"ChatGPT failed {R.CHATGPT_TRIES} times: {why}")
        rec.update(engine="chatgpt", prompt_used=prompt, source=str(src), raw=str(raw))
        _, scale, _ = R.frame(src, raw, SIZE)
        rec["auto"] = auto_checks(raw)
        if not rec["auto"]["pass"]:
            rec["state"] = "auto-failed"
        else:
            job = {"source": str(src), "raw": str(raw), "scene_4k": str(big), "size": list(SIZE), "work_dir": str(work),
                   "stem": stem}
            if scale > R.AI_UPSCALE_ABOVE:
                jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
                jobs.write_text(json.dumps({"finish": job, "results": str(results)}), encoding="utf-8")
                R.gpu_lease(f"scribeboard title attempt {k}: upscale", jobs)
                rec.update(json.loads(results.read_text(encoding="utf-8"))["finish"])
            else:
                rec.update(R.finish(job))
            meta = {"engine": "chatgpt", "style": "title", "attempt": k, "source": str(src), "style_reference": str(ref),
                    "fix": fix, "upscale": rec["upscale"], "driver": "scribeboard title.py"}
            for p in (raw, big):
                R.stamp(p, prompt, meta)
            rec.update(state="rendered", title_4k=str(big), sha256=R.sha256(big))
    except Exception as e:          # any failure is recorded, so the attempt never stays "rendering"
        rec.update(state="failed", error=str(e)[-800:])
    rec["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with R.locked(out):
        doc = load(run)
        doc["attempts"][k - 1].update(rec)
        doc["attempts"][k - 1].pop("pid", None)
        exhaust(run, doc)
        save(run, doc)
    log(run, "render_" + rec["state"], attempt=k, error=rec.get("error"), auto=rec.get("auto"), upscale=rec.get("upscale"))
    if rec["state"] == "failed":
        raise Fail(f"render: title attempt {k} failed: {rec['error']}")
    if rec["state"] == "auto-failed":
        raise Fail(f"render: title attempt {k} failed the automatic checks ({'; '.join(rec['auto']['problems'])}); "
                   f"raw image {raw}; the next attempt carries the fix")
    print("TITLE_RENDER_JSON " + json.dumps({"attempt": k, "title_4k": str(big), "auto": rec["auto"],
                                             "upscale": rec["upscale"]}))
    return 0


# ------------------------------------------------------------------ Higgsfield handoff (images.engine 'higgsfield')


def scene1_job(run: Path) -> str:
    st = rs().load_state(run / "scenes")
    first = st["scenes"][str(st["order"][0])]
    k = first.get("selected")
    job = first["attempts"][k - 1].get("job_id") if k else None
    if not job:
        raise Fail("prep: scene 1 has no selected Higgsfield attempt; it is the title's style reference")
    return job


def cmd_prep(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    out = tdir(run)
    (out / "work").mkdir(parents=True, exist_ok=True)
    with R.locked(out):
        doc = load(run)
        action = next_action(doc)
        if not action.startswith("render"):
            raise Fail(f"prep: the title's next action is '{action}', not a render")
        if doc.get("prompt") != prompt_for(doc["text"], R.load_state(run / "scenes")["style"], run):
            raise Fail(f"prep: the title prompt is out of date; run `title.py build --run {run.as_posix()}` first")
        k = len(doc["attempts"]) + 1
        import runsetup
        if runsetup.required():                     # the title is drawn from scene 1: the user approves it first
            import gate6
            why = gate6.style_problem(run)
            if why:
                raise Fail(f"prep: the title waits: {why}")
        job = scene1_job(run)                       # every other check first: a refusal reserves nothing
        fix = last_fix(doc)
        prompt = hf.ref_lead(True, []) + R.with_fix(doc["prompt"], fix)
        try:
            req = hf.image_request(prompt, run, [{"value": job, "role": "image_references"}], ASPECT)
            budget = hf.ledger_check(run, "image", [f"title/a{k}"],   # checks the setup, reserves
                                     req["arguments"]["params"].get("model"))
        except hf.HFError as e:
            raise Fail(str(e))
        rfile = out / "work" / f"title_a{k}_request.json"
        rfile.write_text(json.dumps(req, indent=2), encoding="utf-8")
        doc["attempts"].append({"k": k, "state": "requested", "rendered_by": a.agent, "fix": fix, "engine": "higgsfield",
                                "prompt_used": prompt, "style_reference": str(style_reference(run)),
                                "style_reference_job": job, "request": str(rfile),
                                "started": time.strftime("%Y-%m-%d %H:%M:%S")})
        save(run, doc)
    log(run, "render_requested", attempt=k, agent=a.agent, fix=fix)
    hf.emit_request("image", f"title/a{k}", req, {"attempt": k, "credits": budget, "request_file": str(rfile)},
                    run=run)
    return 0


def _attempt(doc: dict, k: int, states: tuple[str, ...]) -> dict:
    if not 1 <= k <= len(doc["attempts"]):
        raise Fail(f"the title has no attempt {k}")
    att = doc["attempts"][k - 1]
    if att["state"] not in states:
        raise Fail(f"title attempt {k} is '{att['state']}', expected {' or '.join(states)}")
    return att


def cmd_submitted(a) -> int:
    run = Path(a.run).resolve()
    ref = f"title/a{a.attempt}"
    with rs().locked(tdir(run)):
        doc = load(run)
        att = _attempt(doc, a.attempt, ("requested", "unknown"))
        if a.unknown:
            att.update(state="unknown", unknown=a.unknown)
            hf.ledger_record(run, "image", ref, None, a.quoted, "unknown")
        else:
            if not a.job:
                raise Fail("submitted: give --job ID (or --unknown NOTE when the outcome is unclear)")
            att.update(state="submitted", job_id=a.job, quoted=a.quoted)
            hf.ledger_record(run, "image", ref, a.job, a.quoted, "submitted")
        save(run, doc)
    log(run, "render_submitted", attempt=a.attempt, job_id=a.job, quoted=a.quoted, unknown=a.unknown)
    print("SUBMITTED_JSON " + json.dumps({"title_attempt": a.attempt, "state": att["state"], "job_id": att.get("job_id")}))
    return 0


def cmd_ingest(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    out = tdir(run)
    k = a.attempt
    with R.locked(out):
        # --failed also closes a requested attempt that was never submitted (the user declined its price)
        att = dict(_attempt(load(run), k, ("requested", "submitted", "unknown") if a.failed else ("submitted",)))
    adir, work = out / "attempts", out / "work"
    adir.mkdir(exist_ok=True)
    stem = f"title_a{k}"
    raw, big = adir / f"{stem}_raw.png", adir / f"{stem}_4k.png"
    rec: dict = {}
    if a.failed:
        rec.update(state="failed", error=(f"closed before it was submitted: {a.failed}" if att["state"] == "requested"
                                          else f"Higgsfield job failed: {a.failed}"))   # the ledger is closed below
    else:
        try:
            src = hf.download(a.url, work / f"{stem}_higgsfield.png", att.get("job_id"))
            rec.update(engine="higgsfield", source=str(src), source_url=a.url, raw=str(raw))
            _, scale, _ = R.frame(src, raw, SIZE)
            rec["auto"] = auto_checks(raw)
            if not rec["auto"]["pass"]:
                rec["state"] = "auto-failed"
            else:
                job = {"source": str(src), "raw": str(raw), "scene_4k": str(big), "size": list(SIZE),
                       "work_dir": str(work), "stem": stem}
                if scale > R.AI_UPSCALE_ABOVE:
                    jobs, results = work / f"{stem}_jobs.json", work / f"{stem}_results.json"
                    jobs.write_text(json.dumps({"finish": job, "results": str(results)}), encoding="utf-8")
                    R.gpu_lease(f"scribeboard title attempt {k}: upscale", jobs)
                    rec.update(json.loads(results.read_text(encoding="utf-8"))["finish"])
                else:
                    rec.update(R.finish(job))
                meta = {"engine": "higgsfield", "job_id": att["job_id"], "style": "title", "attempt": k,
                        "source": a.url, "style_reference_job": att.get("style_reference_job"), "fix": att.get("fix", ""),
                        "upscale": rec["upscale"], "driver": "scribeboard title.py"}
                for p in (raw, big):
                    R.stamp(p, att["prompt_used"], meta)
                rec.update(state="rendered", title_4k=str(big), sha256=R.sha256(big))
        except (Fail, R.Fail, hf.HFError, OSError) as e:
            raise Fail(f"ingest: title attempt {k}: {e}; the job is paid, retry `title.py ingest` (do not resubmit)")
    rec["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with R.locked(out):
        doc = load(run)
        now = doc["attempts"][k - 1]["state"]
        if now != att["state"]:   # another command changed the attempt meanwhile (a `submitted` landed): re-check
            raise Fail(f"ingest: title attempt {k} changed from '{att['state']}' to '{now}' meanwhile; run `status` "
                       f"and ingest again")
        if rec["state"] == "failed":
            try:
                hf.ledger_close(run, f"title/a{k}", now)
            except hf.HFError as e:
                raise Fail(f"ingest: {e}")
        doc["attempts"][k - 1].update(rec)
        exhaust(run, doc)
        save(run, doc)
    log(run, "render_" + rec["state"], attempt=k, error=rec.get("error"), auto=rec.get("auto"), upscale=rec.get("upscale"))
    if rec["state"] == "failed":
        raise Fail(f"ingest: title attempt {k} failed: {rec['error']}"
                   + (" (never submitted: its reservation is released, no credits)" if att["state"] == "requested"
                      else " (Higgsfield refunds failed jobs)"))
    if rec["state"] == "auto-failed":
        raise Fail(f"ingest: title attempt {k} failed the automatic checks ({'; '.join(rec['auto']['problems'])}); "
                   f"raw image {raw}; the next attempt carries the fix")
    print("TITLE_RENDER_JSON " + json.dumps({"attempt": k, "title_4k": str(big), "auto": rec["auto"],
                                             "upscale": rec["upscale"]}))
    return 0


def review_dir(run: Path, k: int) -> Path:
    return tdir(run) / "attempts" / f"title_a{k}_review"


def cmd_review_prep(a) -> int:
    import numpy as np
    from PIL import Image
    run = Path(a.run).resolve()
    doc = load(run)
    if not next_action(doc).startswith("review"):
        raise Fail(f"review-prep: the title's next action is '{next_action(doc)}', not a review")
    att = doc["attempts"][-1]
    k = att["k"]
    rdir = review_dir(run, k)
    rdir.mkdir(exist_ok=True)
    im = Image.open(att["title_4k"]).convert("RGB")
    w, h = im.size
    im.resize((1600, round(1600 * h / w)), Image.LANCZOS).save(rdir / "full.png")
    ink = (np.asarray(im) < rs().INK_THRESHOLD).any(axis=2)
    ys, xs = np.where(ink)
    pad = 40
    crop = im.crop((max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad), min(w, int(xs.max()) + pad + 1),
                    min(h, int(ys.max()) + pad + 1)))
    if crop.width > 2400:
        crop = crop.resize((2400, round(2400 * crop.height / crop.width)), Image.LANCZOS)
    crop.save(rdir / "lettering.png")
    ref = Image.open(att["style_reference"]).convert("RGB")
    ref.resize((1600, round(1600 * ref.height / ref.width)), Image.LANCZOS).save(rdir / "style_reference.png")
    checklist = {
        "scene": "title", "attempt": k, "sha256": att["sha256"], "reviewed_by": "",
        "elements": [{"id": "title", "desc": f"the hand-lettered title \"{doc['text']}\"", "present": None, "box": None,
                      "note": ""}],
        "box_space": {"width": 1600, "height": round(1600 * h / w), "image": "full.png"},
        "labels": [{"element": "title", "expected": doc["text"], "read": None, "correct": None}],
        "extra_text": [], "extra_objects": [],
        "style": {k2: None for k2 in rs().STYLE_KEYS},
        "elements_touching": False, "layout_followed": None, "matches_reference_style": None,
        "characters": [], "arrows": [], "worstIssue": "", "requiredFix": "",
    }
    (rdir / "checklist.json").write_text(json.dumps(checklist, indent=2, ensure_ascii=False), encoding="utf-8")
    log(run, "review_prep", attempt=k)
    print("TITLE_REVIEW_JSON " + json.dumps({"review_dir": str(rdir), "full": str(rdir / "full.png"),
                                             "lettering": str(rdir / "lettering.png"),
                                             "style_reference": str(rdir / "style_reference.png"),
                                             "checklist": str(rdir / "checklist.json"), "text": doc["text"],
                                             "fix_applied": att.get("fix", "")}, ensure_ascii=False))
    return 0


def cmd_verdict(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    c = json.loads(Path(a.file).read_text(encoding="utf-8"))
    with R.locked(tdir(run)):
        doc = load(run)
        if not next_action(doc).startswith("review"):
            raise Fail(f"verdict: the title's next action is '{next_action(doc)}', not a review")
        att = doc["attempts"][-1]
        rdir = review_dir(run, att["k"])
        template = json.loads((rdir / "checklist.json").read_text(encoding="utf-8"))
        try:
            R.validate_checklist(c, template)
        except R.Fail as e:
            raise Fail(str(e))
        if c.get("elements_touching") is not False or c.get("arrows") != []:
            raise Fail("verdict: checklist rejected:\n  elements_touching must stay false and arrows must stay [] "
                       "(the title is one element; an arrow is an extra object)")
        if c["reviewed_by"].strip() == att["rendered_by"].strip():
            raise Fail(f"verdict: reviewer '{c['reviewed_by']}' also rendered this attempt; use a fresh review agent")
        if R.sha256(Path(att["title_4k"])) != att["sha256"]:
            raise Fail("verdict: the attempt's 4K image changed after rendering")
        score, passed, deductions = R.score_checklist(c)
        blocking = [d for d in deductions if d.split()[1] not in TOLERATED]
        passed = passed and not blocking
        if not passed and not str(c.get("requiredFix", "")).strip():
            raise Fail(f"verdict: the title fails ({'; '.join(deductions) or 'score'}); requiredFix must say what the "
                       f"next render should change")
        v = {"score": score, "pass": passed, "deductions": deductions, "worstIssue": c.get("worstIssue", ""),
             "requiredFix": c.get("requiredFix", ""), "reviewed_by": c["reviewed_by"], "checklist": c,
             "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        att["verdict"] = v
        (rdir / "verdict.json").write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
        if passed:
            select(run, doc, att, flagged=False)
        else:
            exhaust(run, doc)
        save(run, doc)
    log(run, "verdict", attempt=att["k"], score=score, passed=passed, deductions=deductions, reviewer=c["reviewed_by"],
        fix=v["requiredFix"])
    print("TITLE_VERDICT_JSON " + json.dumps({"attempt": att["k"], "score": score, "pass": passed,
                                              "flagged": doc.get("flagged", False), "status": status_of(doc),
                                              "deductions": deductions}))
    return 0


# ------------------------------------------------------------------ Gate 1 (called by gate1.py apply)

def reopen(run: Path, fix: str, gate_round: int | None = None) -> dict:
    """Gate 1 edit: a new round whose first render carries `fix`. Runs once per gate round."""
    if not fix.strip():
        raise Fail("reopen: the fix must say what the next title render should change")
    with rs().locked(tdir(run)):
        doc = load(run)
        if gate_round is not None and doc.get("reopened_by_gate_round") == gate_round:
            return {"round": doc["round"], "already": True}
        new_round(run, doc, "gate edit", gate_fix=fix.strip())
        if gate_round is not None:
            doc["reopened_by_gate_round"] = gate_round
        save(run, doc)
    return {"round": doc["round"], "already": False}


def approve(run: Path, gate_round: int, sha: str) -> None:
    """Gate 1 approve: the approval is tied to the exact image the page showed."""
    with rs().locked(tdir(run)):
        doc = load(run)
        sel = selected(doc)
        if sel is None or sel["sha256"] != sha:
            raise Fail("title: the selected title image changed after the Gate 1 page was built; rebuild the round")
        if not (doc.get("approved") and doc.get("approved_sha256") == sha):
            doc.update(approved=True, approved_round=gate_round, approved_sha256=sha)
            save(run, doc)
            log(run, "approved", gate_round=gate_round, attempt=sel["k"])


def cmd_reopen(a) -> int:
    res = reopen(Path(a.run).resolve(), a.fix, a.gate_round)
    print("TITLE_REOPEN_JSON " + json.dumps(res))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--run", required=True)
    b.add_argument("--text", default="")
    for name in ("status", "review-prep"):
        sub.add_parser(name).add_argument("--run", required=True)
    r = sub.add_parser("render")
    r.add_argument("--run", required=True)
    r.add_argument("--agent", required=True, help="name of the render agent (must differ from the reviewer)")
    v = sub.add_parser("verdict")
    v.add_argument("--run", required=True)
    v.add_argument("--file", required=True, help="the reviewer's filled-in checklist")
    ro = sub.add_parser("reopen")
    ro.add_argument("--run", required=True)
    ro.add_argument("--fix", required=True)
    ro.add_argument("--gate-round", type=int, default=None)
    hp = sub.add_parser("prep")
    hp.add_argument("--run", required=True)
    hp.add_argument("--agent", required=True)
    hs = sub.add_parser("submitted")
    hs.add_argument("--run", required=True)
    hs.add_argument("--attempt", type=int, required=True)
    hs.add_argument("--job", default="")
    hs.add_argument("--quoted", type=float, default=None)
    hs.add_argument("--unknown", default="")
    hi = sub.add_parser("ingest")
    hi.add_argument("--run", required=True)
    hi.add_argument("--attempt", type=int, required=True)
    hx = hi.add_mutually_exclusive_group(required=True)
    hx.add_argument("--url")
    hx.add_argument("--failed")
    a = ap.parse_args(argv)
    fn = {"build": cmd_build, "status": cmd_status, "render": cmd_render, "review-prep": cmd_review_prep,
          "verdict": cmd_verdict, "reopen": cmd_reopen, "prep": cmd_prep, "submitted": cmd_submitted,
          "ingest": cmd_ingest}
    try:
        return fn[a.cmd](a)
    except (Fail, rs().Fail) as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
