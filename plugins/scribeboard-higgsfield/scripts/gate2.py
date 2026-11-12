"""Gate 2: approve the board image (videos/board.png) before any video frame is rendered.

  gate2.py build --run RUN --round R [--no-open]
      Generate an html-questions spec in RUN/gates/gate2-rR/: the whole board (videos/board.png, written by
      `stitch.py board` with videos/board_layout.json) with its size, bands, seed, flow arrows and fill numbers, and
      one required question `board` (approve / edit + note, e.g. "tighter", "new seed", "move scene 5 next to 6",
      "fewer arrows"). board.png is also copied into the round folder and linked from the page so it opens at full
      size (zoomable). Build the page there (one self-contained HTML file, spot 2 on the run's gate rail
      RUN/gates/gates.js) and open it in a new Chrome tab (--no-open skips that). Refused when board_layout.json is
      stale (its fingerprint is not the current inputs' one). A round that was built but not answered is rebuilt in
      place. Round R > 1 lists round R-1's note (or that it was superseded) and the layout values changed since.
      gate.json records the layout fingerprint shown. Prints GATE2_JSON.
  gate2.py apply --run RUN --round R
      Validate the pasted answers (saved at the bundle's expectedAnswers path) with html_questions read
      and write RUN/gates/gate2-rR/changes.json. An edit needs a note. The round's rail dot turns accepted when the
      board is approved, else answered. Prints APPLY_JSON.

changes.json:
  {"round": R, "approved": true|false, "fingerprint": "<layout fingerprint shown>",
   "approved_fingerprint": "<the same when approved, else null>", "action": "approve|edit", "note": "",
   "notes": "<general notes>"}
The orchestrator applies an edit in styles/<style>/module.json `layout` (seed, gap_px, margin_px, scale, tilt_deg,
arrows.*) and runs `stitch.py board`; a new round follows. Rounds of the old per-scene Gate 2 (gate.json without
"kind": "board") are superseded: the next round number is the highest existing one + 1.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
from pathlib import Path

from gatelib import (DECISION, GATE_POINTER, Fail, build_bundle, decision, emit, gate_dir, load_json, load_script,
                     mark_gate, open_page, read_answers, require_file, response, run_main, unanswered_stem, write_json)

GATE = "gate2"
KIND = "board"
FULL_NAME = "board_full.png"   # board.png copied into the round folder, linked for the full-size view


def stitch_mod():
    spec = importlib.util.spec_from_file_location("stitch", Path(__file__).with_name("stitch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_board(run: Path) -> tuple[dict, Path]:
    """videos/board_layout.json (current for the run's inputs) and videos/board.png."""
    st = stitch_mod()
    try:
        mod, al = st.rv.load_module(st.rv.run_style(run)), st.rv.load_alignment(run)
        doc = st.load_layout(run, mod, al)
    except st.Fail as exc:
        raise Fail(str(exc)) from exc
    png = require_file(run / "videos" / "board.png", "board.png (run stitch.py board first)")
    return doc, png


def since_last_round(run: Path, rnd: int, layout: dict) -> str:
    """Round R > 1: the previous board round's answer and the layout values changed since."""
    if rnd == 1:
        return ""
    prev = gate_dir(run, GATE, rnd - 1)
    ptr = prev / GATE_POINTER
    pointer = load_json(ptr, f"gate2 round {rnd - 1} gate pointer") if ptr.is_file() else {}
    if pointer.get("kind") != KIND:
        return f"**Round {rnd - 1}** reviewed the per-scene videos (the old Gate 2); this page replaces it."
    if (prev / "changes.json").is_file():
        ch = load_json(prev / "changes.json", f"gate2 round {rnd - 1} changes.json")
        head = (f"**Since round {rnd - 1}** ({'approved' if ch.get('approved') else 'edit asked'}): "
                f"{ch.get('note') or '(no note)'}")
    else:
        head = f"**Round {rnd - 1}** was superseded by a rebuilt board before it was answered."
    old = pointer.get("layout") or {}
    diffs = []
    for k in sorted(set(old) | set(layout)):
        a, b = old.get(k), layout.get(k)
        if isinstance(a, dict) or isinstance(b, dict):
            a, b = a or {}, b or {}
            diffs += [f"- layout {k}.{j}: {a.get(j)!r} → {b.get(j)!r}" for j in sorted(set(a) | set(b))
                      if a.get(j) != b.get(j)]
        elif a != b:
            diffs.append(f"- layout {k}: {a!r} → {b!r}")
    return "\n\n".join([head, "Layout values changed:", "\n".join(diffs) or "- none"])


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def build_spec(run: Path, rnd: int, script: dict, out: Path) -> tuple[dict, dict]:
    doc, png = load_board(run)
    b, fill, arrows = doc["board"], doc["fill"], doc["arrows"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png, out / FULL_NAME)
    scenes = sorted({p["scene"] for p in doc["pieces"] if p["scene"] is not None})
    arrow_list = ", ".join(f"{a['from']} → {a['to']} ({a['colour']})" for a in arrows) or "none"
    body = "\n".join([
        f"[Open the board at full size]({FULL_NAME}) ({doc['board_png_size'][0]}×{doc['board_png_size'][1]}; "
        "zoom in the browser).",
        "",
        f"- Board **{b['width']}×{b['height']}** px (16:9), {len(doc['pieces']) - 1} segments of {len(scenes)} "
        f"scenes + the title, in story order along a serpentine of {b['bands']} bands "
        f"({' / '.join(str(len(r)) for r in b['rows'])} pieces), layout seed {doc['seed']}.",
        f"- Fill: piece boxes **{pct(fill['piece_boxes'])}**, dilated ink **{pct(fill['dilated_ink'])}** of the "
        f"board; empty (more than {fill['empty_px']} px from any ink) **{pct(fill['empty'])}**, "
        f"{pct(fill['empty_with_arrows'])} with the arrows.",
        f"- Flow arrows: **{len(arrows)}**, each in a blank gap between two pieces that follow each other in the "
        f"story, pointing to the later one: {arrow_list}.",
        "",
        "The video is rendered from exactly this layout once it is approved: the camera follows the pen from "
        "piece to piece in this order, the pen draws each arrow on the way, and the ending zooms out to this "
        "whole board.",
    ])
    question = {
        "id": "board", "type": "single", "prompt": "The board", "required": True, "allowNote": True,
        "options": DECISION, "body": body,
        "media": [{"type": "image", "path": str(png), "alt": "The whole board"}],
        "notePlaceholder": "What should change (e.g. tighter, new seed, move scene 5 next to 6, fewer arrows)?",
    }
    intro = (f"Round {rnd}. Approve the whole board before the video is made, or ask for an edit in the note "
             "(it goes into the layout values and the board is rebuilt for a new round).")
    changed = since_last_round(run, rnd, doc["module"]["layout"])
    if changed:
        intro += "\n\n" + changed
    spec = {"title": f"Gate 2 · {script.get('title', 'Scribeboard')} · board · round {rnd}", "slug": f"gate2-r{rnd}",
            "stageLabel": "Gate 2", "intro": intro, "questions": [question]}
    meta = {"round": rnd, "kind": KIND, "fingerprint": doc["fingerprint"], "layout": doc["module"]["layout"],
            "seed": doc["seed"], "board": [b["width"], b["height"]], "fill": fill, "arrows": len(arrows)}
    return spec, meta


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    import runsetup   # scribe-higgsfield has no board page: Gate 3 shows the video and the board together
    if runsetup.required():
        raise Fail("scribe-higgsfield has no board page: the board is shown on the video page (gate3.py); follow "
                   "scribe.py status")
    script = load_script(run)
    out = gate_dir(run, GATE, a.round)
    unanswered_stem(out)   # refuse an applied or answered round before copying anything into it
    spec, meta = build_spec(run, a.round, script, out)
    result = build_bundle(spec, out, meta)
    info = {"round": a.round, "html": result["html"], "url": result["url"], "bundle": result["bundle"],
            "expectedAnswers": result["expectedAnswers"], "fingerprint": meta["fingerprint"],
            "board": meta["board"], "fill": meta["fill"], "arrows": meta["arrows"], "rebuilt": result["rebuilt"],
            **open_page(result["url"], a.no_open)}
    emit("GATE2", info)
    return 0


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    responses, pointer = read_answers(out)
    if pointer.get("kind") != KIND:
        raise Fail(f"{out} is a round of the old per-scene Gate 2; it is superseded (build the next round)")
    action, note = decision(responses, "board")
    if action == "edit" and not note.strip():
        raise Fail("the board is marked edit without a note; the note says what to change. Add a note to the "
                   "answers and apply again")
    approved = action == "approve"
    changes = {"round": a.round, "approved": approved, "fingerprint": pointer["fingerprint"],
               "approved_fingerprint": pointer["fingerprint"] if approved else None, "action": action,
               "note": note, "notes": response(responses, "notes")["value"]}
    write_json(out / "changes.json", changes)
    state = mark_gate(out, pointer, approved)
    emit("APPLY", {"round": a.round, "changes": str(out / "changes.json"), "approved": approved, "action": action,
                   "note": note, "gate_state": state})
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
