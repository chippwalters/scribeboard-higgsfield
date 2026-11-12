"""The style gate (gate6): the user approves scene 1 before any other picture is drawn.

Scene 1 is the style reference: every later scene and the title card are drawn with it attached. So in a setup run
(runsetup.required(): scribe-higgsfield) the user approves it, as it is now (its sha256), on its own page, after it
passed its independent review and before render-prep draws scene 2 or later or title.py prep draws the title. Both
refuse until then (`style_problem`), and scribe.py routes this page between scene 1's review and the other scenes.

  gate6.py build --run RUN --round R [--no-open]
      Refused unless scene 1 has a selected picture (scenes/scene_1_4k.png). Shows it with its review score and
      deductions; one question: approve, or edit with a note. Prints GATE6_JSON.
  gate6.py apply --run RUN --round R
      changes.json: {"round", "approved": bool, "approved_sha256" (when approved), "shown_sha256", "note"}. An edit
      needs its note; scribe.py then reopens scene 1 with the note as the fix (render_scenes.py reopen), and the
      redrawn scene comes back to the next round of this page.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import runsetup
from gatelib import (DECISION, Fail, build_bundle, decision, emit, gate_dir, load_json, mark_gate, open_page,
                     read_answers, response, run_main, unanswered_stem, write_json)

GATE = "gate6"


def scene1(run: Path) -> Path:
    return run / "scenes" / "scene_1_4k.png"


def scene1_sha(run: Path) -> str:
    p = scene1(run)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def rounds(run: Path) -> list[int]:
    g = run / "gates"
    return sorted(int(p.name.split("-r")[1]) for p in g.glob(f"{GATE}-r*") if p.is_dir()) if g.is_dir() else []


def style_problem(run: Path) -> str | None:
    """None when no style approval is needed (no setup) or the newest round approved scene 1 as it is now; else why
    the other pictures must wait."""
    if not runsetup.required():
        return None
    if not scene1(run).is_file():
        return "scene 1 (the style reference) has no approved-for-review picture yet"
    rs_ = rounds(run)
    if not rs_:
        return "scene 1 is not approved by the user yet (the style page, gate6.py; follow scribe.py status)"
    ch_p = gate_dir(run, GATE, rs_[-1]) / "changes.json"
    if not ch_p.is_file():
        return f"the style page round {rs_[-1]} is not answered yet"
    ch = load_json(ch_p, "style changes.json")
    if ch.get("approved") is not True:
        return f"the style page round {rs_[-1]} asked to redraw scene 1"
    if ch.get("approved_sha256") != scene1_sha(run):
        return "scene 1 changed after the user approved it; it needs the next style round"
    return None


def review_summary(run: Path) -> str:
    """The selected attempt's review score and deductions, from the scenes state, when available."""
    try:
        st = json.loads((run / "scenes" / "state.json").read_text(encoding="utf-8"))
        sc = st["scenes"]["1"]
        k = sc.get("selected")
        att = sc["attempts"][int(k) - 1] if k else None
        v = (att or {}).get("verdict") or {}
        if not v:
            vp = run / "scenes" / "attempts" / f"scene_1_a{k}_review" / "verdict.json"
            v = json.loads(vp.read_text(encoding="utf-8")) if vp.is_file() else {}
        if not v:
            return ""
        ded = v.get("deductions") or []
        return (f"Independent review: score **{v.get('score')}** (attempt {k})"
                + (": " + "; ".join(str(d) for d in ded) if ded else ", no deductions") + ".")
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return ""


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    if not runsetup.required():
        raise Fail("the style page is for setup runs (scribe-higgsfield)")
    pic = scene1(run)
    if not pic.is_file():
        raise Fail("scene 1 has no selected picture yet (it must pass its review first; follow scribe.py status)")
    out = gate_dir(run, GATE, a.round)
    unanswered_stem(out)
    sha = scene1_sha(run)
    spec = {"title": f"Style · scene 1 · round {a.round}", "slug": f"gate6-r{a.round}", "stageLabel": "Style",
            "intro": ("Scene 1 sets the drawing style for every other scene and the title card: each of them is "
                      "drawn with this picture attached as the style reference. Approve it before they are drawn, "
                      "or say what must change.\n\n"
                      f"**Picture:** `{pic}`\n\n" + review_summary(run)),
            "questions": [{
                "id": "scene-1", "type": "single", "prompt": "Scene 1", "required": True, "allowNote": True,
                "media": [{"type": "image", "path": str(pic), "alt": "Scene 1"}],
                "options": [{"value": "approve", "label": "Approve: use this style for every scene"},
                            {"value": "edit", "label": "Edit (say what must change in the note)"}],
                "notePlaceholder": "What should the next drawing of scene 1 change?"}]}
    result = build_bundle(spec, out, {"round": a.round, "kind": "style", "shown_sha256": sha})
    emit("GATE6", {"round": a.round, "html": result["html"], "url": result["url"], "bundle": result["bundle"],
                   "expectedAnswers": result["expectedAnswers"], "rebuilt": result["rebuilt"],
                   **open_page(result["url"], a.no_open)})
    return 0


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    responses, pointer = read_answers(out)
    action, note = decision(responses, "scene-1")
    if action == "edit" and not note.strip():
        raise Fail("scene 1 is marked edit without a note; ask the user what must change, add it to the answers "
                   "and apply again")
    approved = action == "approve"
    changes = {"round": a.round, "approved": approved, "shown_sha256": pointer["shown_sha256"],
               "approved_sha256": pointer["shown_sha256"] if approved else None, "note": note,
               "notes": response(responses, "notes")["value"]}
    write_json(out / "changes.json", changes)
    state = mark_gate(out, pointer, approved)
    emit("APPLY", {"round": a.round, "approved": approved, "note": note, "gate_state": state,
                   "changes": (out / "changes.json").as_posix()})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--round", type=int, required=True)
        if name == "build":
            p.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "apply": cmd_apply}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(run_main(main))
