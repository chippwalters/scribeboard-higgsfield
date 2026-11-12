"""Character sheet helpers for runs with a setup (runsetup.required(): Higgsfield makes the pictures).

There is no page of its own (no gate4 round folders): every character sheet is drawn before Gate 1 from the start
page's choices, and Gate 1 (gate0.py) shows each one with an approve / edit question, recorded in its changes.json
"cast". Every scene that shows a character carries its sheet as a reference, so no scene is initialised or rendered
until the newest Gate 1 round that answered the sheets approved every sheet as it is now (its sha256): cast_problem,
which render_scenes.py init and render-prep refuse on. Sheets are optional: a character the start page gave "no sheet"
has none.

An edit goes back to the sheet: `characters.py prep --run RUN --id ID --force --fix "<note>"`, the Higgsfield handoff,
the sheet review, then the next Gate 1 round (scribe.py routes each step).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import runsetup
from gatelib import Fail, gate_dir, load_json


def sheet(run: Path, cid: str) -> Path:
    return run / "characters" / f"{cid}_sheet.png"


def sheet_sha(run: Path, cid: str) -> str:
    p = sheet(run, cid)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def sheeted(run: Path, chars: list[dict]) -> list[dict]:
    """The declared characters that have a character sheet (the start page's answer; a sheet is optional)."""
    try:
        want = set(runsetup.sheet_ids(run))
    except runsetup.SetupError as e:
        raise Fail(str(e)) from e
    return [c for c in chars if c["id"] in want]


def sheet_media(run: Path, cid: str, name: str, photos: list) -> tuple[list[dict], str]:
    """(page media, note): the sheet, then each photo it was drawn from that is still on disk. A photo moved or deleted
    since is named in the note instead (the page builder refuses missing media; the sheet stands without it)."""
    media = [{"type": "image", "path": str(sheet(run, cid)), "alt": f"{name} character sheet"}]
    gone = []
    for i, p in enumerate(photos, 1):
        if Path(p).is_file():
            media.append({"type": "image", "path": str(p), "alt": f"photo {i} of {name}"})
        else:
            gone.append(str(p))
    note = ("\n\n**Not shown:** the image file(s) " + ", ".join(f"`{g}`" for g in gone) + " are no longer on disk."
            if gone else "")
    return media, note


def sheet_shas(run: Path) -> dict[str, str]:
    """{id: sha256} of every character sheet the run asks for (sheets are optional; runsetup.sheet_ids)."""
    try:
        ids = runsetup.sheet_ids(run)
    except runsetup.SetupError:
        return {}
    return {cid: sheet_sha(run, cid) for cid in ids}


def previous_sheet_answer(run: Path, cid: str, sha: str) -> str | None:
    """The user's last approve / edit answer for character `cid`'s sheet when it is shown again exactly as then (the
    same sha256): the newest answered Gate 1 round that showed it. None when the sheet changed since, or was never
    answered."""
    g1 = gate1_cast_record(run)
    if g1 is None or (g1[1].get("shown_sha256") or {}).get(cid) != sha:
        return None
    if cid in restored_since(run, g1[0]):   # the user restored this sheet after that round: its edit is answered
        return None
    row = next((x for x in g1[1].get("characters") or [] if x.get("id") == cid), None)
    return row["action"] if row and row.get("action") in ("approve", "edit") else None


def restored_since(run: Path, r: int) -> set[str]:
    """The characters whose current sheet was restored (characters.py restore, the user's choice) after Gate 1 round
    `r` asked to change it: that edit is answered even when the restored sheet is the one round r showed."""
    import characters
    out = set()
    for name, rec in characters.load_sheets(run).items():
        rest = rec.get("restored") if isinstance(rec, dict) else None
        if isinstance(rest, dict) and (rest.get("after_gate1_round") or 0) >= r:
            out.add(rec.get("id") or name.removesuffix("_sheet.png"))
    return out


def gate1_cast_record(run: Path) -> tuple[int, dict, dict] | None:
    """The sheets are approved on Gate 1 (gate0.py): (round, its changes.json "cast" record, the sheets' sha256 that
    round showed) of the newest answered Gate 1 round that showed sheets, else None."""
    gates = run / "gates"
    rounds_ = sorted((int(p.name.split("-r")[1]) for p in gates.glob("gate0-r*") if p.is_dir()), reverse=True) \
        if gates.is_dir() else []
    for r in rounds_:
        ch_file = gate_dir(run, "gate0", r) / "changes.json"
        if not ch_file.is_file():
            continue
        cast = load_json(ch_file, "gate0 changes.json").get("cast")
        if isinstance(cast, dict):
            return r, cast, cast.get("shown_sha256") or {}
    return None


def cast_problem(run: Path) -> str | None:
    """None when no scene may wait on the character sheets: no setup required, no declared character with a sheet, or
    the newest Gate 1 round that answered the sheets approved every sheet exactly as it is now (sha256). Otherwise why
    scenes must wait. render_scenes.py init and render-prep refuse on it, so the gate holds even when someone calls
    them directly."""
    if not runsetup.required():
        return None
    try:
        chars = sheeted(run, load_json(run / "script.json", "script.json").get("characters") or [])
    except Fail as e:
        return str(e)
    if not chars:
        return None
    stale = [c["id"] for c in chars if not ready(run, c["id"])]
    if stale:   # e.g. a photo file changed since its sheet was drawn
        return f"the sheet(s) of {', '.join(stale)} must be drawn again for the current setup before any scene"
    rec = gate1_cast_record(run)
    now = {c["id"]: sheet_sha(run, c["id"]) for c in chars}
    if rec is None:
        return "the character sheets are not approved on Gate 1 yet (follow scribe.py status)"
    approved = rec[1].get("approved_sha256") or {}
    if rec[1].get("approved") is not True or {k: approved.get(k) for k in now} != now:   # only those with a sheet
        return f"Gate 1 round {rec[0]} did not approve every character sheet as it is now"
    return None


def ready(run: Path, cid: str) -> bool:
    import characters   # the sheet state (made through Higgsfield, sheet review passed)
    return characters.sheet_ready(run, cid)
