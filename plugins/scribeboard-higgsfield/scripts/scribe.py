#!/usr/bin/env python
r"""scribe.py - the Scribeboard run driver. Looks at a run folder and prints the ONE next step.

It never guesses and never skips: each stage is checked from the files on disk, in order, and the
first stage that is missing or stale is the next step. Re-run `status` after every step.

A new run (no script.json) approves the narration text before any visual script exists:
  0 narration   RUN/narration.json exists, validates and names its       (writer agent pass 1, references/
                writer (written_by): narration text only, one            SCRIPT-WRITER.md, voice profile;
                paragraph per scene to come                              voice.py validate-narration)
  0b voice      RUN/voice/voice.json passed or flagged for the current   (voice.py review-prep / verdict: a
                narration fingerprint (sentences, voice config, source,   separate review agent,
                profile version); failed = the writer revises the         references/VOICE-REVIEW.md)
                flagged sentences
  0c gate 0     rounds until the latest round's approved_fingerprint     (gate0.py build / apply; its edits or
                equals the current narration fingerprint; after an      chosen version go to the writer)
                approval a round asks only about the changed paragraphs
  1 script      RUN/script.json validates, names its writer and has the  (writer agent pass 2, from the
                approved narration's fingerprint: one scene per           approved narration)
                paragraph, the same sentence ids and texts, voice and
                source (so the voice review still holds)
  setup         With Higgsfield (runsetup.required(): images.engine or narration.engine 'higgsfield') the run
                starts with pass 0 (story_cast.json) and the start page (gate5.py: words, voice, title, image
                model, style, palette, each character, budget -> RUN/start.json). The narration is recorded and
                the character sheets drawn before the gate0 page (Gate 1 on the rail), which plays the narration,
                shows each sheet to approve and asks the budget; 0c passes only when the narration and every sheet
                are approved AND RUN/setup.json is complete and valid (runsetup.load). narration.json must copy the
                start's title and characters (pass 1); script.json must use the start's style, title and exactly
                its cast (pass 2). There is no default for any of it, and every paid prep refuses without the start
                or setup it needs (hf.ledger_check). There is no board page (Gate 2): the video page shows the
                board.
A legacy run (script.json and no narration.json) skips 0-0c and checks instead:
  1 script      RUN/script.json exists, validates and names its writer
  1b voice      voice.json passed or flagged for the current script.json fingerprint
Then, for both:
  2 narration   RUN/alignment.json matches every sentence, in the voice  (narrate.py run; settings narration.engine
                of settings narration.engine                             'recorded': narrate.py reading, the user
                                                                          records narration/recording.wav, then run)
  2b characters every declared character has characters/<id>_sheet.png   (characters.py build; with a setup only
                the characters it gave a sheet: sheets are optional)
  3 scenes      RUN/scenes/state.json matches script + budget            (render_scenes.py init / sync)
  4 images      every scene selected, reviewed, masked, its connector    (/render-scribe loop; rebox)
                arrows split off (arrows_mask); the image gate passes
  4a report     scenes/_contact.png + run_summary.md, newer than every   (render_scenes.py report --no-open;
                selected scene image (only before the first Gate 1        never asked once Gate 1 has a round)
                round)
  4b title      title/title.json passed or flagged, with script.json      (title.py build / render / review)
                title_text (when set) as its text
  5 gate 1      rounds until every scene (on its current image sha) and   (gate1.py build / apply + edits)
                the title are approved; the page also takes script,
                narration and character-sheet notes
  6 board       videos/board_layout.json (its fingerprint = the current    (stitch.py board)
                images, masks, title, segment order, module layout
                values and palette.json arrow colours, flow arrows
                included) and videos/board.png
  7 gate 2      rounds until the latest board round approved the current  (gate2.py build / apply; edits go to
                layout fingerprint (one question: the board image, with   module.json layout, then stitch.py board)
                its flow arrows); no video renders before it
  8 video       videos/final.mp4, final_timeline.json (v2, made from the   (stitch.py build)
                approved layout) and every Scene-NN.mp4, newer than the
                alignment and narration, made with the current module
                drawing/schedule/camera values
  9 gate 3      rounds until the whole video and every scene clip are   (gate3.py build / apply; camera notes go
                approved for the current build                            to module.json camera (shots) or
                                                                          stitch.py, then stitch.py build)

Usage:  python scripts/scribe.py status --run RUN [--style STYLE]
The style is the run's: once scenes/state.json exists, the style it was initialised with (for everything); before
that script.json `style`, else narration.json `style`, else --style (a new run that names none), else rsa_animate.
It is printed first (`style:` line, NEXT_JSON "style") and goes into characters.py build and render_scenes.py init.
When script.json / narration.json name another style than state.json, the next step is a STOP (stage `style`):
nothing is dispatched until the run names one style.
Prints human lines, then `NEXT_JSON {"stage", "action", "command"|null, "detail", "style"}`.
After applying a gate 1 round's scene/segment edits, the orchestrator writes RUN/gates/gate1-rN/edits_done.json
({"round": N, "actions": [...]}) so the driver knows the round's edits are in (a title-only edit needs none:
gate1.py apply reopens the title itself). Gate 2 records the layout fingerprint it showed in its gate.json and
changes.json (`approved_fingerprint`); a round whose board was rebuilt is superseded, and rounds of the old
per-scene Gate 2 are superseded by the next round number. Gate 3 records final_timeline.json's fingerprints: a round
is superseded once the video is rebuilt, and an approval holds while the build fingerprint is unchanged. A run whose
video was approved at the old per-scene Gate 2 (and has no board_layout.json) keeps that video and goes to Gate 3.
A Gate 0 round that asked for edits (or another version) waits for the writer to rewrite narration.json; the new
fingerprint then sends it through the voice review and into the next Gate 0 round.
"""
from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import hf               # scripts/hf.py: images.engine / narration.engine 'higgsfield'
import settings as cfg   # scripts/settings.py: python, styles_dir
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

REPO = Path(__file__).resolve().parents[1]
PY = cfg.python().as_posix()
S = REPO / "scripts"
# The agent briefs: references/ beside scripts/ in the installed skill folder, else the repo's skill copy.
REFS = "references" if (REPO / "references").is_dir() else ".claude/skills/render-scribe/references"


def module_path(style: str) -> Path:
    return cfg.path("styles_dir") / style / "module.json"


def shown(p: Path) -> str:
    """A path for a message: relative to the repo when inside it."""
    return p.relative_to(REPO).as_posix() if p.is_relative_to(REPO) else p.as_posix()


@functools.cache
def load(name: str):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gatelib():
    return load("gatelib")


def cmd(script: str, *args: str) -> str:
    return cfg.command(PY, S / script, *args)


def run_json(argv: list[str], tag: str) -> dict:
    p = subprocess.run([PY, *argv], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    for line in p.stdout.splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    raise SystemExit(f"FAILED {argv[0]} printed no {tag} (exit {p.returncode}): {(p.stdout + p.stderr)[-600:]}")


def read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def rounds(run: Path, gate: str) -> list[int]:
    d = run / "gates"
    return sorted(int(p.name.split("-r")[1]) for p in d.glob(f"{gate}-r*") if p.is_dir()) if d.is_dir() else []


def round_pending(run: Path, gate: str, r: int) -> dict | None:
    """The wait/apply action while round r has no changes.json, else None."""
    rdir = run / "gates" / f"{gate}-r{r}"
    if (rdir / "changes.json").is_file():
        return None
    pointer = read(rdir / "gate.json") if (rdir / "gate.json").is_file() else {}
    answers = Path(pointer.get("expectedAnswers", ""))
    if pointer and answers.is_file():
        return {"action": f"apply {gate} round {r}", "command": cmd(f"{gate}.py", "apply", "--run", run.as_posix(), "--round", str(r))}
    return {"action": f"WAIT for the user's {gate} round {r} answers", "command": None,
            "detail": f"page {pointer.get('html')}; write the pasted JSON to {answers}"}



# ------------------------------------------------------------------ stages

def voice_stage(run: Path) -> dict | None:
    """None once voice/voice.json is passed or flagged for the reviewed text (narration.json, or script.json in a
    legacy run), else the next voice step."""
    st = run_json([str(S / "voice.py"), "status", "--run", str(run)], "VOICE_JSON")
    nxt = str(st["next"])
    what = st.get("file", "script.json")
    if nxt == "done":
        return None
    if nxt.startswith("review"):
        return {"stage": "voice", "action": "voice review (separate review agent, references/VOICE-REVIEW.md)",
                "command": cmd("voice.py", "review-prep", "--run", run.as_posix(), "--agent", "<review-agent>"),
                "detail": f"{nxt} of {what} ({st['status']}{': ' + ', '.join(st['stale_parts']) + ' changed' if st['stale_parts'] else ''}); "
                          f"the reviewer must not be any writer of this text (written_by now: {st['written_by']!r}); "
                          f"it ends with voice.py verdict --run {run.as_posix()} --file <review_dir>/filled.json"}
    if nxt.startswith("revise"):
        return {"stage": "voice", "command": None,
                "action": f"revise {what} per RUN/voice/voice.json flags (writer agent, references/SCRIPT-WRITER.md)",
                "detail": f"round {st['round']} failed (score {st['score']}): "
                          + "; ".join(f"{f['id']} -> {f['fix']!r}" for f in st["flags"])}
    return {"stage": "voice", "action": f"WAIT: {nxt}", "command": None, "detail": st.get("status")}


def gate0_stage(run: Path, fp: str) -> dict | None:
    """None when the latest Gate 0 round approved the current narration fingerprint `fp`, else the next action."""
    rs_ = rounds(run, "gate0")
    r = rs_[-1] if rs_ else 0

    def build(n: int, why: str = "") -> dict:
        out = {"stage": "gate0", "action": f"build gate0 round {n}",
               "command": cmd("gate0.py", "build", "--run", run.as_posix(), "--round", str(n))}
        return {**out, "detail": why} if why else out

    if r == 0:
        return build(1)
    rdir = run / "gates" / f"gate0-r{r}"
    if not (rdir / "gate.json").is_file():
        return build(r, f"round {r} has no gate.json (its build did not finish)")
    pointer0 = read(rdir / "gate.json")
    shown = pointer0.get("fingerprint")
    if not (rdir / "changes.json").is_file():
        if shown != fp:
            return build(r + 1, f"round {r} shows an older narration; it is superseded")
        if load("runsetup").required() and \
                (pointer0.get("sheets") or {}).get("shown_sha256", {}) != load("gate4").sheet_shas(run):
            return build(r + 1, f"round {r} shows other character sheets than the run has now; it is superseded")
        return {"stage": "gate0", **round_pending(run, "gate0", r)}
    ch = read(rdir / "changes.json")
    if ch.get("approved_fingerprint") == fp:
        missing = setup_problem(run)
        if missing:   # the narration is approved, but the setup page's answers are missing or no longer valid
            return build(r + 1, f"setup: {missing}")
        return None
    if shown != fp:
        return build(r + 1, f"narration.json changed since round {r} "
                            f"{'approved it' if ch.get('approved') else 'asked for edits'}")
    what = [f"version {ch['version']}"] if ch.get("version", "current") != "current" else []
    what += [f"paragraph {p['id']}" for p in ch.get("paragraphs", []) if p.get("action") == "edit"]
    if (ch.get("narration") or {}).get("action") == "edit":
        what.append("the whole narration")
    return {"stage": "gate0", "command": None,
            "action": f"send the gate0 round {r} answers to the writer agent, who rewrites narration.json "
                      f"(references/SCRIPT-WRITER.md, revision after Gate 0)",
            "detail": f"{', '.join(what)}; changes: {(rdir / 'changes.json').as_posix()}"}


def setup_problem(run: Path) -> str | None:
    """With setup required (runsetup.py: Higgsfield makes pictures or narration): why RUN/setup.json is not a
    complete, valid setup for the run, else None. Without setup required: always None."""
    rsu = load("runsetup")
    if not rsu.required():
        return None
    try:
        rsu.load(run)
        return None
    except rsu.SetupError as e:
        return str(e)


def script_matches_setup(run: Path, script: dict, writer_brief: str) -> dict | None:
    """With setup required: None when script.json uses the setup's style and exactly its cast (no added, dropped or
    photo-referenced characters), else the fix for the writer."""
    rsu = load("runsetup")
    if not rsu.required():
        return None
    setup = rsu.load(run)      # gate 0 passed, so the setup is valid
    errs = []
    if script.get("style") != setup["style"]:
        errs.append(f"style is {script.get('style')!r}, but the setup page chose {setup['style']!r}")
    have = [c.get("id") for c in script.get("characters") or []]
    if sorted(map(str, have)) != sorted(setup["cast"]):
        errs.append(f"characters are {', '.join(map(str, have)) or 'none'}, but the setup page has "
                    f"{', '.join(setup['cast']) or 'none'} (pass 2 may not add or drop characters)")
    refs = [c.get("id") for c in script.get("characters") or [] if c.get("reference")]
    if refs:
        errs.append(f"characters {', '.join(map(str, refs))} set `reference`; photos come only from the setup page")
    title = rsu.start_title(run)
    if script.get("title_text") != title:   # the title card shows the user's own title
        errs.append(f"title_text must be the start page's title, exactly: {title!r}")
    cast = setup["cast"]   # the sheets were drawn from the start page's looks before the script existed
    off = [c.get("id") for c in script.get("characters") or []
           if (c.get("desc"), c.get("name")) != ((cast.get(c.get("id")) or {}).get("look"),
                                                 (cast.get(c.get("id")) or {}).get("name"))]
    if off:
        errs.append(f"characters {', '.join(map(str, off))}: name and desc must be the start page's name and "
                    f"look, exactly (RUN/start.json cast)")
    if not errs:
        return None
    return {"stage": "script", "command": None,
            "action": f"fix script.json to match the run's setup (writer agent pass 2, {writer_brief})",
            "detail": "; ".join(errs) + f" (RUN/setup.json)"}


def style_stage(run: Path) -> dict | None:
    """The style page (gate6.py, setup runs): None when the newest round approved scene 1 as it is now, else the next
    step. Runs once scene 1 passed its review, before any other scene or the title is drawn."""
    g6 = load("gate6")
    if g6.style_problem(run) is None:
        return None
    rs_ = g6.rounds(run)
    r = rs_[-1] if rs_ else 0
    now = g6.scene1_sha(run)

    def build(n: int, why: str = "") -> dict:
        return {"stage": "style", "action": f"build the style page round {n} (the user approves scene 1 before the "
                                            f"other pictures are drawn)", "detail": why,
                "command": cmd("gate6.py", "build", "--run", run.as_posix(), "--round", str(n))}

    if r == 0:
        return build(1)
    rdir = run / "gates" / f"gate6-r{r}"
    if not (rdir / "gate.json").is_file():
        return build(r, f"round {r} has no page yet")
    shown = read(rdir / "gate.json").get("shown_sha256")
    if not (rdir / "changes.json").is_file():
        if shown != now:
            return build(r + 1, "scene 1 changed since the page was built; it is superseded")
        return {"stage": "style", **round_pending(run, "gate6", r)}
    ch = read(rdir / "changes.json")
    if ch.get("approved") is not True and shown == now:   # an edit: redraw scene 1 with the user's note
        return {"stage": "style", "action": f"redraw scene 1 with the style page round {r} note (a new round of "
                                            f"scene 1; then its review, then the next style round)",
                "command": cmd("render_scenes.py", "reopen", "--out", (run / "scenes").as_posix(), "--scene", "1",
                               "--fix", ch.get("note") or ""),
                "detail": f"note: {ch.get('note')!r}"}
    return build(r + 1, "scene 1 was redrawn" if shown != now else "")


def sheets_stage(run: Path, style: str) -> tuple[dict | None, dict]:
    """(next step, characters status): the character sheets still to make, upload, wait for or review."""
    ch = run_json([str(S / "characters.py"), "status", "--run", str(run)], "CHARACTERS_JSON")
    if ch["ready"]:
        return None, ch
    if hf.on("images"):
        cid, step = next((k, v) for k, v in ch["next"].items() if v != "done")
        command = (cmd("characters.py", "prep", "--run", run.as_posix(), "--id", cid, "--style", style)
                   if step.startswith("prep") else
                   cmd("characters.py", "upload", "--run", run.as_posix(), "--id", cid)
                   if step.startswith("upload") else None)
        return {"stage": "characters", "action": f"character sheet: {step} (Higgsfield handoff; see SKILL.md)",
                "command": command, "detail": json.dumps(ch["next"])}, ch
    return {"stage": "characters", "action": "build the character sheets",
            "command": cmd("characters.py", "build", "--run", run.as_posix(), "--style", style),
            "detail": ", ".join(k for k, v in ch["sheets"].items() if not v) + " missing"}, ch


def gate1_cast_stage(run: Path) -> dict | None:
    """Setup runs: Gate 1 shows each character sheet with an approve / edit question. None when the newest
    Gate 1 round that answered them approved every sheet as it is now, else the next step (a redraw with the note, or
    the next Gate 1 round)."""
    g4 = load("gate4")
    rec = g4.gate1_cast_record(run)
    now = g4.sheet_shas(run)
    if not now:
        return None
    if rec is None:
        return None   # no Gate 1 round has shown the sheets yet: gate0_stage builds it
    r, ch, shown = rec
    approved = ch.get("approved_sha256") or {}
    if ch.get("approved") is True and {k: approved.get(k) for k in now} == now:
        return None
    answered = g4.restored_since(run, r)   # a sheet restored after this round (the user's choice) answers its edit
    todo = [row for row in ch.get("characters", []) if row.get("action") == "edit"
            and now.get(row["id"]) == shown.get(row["id"]) and row["id"] not in answered]
    if todo:
        row = todo[0]
        return {"stage": "characters", "command": cmd("characters.py", "prep", "--run", run.as_posix(), "--id",
                                                      row["id"], "--force", "--fix", row["note"]),
                "action": f"redraw the sheet of {row['id']} with the Gate 1 round {r} note, then the Higgsfield handoff "
                          f"and a fresh sheet review (SKILL.md)",
                "detail": f"edits still to redraw: {', '.join(t['id'] for t in todo)}"}
    return {"stage": "gate0", "action": f"build gate0 round {r + 1} (a character sheet changed since round {r} "
                                        f"{'approved it' if ch.get('approved') else 'asked to change it'})",
            "command": cmd("gate0.py", "build", "--run", run.as_posix(), "--round", str(r + 1))}


def narration_audio_stage(run: Path) -> dict | None:
    """None once every sentence has a passing take and the track and alignment are current (narrate.py status),
    else the next narration step. Before script.json exists narrate.py reads the sentences from narration.json."""
    nar = run_json([str(S / "narrate.py"), "status", "--run", str(run)], "NARRATE_JSON")
    if nar.get("alignment_current"):   # the next step depends on settings narration.engine (narrate.py status)
        return None
    nxt = nar.get("next", "run")
    if nxt == "tts-plan":
        return {"stage": "narration", "action": "Higgsfield narration: run the command, submit each item (quote one "
                                                "first), `narrate.py tts-ingest` with the job ids, jobs_wait, "
                                                "`tts-ingest` with each job id and url; repeat until no item is left",
                "command": cmd("narrate.py", "tts-plan", "--run", run.as_posix())}
    command = None if nxt == "retake" else cmd("narrate.py", "reading" if nxt == "record" else "run",
                                                 "--run", run.as_posix())
    return {"stage": "narration", "action": nar.get("action") or "render narration", "command": command}


def cast_find_stage(run: Path, story: Path | None) -> dict | None:
    """Setup runs, before the start page: None once RUN/story_cast.json lists the story's characters (checked and
    stamped by runsetup.py cast-check), else the writer agent's pass 0."""
    rsu = load("runsetup")
    if story is None:
        return {"stage": "start", "command": None,
                "action": f"STOP: {run} has no single story file (.md / .txt); start the video with `scribe.py new`"}
    try:
        rsu.story_cast(run, story)
        return None
    except rsu.SetupError as e:
        why = str(e)
    return {"stage": "cast-find",
            "action": f"list the story's recurring characters (writer agent pass 0, {REFS}/SCRIPT-WRITER.md \"Pass 0\"): "
                      f"read {story.as_posix()} and write {(run / rsu.STORY_CAST).as_posix()} as "
                      f"{{\"written_by\", \"characters\": [{{\"id\", \"name\", \"look\"}}]}} (at most "
                      f"{rsu.MAX_CHARACTERS}; [] for none); then run the command",
            "command": cmd("runsetup.py", "cast-check", "--run", run.as_posix(), "--story", str(story)),
            "detail": why}


def start_stage(run: Path) -> dict | None:
    """Setup runs: the start page (gate5.py, Gate 0 on the rail): None once RUN/start.json is valid, else its next
    step. Nothing else happens before it: no writer, no narration."""
    rsu = load("runsetup")
    try:
        rsu.load_start(run)
        return None
    except rsu.SetupError as e:
        why = str(e)
    gate = f"gate{rsu.START_GATE}"
    rs_ = rounds(run, gate)
    r = rs_[-1] if rs_ else 0

    def build(n: int, detail: str = "") -> dict:
        return {"stage": "start", "action": f"build the start page round {n} (Gate 0: the script, keep or rewrite "
                                            f"the words, the narrator voice, the title, the image model, the drawing "
                                            f"style, the colours, each character, the credit budget)",
                "detail": detail,
                "command": cmd("gate5.py", "build", "--run", run.as_posix(), "--round", str(n))}

    rdir = run / "gates" / f"{gate}-r{r}"
    stale = None
    if r and (rdir / "gate.json").is_file() and not (rdir / "changes.json").is_file():
        stale = rsu.start_round_problem(run, read(rdir / "gate.json"))
        if not stale:
            return {"stage": "start", **round_pending(run, gate, r)}
    # the start page asks about the story's characters: a writer lists them first (pass 0)
    c = cast_find_stage(run, rsu.folder_story(run))
    if c:
        return c
    if r == 0:
        return build(1)
    if not (rdir / "gate.json").is_file():
        return build(r, f"round {r} has no page yet")
    if stale:
        return build(r + 1, f"round {r} is superseded: {stale}")
    return build(r + 1, why)


def narration_stage(run: Path, writer_brief: str) -> dict | None:
    """Stages 0-0c: None once narration.json validates, its voice review holds and Gate 0 approved it. Setup runs:
    "keep my words" replaces the voice review with the word-for-word check (in validate_narration), and the narration
    is recorded before the setup page (Gate 1 on the rail), which plays it."""
    vo = load("voice")
    p = run / "narration.json"
    try:
        doc = read(p)
        vo.validate_narration(doc, run if load("runsetup").required() else None)
    except (json.JSONDecodeError, vo.Fail) as e:
        return {"stage": "narration-text", "action": f"fix narration.json (writer agent pass 1, {writer_brief})",
                "command": cmd("voice.py", "validate-narration", "--run", run.as_posix()), "detail": str(e)}
    if not vo.keep_words(run):
        v = voice_stage(run)
        if v:
            return v
    cast_next = None
    if load("runsetup").required():
        a = narration_audio_stage(run)
        if a:
            return a
        sh, _ = sheets_stage(run, load("runsetup").style(run))   # Gate 1 shows the sheets, so they are made first
        if sh:
            return sh
        cast_next = gate1_cast_stage(run)
        if cast_next and cast_next["stage"] == "characters":   # a sheet edit from Gate 1: redraw it first
            return cast_next
    g = gate0_stage(run, vo.fingerprint(vo.fingerprint_parts(doc)))
    return g or cast_next


def script_matches_narration(run: Path, script: dict, writer_brief: str) -> dict | None:
    """None when script.json has the approved narration's fingerprint (same sentence ids and normalized texts, voice
    config and source), else the pass 2 rebuild. Gate 0 already holds, so narration.json is the approved text."""
    vo = load("voice")
    rebuild = {"stage": "script", "command": None,
               "action": f"rebuild script.json from the approved narration (writer agent pass 2, {writer_brief})"}
    try:
        sp = vo.fingerprint_parts(script)
    except vo.Fail as e:
        return {**rebuild, "detail": f"script.json: {e}"}
    nar = read(run / "narration.json")
    np_ = vo.fingerprint_parts(nar)
    if vo.fingerprint(sp) == vo.fingerprint(np_):
        if load("runsetup").required():   # the takes are keyed on the exact text: spaces must match too
            have = [[g["id"], g["text"]] for sc in script.get("scenes", []) for g in sc.get("segments", [])]
            want = [[g["id"], g["text"]] for p in nar.get("paragraphs", []) for g in p.get("sentences", [])]
            if have != want:
                return {**rebuild, "detail": "copy every sentence exactly, spaces included (the recorded takes are "
                                             "tied to the exact text)"}
        return None
    have, want = dict(sp["sentences"]), dict(np_["sentences"])
    diff = [i for i in dict.fromkeys([*want, *have]) if have.get(i) != want.get(i)]
    why = [f"sentences {', '.join(diff[:12])}{' ...' if len(diff) > 12 else ''} differ"] if diff else []
    if not diff and list(have) != list(want):
        why.append("sentence order differs")
    why += [f"{k} differs" for k in ("voice", "source_sha256") if sp[k] != np_[k]]
    return {**rebuild, "detail": "; ".join(why)}


def title_stage(run: Path, script: dict) -> dict | None:
    """None once title/title.json is passed or flagged (ready for Gate 1), else the next title step. script.json
    title_text, when set, is the card's text: a new or changed text runs title.py build (a new round). Without it
    title.json keeps its text."""
    tj = run / "title" / "title.json"
    want = script.get("title_text")
    have = read(tj).get("text") if tj.is_file() else None
    if want and want != have:
        return {"stage": "title", "action": "create the title card state" if have is None else
                f"the title text changed ({have!r} -> {want!r}): start a new title round",
                "command": cmd("title.py", "build", "--run", run.as_posix())}
    if have is None:
        return {"stage": "title", "command": None,
                "action": "set script.json title_text (the title card's words), or run "
                          f"{cmd('title.py', 'build', '--run', run.as_posix(), '--text', '<title>')}"}
    st = run_json([str(S / "title.py"), "status", "--run", str(run)], "TITLE_JSON")
    if st.get("prompt_current") is False:
        return {"stage": "title", "action": "the title prompt changed (style colours in palette.json): start a new "
                                            "title round",
                "command": cmd("title.py", "build", "--run", run.as_posix())}
    if st.get("ready_for_gate1"):
        return None
    nxt = str(st.get("next", ""))
    if nxt.startswith("render") and hf.on("images"):
        return {"stage": "title", "action": "Higgsfield title attempt: run the command, quote and submit its REQUEST_JSON "
                                            "(generate_image), `title.py submitted`, jobs_wait, `title.py ingest`",
                "command": cmd("title.py", "prep", "--run", run.as_posix(), "--agent", "<orchestrator>"), "detail": nxt}
    if nxt.startswith(("submit", "wait for job", "reconcile")):
        return {"stage": "title", "action": f"Higgsfield handoff: {nxt} (title.py submitted / ingest)", "command": None,
                "detail": st.get("status")}
    if nxt.startswith("render"):
        return {"stage": "title", "action": "render a title attempt (render agent; foreground, exit 1 = re-check status)",
                "command": cmd("title.py", "render", "--run", run.as_posix(), "--agent", "<render-agent>"), "detail": nxt}
    if nxt.startswith("review"):
        return {"stage": "title", "action": f"review the title attempt (review agent, {REFS}/TITLE-REVIEW.md), "
                                           "then title.py verdict --file <review_dir>/filled.json",
                "command": cmd("title.py", "review-prep", "--run", run.as_posix()), "detail": nxt}
    if nxt.startswith("exhausted"):
        return {"stage": "title", "action": "STOP: the title ran out of attempts; report to the user", "command": None,
                "detail": nxt}
    return {"stage": "title", "action": f"WAIT: {nxt}", "command": None, "detail": st.get("status")}


def gate1_stage(run: Path, ids: list[int]) -> dict | None:
    """None when every scene (on its current image) and the title are approved, else the next Gate 1 action."""
    rs_ = rounds(run, "gate1")
    r = rs_[-1] if rs_ else 0

    def build(n: int) -> dict:
        return {"stage": "gate1", "action": f"build gate1 round {n}",
                "command": cmd("gate1.py", "build", "--run", run.as_posix(), "--round", str(n))}

    if r == 0:
        return build(1)
    pending = round_pending(run, "gate1", r)
    if pending:
        return {"stage": "gate1", **pending}
    rdir = run / "gates" / f"gate1-r{r}"
    ch = read(rdir / "changes.json")
    title = read(run / "title" / "title.json")
    shas = ch.get("approved_sha256", {})
    stale = [n for n in ids if n in ch.get("approved_scenes", []) and shas.get(str(n)) != gatelib().image_sha(run, n)]
    scenes_ok = set(ids) <= set(ch.get("approved_scenes", [])) and not stale
    if scenes_ok and title.get("approved") is True:
        return None
    if ch.get("scenes") and not (rdir / "edits_done.json").is_file():
        return {"stage": "gate1", "action": f"apply the gate1 round {r} edits, then write {(rdir / 'edits_done.json').as_posix()}",
                "command": None, "detail": f"changes: {(rdir / 'changes.json').as_posix()}"}
    out = build(r + 1)        # edits are in (the earlier stages decided what had to be redone), title-only, or
    if stale:                 # an approved scene's image changed since
        out["detail"] = f"scene image changed since its approval: {', '.join(map(str, stale))}"
    return out


def legacy_video(run: Path) -> dict | None:
    """A run finished before the board gate existed keeps its video: when there is no videos/board_layout.json and the newest Gate 2
    round is an old per-scene round (gate.json without "kind": "board") that approved everything for the current
    final_timeline.json schedule, that video goes straight to Gate 3. Returns the timeline's fingerprints, else
    None."""
    videos = run / "videos"
    rs_ = rounds(run, "gate2")
    if (videos / "board_layout.json").is_file() or not rs_ or not (videos / "final_timeline.json").is_file():
        return None
    rdir = run / "gates" / f"gate2-r{rs_[-1]}"
    if not (rdir / "gate.json").is_file() or not (rdir / "changes.json").is_file():
        return None
    pointer, ch = read(rdir / "gate.json"), read(rdir / "changes.json")
    fp = read(videos / "final_timeline.json").get("fingerprints") or {}
    if pointer.get("kind") is None and ch.get("all_approved") is True and \
            (pointer.get("fingerprints") or {}).get("schedule") == fp.get("schedule"):
        return fp
    return None


def board_stage(run: Path, module: Path) -> tuple[dict | None, str | None]:
    """(None, the layout's fingerprint) when videos/board_layout.json + board.png are current for the run's inputs
    and the style's arrow colours (stitch.layout_current), else (the stitch.py board action, None). Gate 2 and the
    video compare their recorded fingerprint with the returned one."""
    stitch = load("stitch")
    videos = run / "videos"
    board = {"stage": "board", "command": cmd("stitch.py", "board", "--run", run.as_posix())}
    missing = [p.name for p in (videos / "board_layout.json", videos / "board.png") if not p.is_file()]
    if missing:
        return {**board, "action": "build the board (layout + flow arrows -> board.png for Gate 2)",
                "detail": ", ".join(missing) + " missing"}, None
    doc = read(videos / "board_layout.json")
    if doc.get("version") != stitch.LAYOUT_VERSION:
        return {**board, "action": "rebuild the board: board_layout.json is an older version"}, None
    try:
        fp, why = stitch.layout_current(run, read(module), doc)
    except stitch.Fail as e:
        raise SystemExit(f"FAILED board inputs: {e}")
    if fp is None:
        return {**board, "action": "rebuild the board (inputs changed)", "detail": why}, None
    return None, fp


def gate2_stage(run: Path, fp: str, style: str) -> dict | None:
    """Gate 2 (the board): None when the latest board round approved layout fingerprint `fp`, else the next action. Old
    per-scene rounds (gate.json without "kind": "board") are superseded by the next round number."""
    rs_ = rounds(run, "gate2")
    r = rs_[-1] if rs_ else 0

    def build(n: int, why: str = "") -> dict:
        return {"stage": "gate2", "action": f"build gate2 round {n} (the board image)", "detail": why,
                "command": cmd("gate2.py", "build", "--run", run.as_posix(), "--round", str(n))}

    if r == 0:
        return build(1)
    rdir = run / "gates" / f"gate2-r{r}"
    if not (rdir / "gate.json").is_file():
        return build(r, f"round {r} has no page yet")
    pointer = read(rdir / "gate.json")
    if pointer.get("kind") != "board":
        return build(r + 1, f"round {r} is the old per-scene Gate 2; the board gate replaces it")
    if not (rdir / "changes.json").is_file():
        if pointer.get("fingerprint") != fp:
            return build(r + 1, f"round {r} shows an older board; it is superseded")
        return {"stage": "gate2", **round_pending(run, "gate2", r)}
    ch = read(rdir / "changes.json")
    if ch.get("approved") is True:
        if ch.get("approved_fingerprint") == fp:
            return None
        return build(r + 1, f"the board changed since round {r} approved it")
    if pointer.get("fingerprint") != fp:
        return build(r + 1, f"the board was rebuilt after round {r}'s edit")
    return {"stage": "gate2", "command": cmd("stitch.py", "board", "--run", run.as_posix()),
            "action": f"apply the gate2 round {r} edit ({shown(module_path(style))} layout: seed, gap_px, margin_px, "
                      "scale, tilt_deg, arrows.*), then rebuild the board",
            "detail": f"note: {ch.get('note')!r}; changes: {(rdir / 'changes.json').as_posix()}"}


def video_stage(run: Path, ids: list[int], module: Path, fp: str) -> dict | None:
    """None when final.mp4, final_timeline.json (v2, made from layout `fp`) and every Scene-NN.mp4 are current,
    else the stitch.py build action. The images, masks, title and layout values are covered by the layout
    fingerprint; the narration and alignment by mtime; the other module values by comparison."""
    videos = run / "videos"
    tl = videos / "final_timeline.json"
    outs = [videos / "final.mp4", tl, *(videos / f"Scene-{n:02d}.mp4" for n in ids)]
    build = {"stage": "video", "command": cmd("stitch.py", "build", "--run", run.as_posix())}
    missing = [p.name for p in outs if not p.is_file()]
    if missing:
        return {**build, "action": "render the video from the approved board",
                "detail": ", ".join(missing[:8]) + " missing"}
    doc = read(tl)
    if doc.get("version") != 2 or (doc.get("board") or {}).get("layout_fingerprint") != fp:
        return {**build, "action": "render the video from the approved board (the current video is from another "
                                  "layout)"}
    built = min(p.stat().st_mtime for p in outs)
    newer = [p.relative_to(run).as_posix() for p in (run / "alignment.json", run / "narration" / "narration.wav")
             if p.stat().st_mtime > built]
    if newer:
        return {**build, "action": "re-render the video (narration changed)", "detail": ", ".join(newer)}
    stitch = load("stitch")
    mod = read(module)
    used = doc.get("module", {})
    changed = sorted(k for k in (*stitch.rv.MODULE_KEYS, *stitch.STITCH_KEYS, "camera")
                     if k not in used or mod.get(k) != used[k])
    if changed:
        return {**build, "action": "re-render the video (module.json values changed)", "detail": ", ".join(changed)}
    return None


def gate3_stage(run: Path, fp: dict) -> dict | None:
    """Gate 3: None when the latest round approved everything for the current build fingerprint, else the next
    action."""
    gate = "gate3"
    rs_ = rounds(run, gate)
    r = rs_[-1] if rs_ else 0

    def nxt(n: int, why: str = "") -> dict:
        return {"stage": gate, "action": f"build {gate} round {n}", "detail": why,
                "command": cmd(f"{gate}.py", "build", "--run", run.as_posix(), "--round", str(n))}

    if r == 0:
        return nxt(1)
    rdir = run / "gates" / f"{gate}-r{r}"
    pointer = read(rdir / "gate.json") if (rdir / "gate.json").is_file() else {}
    shown = pointer.get("fingerprints") or {}
    if not (rdir / "changes.json").is_file():
        if shown.get("build") != fp["build"]:
            return nxt(r + 1, f"round {r} shows an older build of the board; it is superseded")
        return {"stage": gate, **round_pending(run, gate, r)}
    ch = read(rdir / "changes.json")
    if ch.get("all_approved") is True:
        if shown.get("build") == fp["build"]:
            return None
        return nxt(r + 1, f"the video was rebuilt since round {r} approved it")
    if shown.get("build") != fp["build"]:
        return nxt(r + 1, f"the video was rebuilt after round {r}'s edits")
    edits = ch.get("edits") or []
    pictures = [e for e in edits if e.get("kind") == "picture" and e.get("scene")]
    voice = [e for e in edits if e.get("kind") == "narration"]
    detail = f"changes: {(rdir / 'changes.json').as_posix()}"
    if pictures:   # a picture edit reopens the scene: the scenes stage then routes its render, review and Gate 2
        cmds = [cmd("render_scenes.py", "reopen", "--out", (run / "scenes").as_posix(), "--scene", str(e["scene"]),
                    "--fix", e["note"]) for e in pictures]
        return {"stage": gate, "detail": detail,
                "action": f"reopen scene(s) {', '.join(str(e['scene']) for e in pictures)} with their Gate 3 notes, "
                          f"all of them in this one step (then status routes each new attempt, its review and Gate 2)",
                "command": " && ".join(cmds)}
    if voice:      # a word said wrong: trial spellings the user picks by ear; changed wording: the writer
        notes = "; ".join(f"{e['id']}: {e['note']}" for e in voice)
        return {"stage": gate, "command": None, "detail": detail,
                "action": f"apply the {gate} round {r} narration edits ({notes}). A word said wrong: "
                          f"`narrate.py try-plan` with 2-6 spellings, a pick page, RUN/pronunciations.json, "
                          f"`narrate.py try-adopt` (SKILL.md 'How a word is said'). Changed wording: the writer edits "
                          f"narration.json, which goes back through Gate 1. Then follow status (track, video, a new "
                          f"Gate 3 round)"}
    return {"stage": gate, "command": cmd("stitch.py", "build", "--run", run.as_posix()), "detail": detail,
            "action": f"apply the {gate} round {r} camera edits (camera values in module.json, then re-render the "
                      "video; a layout change goes to module.json layout, then `stitch.py board`"
                      + (")" if load("runsetup").required() else " and the board gate)")}


DEFAULT_STYLE = "rsa_animate"


def _style_field(p: Path) -> str | None:
    """A run file's non-empty `style`, else None (an unreadable or invalid file is reported by its own stage)."""
    try:
        s = json.loads(p.read_text(encoding="utf-8")).get("style") if p.is_file() else None
    except (OSError, ValueError, AttributeError):
        return None
    return s.strip() if isinstance(s, str) and s.strip() else None


def run_style(run: Path, given: str | None) -> tuple[str, str, dict | None]:
    """(style, where it came from, conflict). Once scenes/state.json exists, the style it was initialised with is the
    run's style for everything (the images, character sheets, board and video already use it). Before that: script.json
    `style`, else narration.json `style`, else --style, else DEFAULT_STYLE. `conflict` is a STOP action when the run
    files name another style than state.json, so no work in a second style is ever dispatched."""
    rsu = load("runsetup")
    if rsu.required():   # the style is the start page's answer, nothing else; --style and run files cannot set it
        state = _style_field(run / "scenes" / "state.json")
        try:
            chosen = rsu.style(run)
        except rsu.SetupError:
            return "(chosen on the start page)", "not chosen yet", None
        if state and state != chosen:
            return state, "scenes/state.json", {
                "stage": "style", "command": None,
                "action": f"STOP: the scenes were started in style {state!r}, but the start page (RUN/start.json) "
                          f"chose {chosen!r}. Ask the user: redraw every scene in {chosen!r} (`render_scenes.py init "
                          f"--reset`, which discards every scene image of this run), or start a new video for "
                          f"{state!r}",
                "detail": "nothing else is dispatched until the run names one style"}
        return chosen, "start.json", None
    named = next(((s, n) for n in ("script.json", "narration.json") if (s := _style_field(run / n))), None)
    state = _style_field(run / "scenes" / "state.json")
    if state is None:
        if named:
            return named[0], named[1], None
        return (given, "--style", None) if given else (DEFAULT_STYLE, "default", None)
    if named and named[0] != state:
        return state, "scenes/state.json", {
            "stage": "style", "command": None,
            "action": f"STOP: this run was started in style {state!r} (scenes/state.json), but {named[1]} names "
                      f"{named[0]!r}. Ask the user which one: to keep {state!r}, set `style` in {named[1]} to "
                      f"{state!r}; to draw it in {named[0]!r}, start a new run folder (or `render_scenes.py init "
                      f"--reset` with --style {named[0]}, which discards every scene image of this run)",
            "detail": "nothing else is dispatched until the run names one style"}
    return state, "scenes/state.json", None


def report_stage(run: Path, out: Path, ids: list) -> dict | None:
    """Stage 3 report: render_scenes.py report (contact sheet + run summary) once every scene passed the image gate,
    when scenes/_contact.png or run_summary.md is missing or older than a selected scene image (an interrupted report
    leaves the contact sheet without its summary). Only before the first Gate 1 round: a run already at Gate 1 or
    later is never sent back for it."""
    if rounds(run, "gate1"):
        return None
    newest = max((out / f"scene_{n}_4k.png").stat().st_mtime for n in ids)
    stale = [p.name for p in (out / "_contact.png", out / "run_summary.md")
             if not p.is_file() or p.stat().st_mtime < newest]
    if not stale:
        return None
    return {"stage": "report", "action": "write the contact sheet and run summary of the selected scene images",
            "command": cmd("render_scenes.py", "report", "--out", out.as_posix(), "--no-open"),
            "detail": f"{' and '.join(stale)} missing or older than a selected scene image"}


def status(run: Path, style: str) -> dict:
    rs = load("render_scenes")
    script_p = run / "script.json"
    writer_brief = f"{REFS}/SCRIPT-WRITER.md"
    profile = load("voice").DEFAULT_PROFILE or "(none set: voice_profile in settings.local.json, or voice.profile)"
    setup_on = load("runsetup").required()
    if setup_on:   # the start page comes first: no writer and no narration before it
        s0 = start_stage(run)
        if s0:
            return s0
    narrated = (run / "narration.json").is_file()      # the Gate 0 flow; legacy runs have script.json only
    if narrated:
        n = narration_stage(run, writer_brief)
        if n:
            return n
    if not script_p.is_file():
        if narrated:
            extra = ""
            if setup_on:
                su = load("runsetup").load(run)
                extra = (f"; style {su['style']!r} and exactly the setup's characters "
                         f"({', '.join(su['cast']) or 'none'}), no `reference` (RUN/setup.json)"
                         f"; title_text exactly {load('runsetup').start_title(run)!r} and each character's desc "
                         f"exactly its look in start.json cast")
            return {"stage": "script", "command": None,
                    "action": f"write script.json from the approved narration (writer agent pass 2, {writer_brief}"
                              f"{extra})"}
        if setup_on:
            rsu = load("runsetup")
            keep = rsu.words_mode(run) == "keep"
            return {"stage": "narration-text", "command": None,
                    "action": f"write narration.json from the script {(run / rsu.SOURCE).as_posix()} (writer agent pass "
                              f"1, {writer_brief}; " + ("KEEP THE USER'S WORDS: split into paragraphs and sentences "
                                                        "only, change no word" if keep else
                                                        f"rewrite it as narration, voice profile {profile}") +
                              "; \"characters\" copied exactly from start.json cast (id, name, look)"
                              f"; \"title\" exactly {rsu.start_title(run)!r} (start.json title))"}
        return {"stage": "narration-text", "command": None,
                "action": f"write narration.json from the source story (writer agent pass 1, {writer_brief}, "
                          f"voice profile {profile})"}
    script = read(script_p)
    try:
        scenes = rs.validate(script)
    except rs.Fail as e:
        return {"stage": "script", "action": f"fix script.json (writer agent, {writer_brief})",
                "command": cmd("render_scenes.py", "validate", "--scenes", script_p.as_posix()), "detail": str(e)}
    if not str(script.get("written_by") or "").strip():
        return {"stage": "script", "command": None,
                "action": f"set script.json written_by to the name of the writer agent that wrote it ({writer_brief}); "
                          f"the voice review needs it to refuse the writer as reviewer"}
    ids = [s["id"] for s in scenes]
    if not narrated and load("runsetup").required():
        return {"stage": "script", "command": None,
                "action": "STOP: this run has script.json but no narration.json; with Higgsfield every run starts from "
                          "the narration and its setup page. Start a new run folder from the source story"}
    v = script_matches_narration(run, script, writer_brief) if narrated else voice_stage(run)
    if v:
        return v
    v = script_matches_setup(run, script, writer_brief)
    if v:
        return v

    a = narration_audio_stage(run)
    if a:
        return a

    sh, _ = sheets_stage(run, style)   # setup runs: made before Gate 1, which approved them
    if sh:
        return sh

    out = run / "scenes"
    align = run / "alignment.json"
    if not (out / "state.json").is_file():
        return {"stage": "scenes", "action": "initialise scene images",
                "command": cmd("render_scenes.py", "init", "--scenes", script_p.as_posix(), "--style", style, "--out", out.as_posix(),
                               "--alignment", align.as_posix())}
    st = rs.load_state(out)
    budget = rs.compute_budget(scenes, read(align), rs.load_module(st["style"]))
    stale = st["order"] != ids or any(st["scenes"][str(s["id"])]["scene"] != s or
                                      st["scenes"][str(s["id"])].get("budget") != budget[s["id"]] for s in scenes if str(s["id"]) in st["scenes"])
    try:   # which characters have a sheet shapes the prompts (sheets are optional); a state from before this was
        #    recorded had a sheet for every declared character
        sheeted = sorted(rs.sheeted_ids(run, script.get("characters") or []))
    except rs.Fail as e:
        return {"stage": "scenes", "command": None, "action": f"fix the run's setup: {e}"}
    stale = stale or sheeted != st.get("sheeted", sorted(c["id"] for c in st.get("characters", [])))
    if stale:
        return {"stage": "scenes", "action": "sync scene images with the script and narration",
                "command": cmd("render_scenes.py", "sync", "--out", out.as_posix(), "--scenes", script_p.as_posix(),
                               "--alignment", align.as_posix())}
    if setup_on and (out / "scene_1_4k.png").is_file():
        sty = style_stage(run)
        if sty:
            return sty
    nxt = run_json([str(S / "render_scenes.py"), "status", "--out", str(out)], "NEXT_JSON")
    if not nxt["all_done"]:
        todo = [f"scene {r['scene']}: {r['next']}" for r in nxt["scenes"] if r["next"] != "done"]
        loop = "the Higgsfield render/review loop (render-prep, submit, render-submitted, jobs_wait, render-ingest, " \
               "review)" if hf.on("images") else "the /render-scribe render/review loop"
        return {"stage": "images", "action": f"run {loop}", "command": None, "detail": "; ".join(todo)}
    for n in ids:
        seg = out / f"scene_{n}_segments.json"
        if seg.is_file() and "arrows_mask" not in read(seg):
            base = cmd("render_scenes.py", "rebox", "--out", out.as_posix(), "--scene", str(n), "--agent", "<review-agent>")
            return {"stage": "images", "command": base,
                    "action": f"rebox scene {n}: its boxes predate the connector-arrow split (review agent: run the command, "
                              f"copy the boxes template to <dir>/filled.json and fill it, then the same command with --file <dir>/filled.json)"}
    g = subprocess.run([PY, str(S / "render_scenes.py"), "gate", "--out", str(out)], capture_output=True, text=True, creationflags=NO_WINDOW)
    if g.returncode != 0:
        return {"stage": "images", "action": "fix what the image gate reports", "command": cmd("render_scenes.py", "gate", "--out", out.as_posix()),
                "detail": g.stderr.strip()[-800:]}

    rep = report_stage(run, out, ids)
    if rep:
        return rep

    t = title_stage(run, script)
    if t:
        return t
    g1 = gate1_stage(run, ids)
    if g1:
        return g1

    module = module_path(st["style"])
    # render-scribe: a run whose video passed the old per-scene Gate 2 keeps it (scribe-higgsfield has no Gate 2)
    fp = None if setup_on else legacy_video(run)
    if fp is None:
        b, layout_fp = board_stage(run, module)
        if b:
            return b
        if not setup_on:   # scribe-higgsfield: no board page; the video page (Gate 3) shows the board image too
            g2 = gate2_stage(run, layout_fp, st["style"])
            if g2:
                return g2
        v = video_stage(run, ids, module, layout_fp)
        if v:
            return v
        fp = read(run / "videos" / "final_timeline.json")["fingerprints"]
    g3 = gate3_stage(run, fp)
    if g3:
        return g3
    r = rounds(run, "gate3")[-1]
    g2r = [] if setup_on else rounds(run, "gate2")   # scribe-higgsfield has no board page
    return {"stage": "done", "action": "videos/final.mp4 is approved ("
                                       + (f"board Gate 2 round {g2r[-1]}, " if g2r else "")
                                       + f"video round {r})", "command": None,
            "detail": (run / "videos" / "final.mp4").as_posix()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    s.add_argument("--style", default=None,
                   help=f"style for a new run whose script.json / narration.json name none (default {DEFAULT_STYLE})")
    nw = sub.add_parser("new", help="scribe-higgsfield: start a new video from its story file, chosen in the "
                                    "operating system's file dialog; the video's files go into the story's folder")
    nw.add_argument("--story", default=None, help="the story file, where no dialog can open (no screen)")
    a = ap.parse_args(argv)
    if a.cmd == "new":
        rsu, osl = load("runsetup"), load("oslib")
        if not rsu.required():
            raise SystemExit("FAILED new: only for scribe-higgsfield (Higgsfield on); render-scribe runs start from a "
                             "run folder with the story")
        if a.story:
            story = Path(a.story).expanduser()
        else:
            try:
                story = osl.pick_file(rsu.runs_home(), "Choose the story for the new video (its files go in its "
                                                       "folder)", [("Story", "*.md *.txt"), ("All files", "*.*")])
            except osl.NoDialog as e:
                raise SystemExit(f"FAILED new: the file dialog cannot open here ({e}); ask the user for the story "
                                 f"file's full path on a page or run `scribe.py new --story FILE`")
            if story is None:
                raise SystemExit("FAILED new: no story was chosen (the dialog was cancelled); run `scribe.py new` "
                                 "again when the user is ready")
        story = story.resolve()
        why = rsu.story_problem(story)
        if why:
            raise SystemExit(f"FAILED new: {why}")
        run = story.parent
        if rsu.previous_start(run) or list((run / "gates").glob(f"gate{rsu.START_GATE}-r*")):
            # this story's video exists already (or its start page waits): continue it, never start it again
            print(f"story:  {story}\nrun:    {run.as_posix()}\nThis story's video is already started; continue it "
                  f"with: {cmd('scribe.py', 'status', '--run', run.as_posix())}")
            print("NEXT_JSON " + json.dumps({"stage": "existing", "run": run.as_posix(), "story": str(story),
                                             "action": "continue the existing video (scribe.py status)",
                                             "command": cmd("scribe.py", "status", "--run", run.as_posix())}))
            return 0
        nxt = cast_find_stage(run, story) or {
            "stage": "start", "action": "build the start page (Gate 0); this video's run folder is the story's folder",
            "command": cmd("gate5.py", "build", "--story", str(story), "--round", "1")}
        nxt = {**nxt, "run": run.as_posix(), "story": str(story)}
        print(f"story:  {story}\nrun:    {run.as_posix()}")
        print(f"stage:  {nxt['stage']}\nNEXT:   {nxt['action']}\nrun:    {nxt['command']}")
        print("NEXT_JSON " + json.dumps(nxt))
        return 0
    run = Path(a.run).resolve()
    style, origin, conflict = run_style(run, a.style)
    nxt = conflict or status(run, style)
    nxt["style"] = style
    print(f"style:  {style} ({origin}" + (f"; --style {a.style} ignored" if a.style and a.style != style else "") + ")")
    print(f"stage:  {nxt['stage']}\nNEXT:   {nxt['action']}")
    if nxt.get("command"):
        print(f"run:    {nxt['command']}")
    if nxt.get("detail"):
        print(f"detail: {nxt['detail']}")
    print("NEXT_JSON " + json.dumps(nxt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
