"""Gate 3: review the board video (videos/final.mp4): the whole film and each scene on its own.

  gate3.py build --run RUN --round R [--no-open]
      Generate an html-questions spec in RUN/gates/gate3-rR/: the whole final.mp4 and the board.png overview with an
      'overall' approve/edit question, then one 'scene-N' question per scene with a clip of final.mp4 cut on the
      narration: from 0.5 s before the scene's first word to 0.5 s after its last word, never reaching into a
      neighbour scene's words (scene_clips). The first scene's clip starts at 0 (the title) and the last one runs to
      the end of the video (the zoom-out). The clips are cut into RUN/gates/gate3-rR/clips/ (the page links them).
      Build the page there (one self-contained HTML file, spot 3 on the run's gate rail RUN/gates/gates.js) and open
      it in a new Chrome tab (--no-open skips that). A round that was built but not answered is rebuilt in place.
      Round R > 1 lists the previous round's edits and the camera/layout values that changed since. gate.json
      records final_timeline.json's fingerprints. Prints GATE3_JSON.
  gate3.py apply --run RUN --round R
      Validate the pasted answers (saved at the bundle's expectedAnswers path) with html_questions read
      and write RUN/gates/gate3-rR/changes.json. The round's rail dot turns accepted when every question is
      approved (all_approved), else answered. Prints APPLY_JSON.

changes.json:
  {"round": R, "all_approved": false, "edits": [{"id": "scene-2", "kind": "camera|narration|picture", "scene": 2,
   "note": "..."}],
   "overall_note": "", "notes": "<general notes>", "approved_notes": [{"id": "scene-3", "note": "..."}]}
Every question is re-asked each round: final.mp4 is rebuilt as a whole, so an approved part can change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from gatelib import (DECISION, GATE_POINTER, Fail, build_bundle, decision, emit, gate_dir, load_json, load_script,
                     mark_gate, open_page, read_answers, require_file, response, run_main, span, timecode,
                     unanswered_stem, write_json)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

GATE = "gate3"
SPEECH_PAD_S = 0.5   # a scene clip starts this long before its first word and ends this long after its last
# a scene's answer says what kind of change it asks for, so scribe.py routes it to the right fix
SCENE_OPTIONS = [
    {"value": "approve", "label": "Approve"},
    {"value": "edit-camera", "label": "Edit the camera or timing (framing, speed, pauses, drawing order)"},
    {"value": "edit-narration", "label": "Edit the narration (a word said wrong, wording, pacing of the voice)"},
    {"value": "edit-picture", "label": "Edit the picture (redraw something in this scene's drawing)"},
]
EDIT_KINDS = {"edit-camera": "camera", "edit-narration": "narration", "edit-picture": "picture"}


def num(obj: dict, key: str, what: str) -> float:
    if not isinstance(obj.get(key), (int, float)):
        raise Fail(f"final_timeline.json {what} lacks number {key}")
    return float(obj[key])


def load_timeline(run: Path, script: dict) -> dict:
    tl = load_json(run / "videos" / "final_timeline.json", "final_timeline.json (run stitch.py build first)")
    if tl.get("version") != 2:
        raise Fail(f"final_timeline.json version {tl.get('version')!r}, expected 2 (rebuild with stitch.py build)")
    duration = num(tl, "duration_s", "top level")
    for key in ("pieces", "scenes", "moves"):
        if not isinstance(tl.get(key), list):
            raise Fail(f"final_timeline.json lacks list {key}")
    for key in ("ending", "camera", "layout", "board", "fingerprints"):
        if not isinstance(tl.get(key), dict):
            raise Fail(f"final_timeline.json lacks object {key}")
    ids = [sc["id"] for sc in script["scenes"]]
    got = [sc.get("scene") for sc in tl["scenes"]]
    if got != ids:
        raise Fail(f"final_timeline.json scenes {got} do not match script.json scenes {ids}")
    scene_of = {p["id"]: p.get("scene") for p in tl["pieces"]}
    for mv in tl["moves"]:
        if mv.get("from") not in scene_of or mv.get("to") not in scene_of:
            raise Fail(f"final_timeline.json move {mv.get('from')}->{mv.get('to')} names an unknown piece")
        for key in ("start_s", "handoff_s", "end_s"):
            num(mv, key, f"move {mv['from']}->{mv['to']}")
        if not 0 <= mv["start_s"] <= mv["handoff_s"] <= mv["end_s"] <= duration:
            raise Fail(f"final_timeline.json move {mv['from']}->{mv['to']} times are not ordered within 0..{duration}")
    end = tl["ending"]
    for key in ("zoom_start_s", "zoom_end_s", "end_s"):
        num(end, key, "ending")
    if not 0 <= end["zoom_start_s"] < end["zoom_end_s"] <= end["end_s"] <= duration + 1e-3:
        raise Fail(f"final_timeline.json ending {end} is not ordered within 0..{duration}")
    return tl


def rerecorded(run: Path) -> set[str]:
    """Sentences whose audio differs from what the newest approved Gate 1 round played (e.g. a pronunciation fixed
    after Gate 1): the video page names them, so the user listens to them again."""
    import narrate
    gdir = run / "gates"
    rounds = sorted((int(p.name.split("-r")[1]), p) for p in gdir.glob("gate0-r*") if p.name.split("-r")[1].isdigit()) \
        if gdir.is_dir() else []
    for _, p in reversed(rounds):
        if (p / "changes.json").is_file() and (p / GATE_POINTER).is_file():
            ch = load_json(p / "changes.json", "gate0 changes.json")
            if not ch.get("approved_fingerprint"):
                continue
            played = load_json(p / GATE_POINTER, "gate0 gate pointer").get("audio_sha256") or {}
            return {gid for gid, sha in played.items()
                    if (w := run / "narration" / f"segment_{gid}.wav").is_file() and narrate.file_sha(w) != sha}
    return set()


def scene_clips(tl: dict, duration: float) -> list[dict]:
    """One clip per scene, cut on the narration: SPEECH_PAD_S before the scene's first word to SPEECH_PAD_S after its
    last, but never into a neighbour scene's words. The first clip starts at 0 (the title), the last ends at
    `duration` (the zoom-out)."""
    scenes = tl["scenes"]
    speech = []
    for sc in scenes:
        what = f"scene {sc.get('scene')}"
        first, last = num(sc, "first_speech_s", what), num(sc, "last_speech_end_s", what)
        if not 0 <= first < last <= duration + 1e-3:
            raise Fail(f"final_timeline.json {what} speech {first}..{last} is not ordered within 0..{duration}")
        if speech and first < speech[-1][1]:
            raise Fail(f"final_timeline.json {what} speech starts at {first} before the previous scene's ends at "
                       f"{speech[-1][1]}")
        speech.append((first, last))
    clips = []
    for i, (sc, (first, last)) in enumerate(zip(scenes, speech)):
        lo = 0.0 if i == 0 else max(speech[i - 1][1], first - SPEECH_PAD_S)
        hi = duration if i == len(scenes) - 1 else min(speech[i + 1][0], last + SPEECH_PAD_S)
        clips.append({"scene": sc["scene"], "lo": round(lo, 3), "hi": round(min(hi, duration), 3),
                      "first_speech_s": first, "last_speech_end_s": last})
    return clips


def probe_duration(video: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"ffprobe could not read {video}: {p.stderr.strip()[-400:]}")
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise Fail(f"ffprobe gave no duration for {video}: {p.stdout.strip()[:300]}") from exc


def cut_clip(video: Path, start: float, end: float, out: Path) -> Path:
    """Frame-accurate re-encoded excerpt, same encode spec as final.mp4."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
           "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end - start:.3f}",
           "-map", "0:v:0", "-map", "0:a:0",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode != 0 or not out.is_file():
        raise Fail(f"ffmpeg could not cut {out.name} ({start:.3f}-{end:.3f} s): {p.stderr.strip()[-400:]}")
    return out


def label(qid: str) -> str:
    if qid.startswith("scene-"):
        return f"Scene {qid.split('-', 1)[1]}"
    return {"overall": "The whole video"}.get(qid, qid)


def since_last_round(run: Path, rnd: int, camera: dict, layout: dict) -> str:
    """Round R > 1: the previous round's edits, and what the rebuild changed."""
    if rnd == 1:
        return ""
    prev = gate_dir(run, GATE, rnd - 1)
    pointer = load_json(prev / GATE_POINTER, f"gate3 round {rnd - 1} gate pointer")
    if (prev / "changes.json").is_file():
        changes = load_json(prev / "changes.json", f"gate3 round {rnd - 1} changes.json")
        head = f"**Since round {rnd - 1}**, edits asked for:"
        asked = "\n".join(f"- {label(e['id'])}: {e['note'] or '(no note)'}"
                          for e in changes.get("edits", [])) or "- none (the video was rebuilt anyway)"
    else:
        head, asked = f"**Round {rnd - 1}** was superseded by a rebuild before it was answered.", ""
        pasted = sorted(prev.glob("*.answers.json"))
        if pasted:   # copied but not applied (e.g. a required question left blank): still show what was asked
            raw = load_json(pasted[-1], f"gate3 round {rnd - 1} pasted answers")
            notes = [a for a in raw.get("answers", []) if isinstance(a, dict) and a.get("kind") == "note"
                     and str(a.get("value", "")).strip()]
            head = f"**Round {rnd - 1}**'s answers were not applied; the video was rebuilt for its notes:"
            asked = "\n".join(f"- {str(a.get('title', '')).split(' · ')[0]}: {a['value']}" for a in notes) or "- none"
    diffs = []
    for name, old, new in (("camera", pointer.get("camera", {}), camera), ("layout", pointer.get("layout", {}), layout)):
        diffs += [f"- {name} {k}: {old.get(k)!r} → {new.get(k)!r}" for k in sorted(set(old) | set(new))
                  if old.get(k) != new.get(k)]
    return "\n\n".join(x for x in [head, asked, "Camera/layout values changed:", "\n".join(diffs) or "- none"] if x)


def build_spec(run: Path, rnd: int, script: dict, cut_dir: Path, clips: Path) -> tuple[dict, dict]:
    """The page. Clips are cut into `cut_dir` (staging) and linked at `clips` (where cmd_build moves them)."""
    tl = load_timeline(run, script)
    video = require_file(run / "videos" / "final.mp4", "final.mp4 (run stitch.py build first)")
    board = require_file(run / "videos" / "board.png", "board.png (run stitch.py board first)")
    duration = float(tl["duration_s"])
    real = probe_duration(video)
    if abs(real - duration) > 0.1:
        raise Fail(f"final.mp4 is {real:.3f} s but final_timeline.json says {duration:.3f} s; rebuild with stitch.py")
    by_id = {sc["id"]: sc for sc in script["scenes"]}
    n_pieces = len(tl["pieces"])
    n_shots = len(tl.get("shots") or [])
    shots = (f"{n_shots - 1} camera shots (the frame holds still while a shot's segments draw), "
             f"{len(tl['moves'])} glides" if n_shots else f"{len(tl['moves'])} camera moves")

    questions = [{
        "id": "overall", "type": "single", "prompt": "The whole video", "required": True, "allowNote": True,
        "options": DECISION,
        "body": f"`final.mp4` · **{timecode(duration)}** · the title + {n_pieces - 1} segments on one board "
                f"{tl['board'].get('width')}×{tl['board'].get('height')} (layout seed {tl['board'].get('seed')}) · "
                f"{shots} · {len(tl.get('pauses', []))} narration pause(s) inserted.\n\n"
                "Watch it end to end. The board image below is every piece in its place.",
        "media": [{"type": "video", "path": str(video), "alt": "final.mp4"},
                  {"type": "image", "path": str(board), "alt": "The whole board"}],
        "notePlaceholder": "What should change overall (pacing, layout, framing)?",
    }]
    asked = []
    again = rerecorded(run)
    cuts = scene_clips(tl, duration)
    for i, c in enumerate(cuts):
        n, qid = c["scene"], f"scene-{c['scene']}"
        sc = by_id[n]
        clip = clips / cut_clip(video, c["lo"], c["hi"], cut_dir / f"{qid}.mp4").name
        where = [f"Clip {span(c['lo'], c['hi'])} of final.mp4: the scene's narration "
                 f"{span(c['first_speech_s'], c['last_speech_end_s'])} with a moment of quiet each side, so every "
                 "sentence is whole."]
        if i == 0:
            where.append(f"It opens on the title (drawn in a {tl['lead_in_s']:.2f} s silent lead-in).")
        if i == len(cuts) - 1:
            end = tl["ending"]
            where.append(f"It ends with the zoom-out to the whole board {span(end['zoom_start_s'], end['zoom_end_s'])} "
                         f"and the hold until {timecode(end['end_s'])}.")
        words = "\n".join(f"- “{seg['text']}”" + (" **(recorded again since Gate 1: listen to it)**"
                                                   if seg["id"] in again else "")
                          for seg in sc.get("segments", []))
        questions.append({
            "id": qid, "type": "single", "prompt": f"Scene {n}: {sc.get('title', '?')}", "required": True,
            "allowNote": True, "options": SCENE_OPTIONS,
            "body": f"{' '.join(where)}\n\n{words}",
            "media": [{"type": "video", "path": str(clip), "alt": f"Scene {n}"}],
            "notePlaceholder": f"What should change in scene {n} (pacing, framing, drawing order, camera)?",
        })
        asked.append({"id": qid, "clip_s": [c["lo"], c["hi"]]})

    intro = (f"Round {rnd}. Watch the whole board video, then approve or edit each scene on its own. Every question "
             "takes an optional note.")
    changed = since_last_round(run, rnd, tl["camera"], tl["layout"])
    if changed:
        intro += "\n\n" + changed
    spec = {"title": f"Gate 3 · {script.get('title', 'Scribeboard')} · round {rnd}", "slug": f"gate3-r{rnd}",
            "stageLabel": "Gate 3", "intro": intro, "questions": questions}
    meta = {"round": rnd, "questions": ["overall", *(q["id"] for q in asked)], "clips": asked,
            "camera": tl["camera"], "layout": tl["layout"], "seed": tl["board"].get("seed"), "duration_s": duration,
            "fingerprints": tl["fingerprints"]}
    return spec, meta


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    script = load_script(run)
    out = gate_dir(run, GATE, a.round)
    unanswered_stem(out)   # refuse an applied or answered round before cutting anything into it
    clips = out / "clips"   # the page links the clips where they are, so they live in the round folder
    staged, previous = out / "clips.new", out / "clips.old"
    if previous.is_dir():   # an interrupted swap: the live page's clips are in clips.old unless clips/ exists
        if clips.is_dir():
            shutil.rmtree(previous)
        else:
            previous.rename(clips)
    if staged.is_dir():   # an interrupted cut
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    try:
        spec, meta = build_spec(run, a.round, script, staged, clips)
        if clips.is_dir():   # a rebuilt round: keep its old clips until the new page is built
            clips.rename(previous)
        try:
            staged.rename(clips)
            result = build_bundle(spec, out, meta)
        except BaseException:
            if clips.is_dir():
                shutil.rmtree(clips)
            if previous.is_dir():
                previous.rename(clips)
            raise
        if previous.is_dir():
            shutil.rmtree(previous)
    finally:
        if staged.is_dir():
            shutil.rmtree(staged)
    info = {"round": a.round, "html": result["html"], "url": result["url"], "bundle": result["bundle"],
            "expectedAnswers": result["expectedAnswers"], "questions": meta["questions"],
            "duration_s": meta["duration_s"], "rebuilt": result["rebuilt"], **open_page(result["url"], a.no_open)}
    emit("GATE3", info)
    return 0


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    responses, pointer = read_answers(out)
    edits, approved_notes, overall_note = [], [], ""
    for qid in pointer["questions"]:
        if qid == "overall":
            action, note = decision(responses, qid)
            overall_note = note
            kind = "camera"
        else:
            r = response(responses, qid)
            action, note = r.get("value"), r.get("note", "")
            if action not in ("approve", *EDIT_KINDS):
                raise Fail(f"question {qid} has no approve/edit answer: {action!r}")
            kind = EDIT_KINDS.get(action)
        if action != "approve":
            if not note.strip():
                raise Fail(f"{qid} asks for an edit without a note; the note says what to change. Add it to the "
                           f"answers and apply again")
            edits.append({"id": qid, "kind": kind, "note": note,
                          **({"scene": int(qid.split("-", 1)[1])} if qid.startswith("scene-") else {})})
        elif note and qid != "overall":  # the overall note is kept in overall_note
            approved_notes.append({"id": qid, "note": note})
    changes = {"round": a.round, "all_approved": not edits, "edits": edits, "overall_note": overall_note,
               "notes": response(responses, "notes")["value"], "approved_notes": approved_notes}
    write_json(out / "changes.json", changes)
    state = mark_gate(out, pointer, changes["all_approved"])
    emit("APPLY", {"round": a.round, "changes": str(out / "changes.json"), "all_approved": changes["all_approved"],
                   "edits": [e["id"] for e in edits], "reviewed": len(pointer["questions"]), "gate_state": state})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--run", required=True)
    b.add_argument("--round", type=int, required=True)
    b.add_argument("--no-open", action="store_true", help="do not open the page in Chrome")
    p = sub.add_parser("apply")
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    a = ap.parse_args(argv)
    return {"build": cmd_build, "apply": cmd_apply}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(run_main(main))
