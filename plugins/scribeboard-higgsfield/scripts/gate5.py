"""Gate 0 of a scribe-higgsfield run: the start page, the first page of a video (folder gates/gate5-rN).

A video starts from its story file, chosen in the operating system's own file dialog (`scribe.py new`). Every file
the video makes goes into the story's own folder: that folder is the run folder. Before the page is built, a writer
agent reads the story and lists its recurring characters (pass 0, RUN/story_cast.json, checked and stamped by
`runsetup.py cast-check`); `build` refuses without it. The page names the story and asks:
  - the user's words: keep them exactly (the writer only splits them into paragraphs and sentences) or rewrite them
    as narration (the writer retells the story; a voice reviewer checks it);
  - the narrator voice (the catalog's voices, each with its free sample clip);
  - the image model (image-models/higgsfield.json);
  - each character: a sheet drawn from the description, a sheet from 1-3 image files (with the consent box), or no
    sheet;
  - the run's credit budget (with a rough estimate by story length and model; Gate 1 shows the exact one).
Only with setup required (runsetup.required(): Higgsfield on). Nothing is written or spoken before it is answered:
scribe.py routes nothing else, and every narration take refuses without RUN/start.json (runsetup.require_start).

  gate5.py build (--story FILE | --run RUN) --round R [--no-open]
      --story: the story file (the run folder is its folder). --run: a run folder whose story the start page already
      recorded, or that holds exactly one .md / .txt file.
  gate5.py apply --run RUN --round R
      Checks every answer (runsetup.start_from_answers): a problem (budget not a number, the story missing or
      changed) is refused with the reasons and nothing is written. Valid answers write changes.json, then
      RUN/source.md (a copy of the story, the text the run works from) and RUN/start.json. Prints APPLY_JSON.
Once RUN/start.json is valid the start does not change: a new story, voice or folder is a new video.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import runsetup
from gatelib import (GATE_POINTER, Fail, build_bundle, emit, gate_dir, load_json, mark_gate, open_page, read_answers,
                     run_main, unanswered_stem, write_json)

GATE = f"gate{runsetup.START_GATE}"


def resolve_story(a) -> tuple[Path, Path]:
    """(run folder, story file) from --story or --run."""
    if getattr(a, "story", None):
        story = Path(a.story).expanduser().resolve()
        return story.parent, story
    run = Path(a.run).resolve()
    story = runsetup.folder_story(run)
    if story is None:
        raise Fail(f"{run} has no story this page can name (one .md or .txt file, or one the start page recorded); "
                   f"start the video with `scribe.py new`, which asks for the story file")
    return run, story


def cmd_build(a) -> int:
    if not runsetup.required():
        raise Fail("the start page is for scribe-higgsfield runs (Higgsfield on)")
    run, story = resolve_story(a)
    why = runsetup.story_problem(story)
    if why:
        raise Fail(why)
    try:
        runsetup.load_start(run)
        raise Fail("the start of this video is set (story, words, voice); it does not change. A new story, voice or "
                   "folder is a new video; the budget is raised with `hf.py cap` (only with the user's answer)")
    except runsetup.SetupError:
        pass
    try:   # the cast finder (writer pass 0) listed the story's characters first: the page asks about each one
        chars = runsetup.story_cast(run, story)
    except runsetup.SetupError as e:
        raise Fail(f"{e} (follow `scribe.py status`: the characters are listed before the start page)") from e
    out = gate_dir(run, GATE, a.round)
    unanswered_stem(out)
    try:
        questions = runsetup.start_questions(run, runsetup.previous_start(run),
                                             story.read_text(encoding="utf-8-sig"), chars)
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    who = (", ".join(c["name"] for c in chars) if chars else "none")
    spec = {"title": f"Gate 0 · Start · round {a.round}", "slug": f"gate5-r{a.round}", "stageLabel": "Gate 0",
            "intro": (f"A new whiteboard video from **{story.name}**.\n\nStory file: `{story}`\n\nEvery file of this "
                      f"video goes into the story's folder: `{run}`\n\nWhen this page is answered the video keeps its "
                      f"own copy of the story (`{runsetup.SOURCE}` in that folder); later edits to the story file are "
                      f"not used.\n\nChoose whether to keep the story's words, pick the narrator voice, give the "
                      f"video its title, pick the image model, the drawing style and the colours, say how each "
                      f"character is drawn, and set the credit budget. A writer has read the story and found these "
                      f"recurring characters: **{who}**. Nothing is written, spoken or paid for until this page is "
                      f"answered. Next, the story is split into paragraphs (one per scene) and recorded in your "
                      f"voice, and each character sheet is drawn in your style and colours; Gate 1 then plays the "
                      f"narration and shows the sheets for you to approve."),
            "questions": questions}
    result = build_bundle(spec, out, {"round": a.round, "kind": "start", "story": str(story),
                                      **runsetup.start_round_meta(story, chars)})
    emit("GATE5", {"round": a.round, "run": run.as_posix(), "story": str(story), "html": result["html"],
                   "url": result["url"], "bundle": result["bundle"], "expectedAnswers": result["expectedAnswers"],
                   "rebuilt": result["rebuilt"], **open_page(result["url"], a.no_open)})
    return 0


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    try:
        runsetup.load_start(run)
        raise Fail("the start of this video is set (story, words, voice); it does not change. A new story, voice or "
                   "keep-or-rewrite choice is a new video; the budget is raised with `hf.py cap` (only with the "
                   "user's answer)")
    except runsetup.SetupError:
        pass
    out = gate_dir(run, GATE, a.round)
    built = load_json(out / GATE_POINTER, "gate pointer (run build first)")
    if not built.get("story"):
        raise Fail(f"start page round {a.round} names no story; build the next round")
    stale = runsetup.start_round_problem(run, built)   # before reading answers: they cannot be applied anyway
    if stale:
        raise Fail(f"start page round {a.round} cannot be applied: {stale}. Follow scribe.py status (it lists the "
                   f"characters again if needed, then builds round {a.round + 1}) and ask the user to answer that one")
    responses, pointer = read_answers(out)
    doc, text, problems = runsetup.start_from_answers(run, responses, a.round, Path(pointer["story"]))
    if problems:
        raise Fail("start answers: " + "; ".join(problems) + ". Ask the user to correct them on the page (never "
                   "guess an answer), paste the new answers and apply again")
    changes = {"round": a.round, "approved": True, "start": doc}
    write_json(out / "changes.json", changes)
    runsetup.write_start(run, doc, text)          # after changes.json: start.json must match an approved round
    state = mark_gate(out, pointer, True)
    emit("APPLY", {"round": a.round, "approved": True, "run": run.as_posix(), "story": doc["story"],
                   "words": doc["words"], "voice": doc["voice"], "title": doc["title"], "style": doc["style"],
                   "palette": doc["palette"], "image_model": doc["image_model"],
                   "cast": {k: v["mode"] for k, v in doc["cast"].items()}, "budget": doc["budget"]["cap"],
                   "story_chars": len(text), "gate_state": state})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    where = b.add_mutually_exclusive_group(required=True)
    where.add_argument("--story")
    where.add_argument("--run")
    b.add_argument("--round", type=int, required=True)
    b.add_argument("--no-open", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    a = ap.parse_args(argv)
    return {"build": cmd_build, "apply": cmd_apply}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(run_main(main))
