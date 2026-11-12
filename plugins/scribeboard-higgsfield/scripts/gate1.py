"""Gate 1: review the title, the script, the narration, every scene image and every segment highlight.

  gate1.py build --run RUN --round R [--no-open]
      Generate an html-questions spec in RUN/gates/gate1-rR/, build the page there (one self-contained HTML file,
      spot 1 on the run's gate rail RUN/gates/gates.js) and open it in a new Chrome tab (--no-open skips that).
      A round that was built but not answered is rebuilt in place. Scenes approved in an earlier round are
      listed collapsed ("approved in round r") with no question. When RUN/title/title.json exists the page opens
      with the `title` question (title/title_4k.png, approve/edit + note) unless that exact image was approved
      already; the title must be passed or flagged by title.py first. When voice/voice.json flagged the
      current script (voice.py: three failed voice reviews in a row), the `script` question opens with the
      open flags. Prints GATE1_JSON.
  gate1.py apply --run RUN --round R
      Validate the pasted answers (saved at the bundle's expectedAnswers path) with html_questions read
      and write RUN/gates/gate1-rR/changes.json. Prints APPLY_JSON. A title `approve` is recorded in
      title.json (approved, approved_round, approved_sha256); a title `edit` reopens the title with its note
      as the next attempt's fix (title.py reopen: new round, approval dropped). The round's rail dot turns
      accepted when nothing is left to edit (no scene edited and the title approved), else answered.

changes.json (contract `Gates`): approved_scenes is cumulative (every scene approved in this or an
earlier round whose image is unchanged); approved_sha256 maps each of them (str id) to the sha256 of the
scenes/scene_N_4k.png it was approved on (recorded when the page was built), so a re-rendered image is
unapproved while a rebox/regions rebuild (same image, new masks) keeps its approval. scenes lists this
round's edited scenes (a scene is edit if it or any of its segments is edit) with every segment's action
and note. Extra keys: notes (the page's general Notes box), approved_scene_notes (notes left on
scenes/segments that were nevertheless approved), title ({"action": "approve"|"edit"|"approved_earlier"|
"none", "note"}) and title_approved (true when there is no title or title.json is approved). Gate 1 passes
when approved_scenes covers every scene with its current image sha and title_approved is true.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import title as title_mod
import voice as voice_mod
from gatelib import (DECISION, Fail, approved_intro, build_bundle, decision, earlier_approvals, emit, gate_dir,
                     image_sha, load_json, load_script, mark_gate, open_page, paragraph, read_answers, require_file,
                     response, run_main, span, timecode, write_json)

GATE = "gate1"


def title_state(run: Path) -> dict | None:
    """None without RUN/title/title.json; else what the page needs: {doc, image, sha256, approved}."""
    if not (run / "title" / "title.json").is_file():
        return None
    doc = title_mod.load(run)
    status = title_mod.status_of(doc)
    if status not in ("passed", "flagged"):
        raise Fail(f"the title is '{status}' (next: {title_mod.next_action(doc)}); finish the title.py render/review "
                   f"loop before Gate 1")
    sel = title_mod.selected(doc)
    image = require_file(run / "title" / "title_4k.png", "title image")
    return {"doc": doc, "image": image, "sha256": sel["sha256"], "score": (sel.get("verdict") or {}).get("score"),
            "approved": bool(doc.get("approved")) and doc.get("approved_sha256") == sel["sha256"]}


def title_question(t: dict) -> dict:
    doc = t["doc"]
    body = (f"Hand-lettered title **\u201c{doc['text']}\u201d**. The video opens on it: it is drawn first, then the "
            f"camera moves on to scene 1.")
    if doc.get("flagged"):
        body += (f"\n\n**Flagged:** no attempt passed review in five tries; this is the best one (score {t['score']}). "
                 f"Check the spelling.")
    return {"id": "title", "type": "single", "prompt": "Title", "required": True, "allowNote": True,
            "options": DECISION, "body": body,
            "media": [{"type": "image", "path": str(t["image"]), "alt": f"Title: {doc['text']}"}],
            "notePlaceholder": "What should change in the title (lettering, spelling, colour, flourish)?"}


def seg_qid(seg_id: str) -> str:
    return "segment-" + seg_id.replace(".", "-")


def load_alignment(run: Path, script: dict) -> dict:
    al = load_json(run / "alignment.json", "alignment.json")
    by_id = {s["id"]: s for s in al.get("segments", [])}
    for scene in script["scenes"]:
        for seg in scene["segments"]:
            a = by_id.get(seg["id"])
            if a is None:
                raise Fail(f"alignment.json has no segment {seg['id']}; run narrate first")
            if a.get("text") != seg["text"]:
                raise Fail(f"alignment.json segment {seg['id']} text differs from script.json; re-run narrate")
    scene_ids = {s["id"] for s in al.get("scenes", [])}
    missing = [sc["id"] for sc in script["scenes"] if sc["id"] not in scene_ids]
    if missing:
        raise Fail(f"alignment.json lacks scenes {missing}")
    if not isinstance(al.get("duration_s"), (int, float)):
        raise Fail("alignment.json lacks duration_s")
    return al


def load_segments(run: Path, scene: dict) -> list[dict]:
    """scene_N_segments.json entries in script order, each with an absolute highlight path."""
    scenes_dir = run / "scenes"
    doc = load_json(scenes_dir / f"scene_{scene['id']}_segments.json", f"scene {scene['id']} segments json")
    entries = doc.get("segments", [])
    want = [seg["id"] for seg in scene["segments"]]
    got = [e.get("id") for e in entries]
    if got != want:
        raise Fail(f"scene_{scene['id']}_segments.json segments {got} do not match script.json {want}")
    for seg, entry in zip(scene["segments"], entries):
        ids = sorted(el["id"] for el in entry.get("elements", []))
        if ids != sorted(seg["reveals"]):
            raise Fail(f"segment {seg['id']}: segments json elements {ids} != script reveals {sorted(seg['reveals'])}")
        if not entry.get("highlight"):
            raise Fail(f"segment {seg['id']} has no highlight image in scene_{scene['id']}_segments.json")
        entry["_highlight"] = require_file(scenes_dir / entry["highlight"], f"segment {seg['id']} highlight")
    return entries


def build_spec(run: Path, rnd: int, script: dict, al: dict, approved: dict[int, int],
               title: dict | None) -> tuple[dict, dict]:
    audio = require_file(run / "narration" / "narration.mp3", "narration.mp3")
    al_seg = {s["id"]: s for s in al["segments"]}
    al_scene = {s["id"]: s for s in al["scenes"]}
    total = float(al["duration_s"])

    script_body = "\n\n".join(f"**Scene {sc['id']}: {sc['title']}**\n\n{paragraph(sc)}" for sc in script["scenes"])
    try:
        voice_note = voice_mod.gate_note(run)
    except voice_mod.Fail as e:
        raise Fail(f"voice: {e}") from e
    if voice_note:
        script_body = voice_note + "\n\n" + script_body
    starts = " · ".join(f"Scene {sc['id']} at {timecode(al_scene[sc['id']]['start_s'])}" for sc in script["scenes"])
    ask_title = title is not None and not title["approved"]
    questions = [title_question(title)] if ask_title else []
    questions += [
        {"id": "script", "type": "text", "prompt": "Full narration script", "body": script_body,
         "placeholder": "Notes on the whole script (wording, order, tone)"},
        {"id": "narration", "type": "text", "prompt": "Narration",
         "body": f"Total duration **{timecode(total)}** ({total:.2f} s). {starts}.",
         "media": [{"type": "audio", "path": str(audio), "alt": "Narration"}],
         "placeholder": "Notes on the narration (voice, pacing, pronunciation)"},
    ]
    chars = script.get("characters", [])
    sheeted = title_mod.rs().sheeted_ids(run, chars)   # sheets are optional (the setup's answer per character)
    sheets = [(c, run / "characters" / f"{c['id']}_sheet.png") for c in chars if c["id"] in sheeted]
    described = [c for c in chars if c["id"] not in sheeted]
    if chars:
        body = ""
        if sheets:
            body += ("Every scene that shows these characters is drawn with these sheets attached as references. "
                     + " ".join(f"**{c['name']}:** {c['desc']}." for c, _ in sheets))
        if described:
            body += ((" " if body else "") + "Without a character sheet (each scene draws them from the description, "
                     "so the look may vary): " + " ".join(f"**{c['name']}:** {c['desc']}." for c in described))
        q = {"id": "characters", "type": "text", "prompt": "Characters" if described else "Character sheets",
             "body": body, "placeholder": "Notes on the characters (likeness, outfit, proportions)"}
        if sheets:
            q["media"] = [{"type": "image", "path": str(require_file(p, f"{c['id']} character sheet")),
                           "alt": f"{c['name']} character sheet"} for c, p in sheets]
        questions.append(q)
    meta_scenes = []
    for sc in script["scenes"]:
        if sc["id"] in approved:
            continue
        image = require_file(run / "scenes" / f"scene_{sc['id']}_4k.png", f"scene {sc['id']} image")
        entries = load_segments(run, sc)
        a = al_scene[sc["id"]]
        questions.append({
            "id": f"scene-{sc['id']}", "type": "single", "prompt": f"Scene {sc['id']}: {sc['title']}",
            "required": True, "allowNote": True, "options": DECISION,
            "body": f"> {paragraph(sc)}\n\nScene time **{span(a['start_s'], a['end_s'])}**",
            "media": [{"type": "image", "path": str(image), "alt": f"Scene {sc['id']}"}],
            "notePlaceholder": f"What should change in scene {sc['id']} (image or paragraph)?",
        })
        elements = {el["id"]: el for el in sc.get("elements", [])}
        for seg, entry in zip(sc["segments"], entries):
            s = al_seg[seg["id"]]
            reveals = "; ".join(f"`{eid}` — {elements[eid]['desc']}" if eid in elements else f"`{eid}`"
                                for eid in seg["reveals"])
            questions.append({
                "id": seg_qid(seg["id"]), "type": "single", "prompt": f"Segment {seg['id']}",
                "required": True, "allowNote": True, "options": DECISION,
                "body": f"> {seg['text']}\n\nTime **{span(s['start_s'], s['end_s'])}**\n\nReveals: {reveals}",
                "media": [{"type": "image", "path": str(entry["_highlight"]), "alt": f"Segment {seg['id']} highlight"}],
                "notePlaceholder": f"What should change in segment {seg['id']} (sentence or highlighted part)?",
            })
        meta_scenes.append({"id": sc["id"], "segments": [seg["id"] for seg in sc["segments"]]})
    if not meta_scenes and not ask_title:
        raise Fail(f"every scene{' and the title' if title else ''} was approved before round {rnd}; gate 1 is complete")

    intro = f"Round {rnd}. Approve or edit {'the title, ' if ask_title else ''}every scene and every segment; " \
            f"use the notes for what must change."
    collapsed = approved_intro(script["scenes"], approved)
    if title is not None and not ask_title:
        collapsed = (f"Title \u201c{title['doc']['text']}\u201d — approved in round {title['doc']['approved_round']}\n"
                     + collapsed).strip()
    if collapsed:
        intro += "\n\n" + collapsed
    spec = {"title": f"Gate 1 · {script.get('title', 'Scribeboard')} · round {rnd}", "slug": f"gate1-r{rnd}",
            "stageLabel": "Gate 1", "intro": intro, "questions": questions}
    meta_title = None if title is None else {"asked": ask_title, "sha256": title["sha256"]}
    return spec, {"round": rnd, "scenes": meta_scenes, "earlier_approved": sorted(approved), "title": meta_title,
                  "scene_sha256": {str(sc["id"]): image_sha(run, sc["id"]) for sc in script["scenes"]}}


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    script = load_script(run)
    al = load_alignment(run, script)
    approved = earlier_approvals(run, GATE, a.round)
    out = gate_dir(run, GATE, a.round)
    title = title_state(run)
    spec, meta = build_spec(run, a.round, script, al, approved, title)
    result = build_bundle(spec, out, meta)
    info = {"round": a.round, "html": result["html"], "url": result["url"], "bundle": result["bundle"],
            "expectedAnswers": result["expectedAnswers"], "scenes": [s["id"] for s in meta["scenes"]],
            "approved_earlier": sorted(approved), "title_asked": bool(meta["title"] and meta["title"]["asked"]),
            "rebuilt": result["rebuilt"], **open_page(result["url"], a.no_open)}
    emit("GATE1", info)
    return 0


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    responses, pointer = read_answers(out)
    approved = set(pointer["earlier_approved"])
    tmeta = pointer.get("title")
    title = {"action": "none", "note": ""}
    if tmeta and tmeta["asked"]:
        t_action, t_note = decision(responses, "title")
        if t_action == "edit" and not t_note.strip():
            raise Fail("the title is marked edit without a note; the note is the next title render's fix. "
                       "Add a note to the answers and apply again")
        title = {"action": t_action, "note": t_note}
    elif tmeta:
        title["action"] = "approved_earlier"
    # title side effects first (idempotent per round), so changes.json only exists once they are in
    if title["action"] == "approve":
        title_mod.approve(run, a.round, tmeta["sha256"])
    elif title["action"] == "edit":
        title_mod.reopen(run, title["note"], gate_round=a.round)
    title_approved = tmeta is None or bool(title_mod.load(run).get("approved"))
    edits, approved_notes = [], []
    for sc in pointer["scenes"]:
        action, note = decision(responses, f"scene-{sc['id']}")
        segs = []
        for seg_id in sc["segments"]:
            s_action, s_note = decision(responses, seg_qid(seg_id))
            segs.append({"id": seg_id, "action": s_action, "note": s_note})
        if action == "edit" or any(s["action"] == "edit" for s in segs):
            edits.append({"id": sc["id"], "action": "edit", "note": note, "segments": segs})
            continue
        approved.add(sc["id"])
        noted = [{"id": s["id"], "note": s["note"]} for s in segs if s["note"]]
        if note or noted:
            approved_notes.append({"id": sc["id"], "note": note, "segments": noted})
    changes = {"round": a.round, "approved_scenes": sorted(approved),
               "approved_sha256": {str(sid): pointer["scene_sha256"][str(sid)] for sid in sorted(approved)},
               "script_note": response(responses, "script")["value"],
               "narration_note": response(responses, "narration")["value"],
               "characters_note": response(responses, "characters")["value"] if "characters" in responses else "",
               "notes": response(responses, "notes")["value"],
               "scenes": edits, "approved_scene_notes": approved_notes,
               "title": title, "title_approved": title_approved}
    write_json(out / "changes.json", changes)
    state = mark_gate(out, pointer, not edits and title_approved)
    emit("APPLY", {"round": a.round, "changes": str(out / "changes.json"), "reviewed_scenes": len(pointer["scenes"]),
                   "approved_scenes": len(changes["approved_scenes"]), "edit_scenes": len(edits),
                   "edit_segments": sum(1 for sc in edits for s in sc["segments"] if s["action"] == "edit"),
                   "has_script_note": bool(changes["script_note"]),
                   "has_narration_note": bool(changes["narration_note"]),
                   "title": title["action"], "title_approved": title_approved, "gate_state": state})
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
    try:
        return {"build": cmd_build, "apply": cmd_apply}[a.cmd](a)
    except (title_mod.Fail, title_mod.rs().Fail) as exc:   # title.py / render_scenes failures: same FAILED line
        raise Fail(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(run_main(main))
