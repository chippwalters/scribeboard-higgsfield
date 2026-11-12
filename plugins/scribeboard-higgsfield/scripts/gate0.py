"""Gate 0: approve the narration text (RUN/narration.json) before any visual script exists. Text only, no audio.

  gate0.py build --run RUN --round R [--alt LABEL=PATH ...] [--no-open]
      Generate an html-questions spec in RUN/gates/gate0-rR/, build the page there (one self-contained HTML file,
      spot 0 on the run's gate rail RUN/gates/gates.js) and open it in a new Chrome tab (--no-open skips that).
      Refused unless narration.json validates and voice/voice.json is passed or
      flagged for the current narration fingerprint (voice.py), and when the newest earlier round is applied and
      approved this exact narration. The page names the source story, then asks one approve/edit question per paragraph
      (`paragraph-<id>`: its title and its sentences with their ids), the whole narration (`narration`; it
      opens with the open flags when the voice review flagged it), and the general `notes` box.
      Changes only: when an earlier round approved a narration (the newest changes.json with approved_fingerprint)
      and its text is on disk (that round's narration.shown.json, else voice/r<K>/narration.json whose checklist has
      that fingerprint), the page asks only about the paragraphs that differ from it (title or any sentence added,
      removed or reworded, whitespace-normalized; a new paragraph; one a later round asked to edit). Each body lists
      only the changed sentences, old -> new. The unchanged paragraphs are one collapsed "approved in round R" entry
      in the intro, removed ones a line there; no `narration` question (Gate 1 has none either; `notes` stays).
      Nothing changed paragraph by paragraph (e.g. only the voice settings): the full page.
      Each --alt (a Markdown file of narration text; LABEL = letters, digits, '.', '_', '-', not current/merge)
      is shown in full beside the current narration, and a required `version` question offers `current`,
      `alt:<LABEL>` per alt, or `merge` (described in the note). The alts are copied into the round folder
      (alt-<LABEL>.md) and recorded with their paths and sha256 in alts.json and gate.json. The shown narration.json
      is copied to narration.shown.json. A round that was built but has no answers file and no changes.json is
      rebuilt in place (its old page files are replaced). Prints GATE0_JSON.
  gate0.py apply --run RUN --round R
      Validate the pasted answers (saved at the bundle's expectedAnswers path) with html_questions read and write
      RUN/gates/gate0-rR/changes.json. Prints APPLY_JSON. An `edit` needs its note (a paragraph edit its own
      note; a whole-narration edit its note or the general notes), and so does `merge`. The round's rail dot turns
      accepted when it approved the narration (approved_fingerprint set), else answered.

Setup (runsetup.py; required whenever Higgsfield makes the pictures or the narration): the same page is the run's setup
page, Gate 1 on the rail. Each paragraph plays its recorded sentences. The page names what the start page (gate5.py)
chose (title, voice, image model, style, colours, characters), shows each character sheet (drawn before this page)
with an approve / edit question (`sheet-<id>`; changes.json "cast"), and asks only the credit budget (with the run's
estimated range at the chosen model). Every round asks again, preselecting the previous answers. `apply` checks them
(runsetup.from_answers): any problem (a budget that is not a number) is refused with the reasons, like a missing note,
and nothing is written. A round that approves the narration with valid answers writes RUN/setup.json (the budget plus
the start's choices); changes.json keeps the answers either way ("setup"). A round that approved the narration while
the setup is missing or no longer valid (the cast changed), or while a sheet is not approved as it is now, does not
complete Gate 1: build the next round.

changes.json (contract `Gates`): {"round", "approved": bool, "approved_fingerprint": the narration's voice
fingerprint shown on the page when every paragraph shown and the whole narration are approved and version is
"current", else null, "fingerprint" (the fingerprint shown), "version": "current"|"alt:<LABEL>"|"merge",
"version_note", "version_file" (the round folder's copy of the chosen alt, or null), "narration": {"action",
"note"} (action "approved_earlier" on a changes-only page), "paragraphs": [{"id", "action", "note"}] (the shown
paragraphs), "baseline_round" (the approval a changes-only page compared against, else null),
"unchanged_paragraphs" (ids not shown, still approved), "notes"}. Gate 0 passes when the latest round's
approved_fingerprint equals the current narration fingerprint (scribe.py stage 0c). Any other outcome goes to
the writer agent, who rewrites narration.json; the new text is voice-reviewed and then shown in round R+1.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import re
import shutil
from pathlib import Path

import runsetup   # scripts/runsetup.py: the setup questions and RUN/setup.json (required with Higgsfield)
import voice as voice_mod
from gatelib import (DECISION, GATE_POINTER, Fail, build_bundle, decision, decision_options, emit, gate_dir, load_json,
                     mark_gate,
                     open_page, read_answers, response, run_main, unanswered_stem, write_json)

GATE = "gate0"
ALT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
RESERVED = {"current", "merge"}
SHOWN = "narration.shown.json"   # the round folder's copy of the narration.json the page shows


def para_qid(pid: int) -> str:
    return f"paragraph-{pid}"


def md_text(text: str) -> str:
    """A sentence as literal Markdown text (no accidental emphasis, code or HTML)."""
    return re.sub(r"([\\`*_\[\]])", r"\\\1", html.escape(text, quote=False))


def load_narration(run: Path) -> tuple[dict, list[dict], dict, str]:
    doc = load_json(run / voice_mod.NARRATION, "narration.json (the writer's pass 1)")
    paras = voice_mod.validate_narration(doc)
    parts = voice_mod.fingerprint_parts(doc)
    return doc, paras, parts, voice_mod.fingerprint(parts)


def voice_state(run: Path, doc: dict, parts: dict, fp: str) -> dict:
    """voice.json when it passed or flagged this exact narration; otherwise Gate 0 is refused. "Keep my words" runs
    (start page) have no voice review: their text is checked word for word against the script instead."""
    if voice_mod.keep_words(run):
        try:
            voice_mod.validate_narration(doc, run, balance=True)   # includes the word-for-word check
        except voice_mod.Fail as e:
            raise Fail(str(e)) from e
        return {"status": "kept", "round": 0, "score": "-", "reviewed_by": "word-for-word check (keep my words)",
                "fingerprint": fp}
    vdoc = voice_mod.load(run)
    status = voice_mod.status_of(vdoc, fp)
    if status not in ("passed", "flagged"):
        raise Fail(f"the voice review is '{status}' for the current narration.json (next: "
                   f"{voice_mod.next_action(vdoc, parts, doc, voice_mod.NARRATION)}); Gate 0 shows only a narration "
                   f"whose voice review passed or was flagged")
    return vdoc


def setup_problem(run: Path) -> str | None:
    """Why the run's setup is not complete (None when RUN/setup.json is valid)."""
    try:
        runsetup.load(run)
        return None
    except runsetup.SetupError as e:
        return str(e)


def latest_applied(run: Path, rnd: int) -> tuple[int, dict] | None:
    """The newest round before `rnd` that has a changes.json, with it."""
    for r in range(rnd - 1, 0, -1):
        p = gate_dir(run, GATE, r) / "changes.json"
        if p.is_file():
            return r, load_json(p, f"gate0 round {r} changes.json")
    return None


def approved_text(run: Path, r: int, fp: str) -> dict | None:
    """The narration round `r` approved (fingerprint `fp`): the round folder's SHOWN copy when gate.json shows `fp`,
    else (rounds built before SHOWN existed) the voice round that reviewed `fp` (voice/r<K>/narration.json)."""
    rdir = gate_dir(run, GATE, r)
    pointer = rdir / GATE_POINTER
    if (rdir / SHOWN).is_file() and pointer.is_file() and load_json(pointer, "gate pointer").get("fingerprint") == fp:
        return load_json(rdir / SHOWN, f"gate0 round {r} {SHOWN}")
    vd = voice_mod.vdir(run)
    for k in sorted((int(p.name[1:]) for p in vd.glob("r*") if p.is_dir() and p.name[1:].isdigit()), reverse=True):
        check, text = voice_mod.rdir(run, k) / "checklist.json", voice_mod.rdir(run, k) / voice_mod.NARRATION
        if check.is_file() and text.is_file() and load_json(check, f"voice round {k} checklist").get("fingerprint") == fp:
            return load_json(text, f"voice round {k} narration.json")
    return None


def approved_baseline(run: Path, rnd: int) -> tuple[int, dict] | None:
    """(round, narration doc) of the newest round before `rnd` whose changes.json approved a narration, when that
    narration's text is on disk; None without one (the page then shows everything)."""
    for r in range(rnd - 1, 0, -1):
        p = gate_dir(run, GATE, r) / "changes.json"
        fp = load_json(p, f"gate0 round {r} changes.json").get("approved_fingerprint") if p.is_file() else None
        if fp:
            doc = approved_text(run, r, fp)
            return (r, doc) if doc is not None else None
    return None


def sentence_changes(old: list[dict], new: list[dict]) -> list[tuple[str, str, str, str]]:
    """(kind, id, old text, new text) per changed sentence, texts whitespace-normalized; kind is reworded, added or
    removed. Sentences are aligned by text, so an inserted sentence does not mark the renumbered ones after it."""
    a = [voice_mod.norm(g["text"]) for g in old]
    b = [voice_mod.norm(g["text"]) for g in new]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        pairs = min(i2 - i1, j2 - j1)
        for k in range(pairs):
            oid, nid = old[i1 + k]["id"], new[j1 + k]["id"]
            out.append(("reworded", nid if oid == nid else f"{oid} → {nid}", a[i1 + k], b[j1 + k]))
        out += [("removed", old[i]["id"], a[i], "") for i in range(i1 + pairs, i2)]
        out += [("added", new[j]["id"], "", b[j]) for j in range(j1 + pairs, j2)]
    return out


def diff_body(title_old: str | None, title_new: str, changes: list[tuple[str, str, str, str]]) -> str:
    blocks = []
    if title_old is not None and voice_mod.norm(title_old) != voice_mod.norm(title_new):
        blocks.append(f"**Title** reworded\n\n> old: {md_text(title_old)}\n>\n> new: {md_text(title_new)}")
    for kind, sid, old, new in changes:
        if kind == "reworded":
            blocks.append(f"**{sid}** reworded\n\n> old: {md_text(old)}\n>\n> new: {md_text(new)}")
        elif kind == "added":
            blocks.append(f"**{sid}** added\n\n> new: {md_text(new)}")
        else:
            blocks.append(f"**{sid}** removed\n\n> old: {md_text(old)}")
    return "\n\n".join(blocks)


def diff_plan(base: dict, paras: list[dict], prev: tuple[int, dict] | None, base_round: int) -> dict:
    """Which paragraphs the page asks about against the approved baseline: every paragraph whose title or sentences
    (whitespace-normalized) differ, a new paragraph, and one a round after the baseline asked to edit."""
    old = {int(p["id"]): p for p in base.get("paragraphs", [])}
    asked = {}
    if prev is not None and prev[0] > base_round:
        asked = {p["id"]: p.get("note", "") for p in prev[1].get("paragraphs", []) if p.get("action") == "edit"}
    shown, unchanged = [], []
    for p in paras:
        o = old.get(p["id"])
        if o is None:
            shown.append((p, diff_body(None, p["title"], [("added", g["id"], "", voice_mod.norm(g["text"]))
                                                          for g in p["sentences"]])))
            continue
        body = diff_body(o.get("title", ""), p["title"], sentence_changes(o.get("sentences", []), p["sentences"]))
        if not body and p["id"] in asked:
            body = (f"Unchanged since round {base_round}, but round {prev[0]} asked: {md_text(asked[p['id']])}\n\n"
                    + "\n>\n".join(f"> **{g['id']}** {md_text(voice_mod.norm(g['text']))}" for g in p["sentences"]))
        if body:
            shown.append((p, body))
        else:
            unchanged.append(p)
    removed = [o for pid, o in sorted(old.items()) if pid not in {p["id"] for p in paras}]
    return {"shown": shown, "unchanged": unchanged, "removed": removed}


def unchanged_block(unchanged: list[dict], base_round: int) -> str:
    """One collapsed entry for every paragraph unchanged since the approval (no question)."""
    ids = ", ".join(str(p["id"]) for p in unchanged)
    inner = "".join(f"<p><strong>{p['id']}. {html.escape(p['title'])}</strong> "
                    f"{html.escape(' '.join(voice_mod.norm(g['text']) for g in p['sentences']))}</p>"
                    for p in unchanged)
    return (f"<details><summary>{len(unchanged)} unchanged paragraph{'s' if len(unchanged) != 1 else ''} "
            f"({ids}) — approved in round {base_round}</summary>{inner}</details>")


def parse_alts(specs: list[str]) -> list[dict]:
    alts, seen = [], set()
    for spec in specs:
        label, sep, raw = spec.partition("=")
        label = label.strip()
        if not sep or not ALT_LABEL.match(label) or label.casefold() in RESERVED:
            raise Fail(f"--alt {spec!r}: use LABEL=PATH with LABEL of letters, digits, '.', '_' or '-' (at most 32, "
                       f"not 'current' or 'merge')")
        if label.casefold() in seen:
            raise Fail(f"--alt label {label!r} is given twice")
        seen.add(label.casefold())
        path = Path(raw.strip()).resolve()
        if not path.is_file():
            raise Fail(f"--alt {label}: file not found: {path}")
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
        if not text.strip():
            raise Fail(f"--alt {label}: {path} is empty")
        alts.append({"label": label, "path": path.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                     "copy": f"alt-{label}.md", "text": text})
    return alts


def current_column(paras: list[dict]) -> str:
    return "".join(f"<p><strong>{p['id']}. {html.escape(p['title'])}</strong></p>"
                   f"<p>{html.escape(' '.join(voice_mod.norm(g['text']) for g in p['sentences']))}</p>"
                   for p in paras)


def alt_column(text: str) -> str:
    """A Markdown alternative as HTML paragraphs: blank lines separate blocks, '#' lines become bold headings."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if lines and all(ln.startswith("#") for ln in lines):
            out += [f"<p><strong>{html.escape(ln.lstrip('#').strip())}</strong></p>" for ln in lines]
        elif lines:
            out.append(f"<p>{html.escape(' '.join(lines))}</p>")
    return "".join(out)


def versions_table(paras: list[dict], alts: list[dict]) -> str:
    """Every version side by side, in full: one HTML table on one line (no blank line may break the HTML block)."""
    width = f"{100 // (len(alts) + 1)}%"
    cell = f'style="vertical-align:top;width:{width}"'
    heads = ["Current narration (narration.json)"] + [f"{a['label']}" for a in alts]
    cols = [current_column(paras)] + [alt_column(a["text"]) for a in alts]
    return ("<table><thead><tr>" + "".join(f"<th {cell}>{html.escape(h)}</th>" for h in heads) + "</tr></thead>"
            "<tbody><tr>" + "".join(f"<td {cell}>{c}</td>" for c in cols) + "</tr></tbody></table>")


def previous_asks(prev: tuple[int, dict] | None, fp: str) -> str:
    if prev is None:
        return ""
    r, ch = prev
    rows = [f"- paragraph {p['id']}: {p['note']}" for p in ch.get("paragraphs", []) if p.get("action") == "edit"]
    if (ch.get("narration") or {}).get("action") == "edit":
        rows.append(f"- whole narration: {ch['narration'].get('note', '')}")
    if ch.get("version", "current") != "current":
        rows.append(f"- version: {ch['version']}" + (f" ({ch['version_note']})" if ch.get("version_note") else ""))
    if ch.get("notes"):
        rows.append(f"- notes: {ch['notes']}")
    if not rows:
        return ""
    since = ("the writer has rewritten the narration since" if ch.get("fingerprint") != fp
             else "the narration below is still the text that round showed")
    return f"**Round {r} asked for** ({since}):\n\n" + "\n".join(rows)


def previous_answers(run: Path, prev: tuple[int, dict] | None, paras: list[dict], fp: str) -> tuple[dict, str | None]:
    """({paragraph id: approve/edit}, whole-narration answer) of the previous answered round, for what is shown again
    unchanged: a paragraph whose sentences (ids and texts) are those that round showed, and the whole narration when
    its fingerprint is the one that round showed. Anything changed gets no default."""
    if not prev:
        return {}, None
    r, ch = prev
    shown_p = gate_dir(run, GATE, r) / SHOWN
    try:
        shown = load_json(shown_p, f"gate0 round {r} {SHOWN}") if shown_p.is_file() else {}
    except Fail:
        shown = {}
    then = {p.get("id"): [(g.get("id"), voice_mod.norm(g.get("text", ""))) for g in p.get("sentences") or []]
            for p in shown.get("paragraphs") or [] if isinstance(p, dict)}
    answers = {row["id"]: row["action"] for row in ch.get("paragraphs") or [] if row.get("action") in ("approve", "edit")}
    out = {}
    for p in paras:
        now = [(g["id"], voice_mod.norm(g["text"])) for g in p["sentences"]]
        if p["id"] in answers and then.get(p["id"]) == now:
            out[p["id"]] = answers[p["id"]]
    n = (ch.get("narration") or {}).get("action")
    return out, (n if n in ("approve", "edit") and ch.get("fingerprint") == fp else None)


def build_spec(run: Path, rnd: int, doc: dict, paras: list[dict], vdoc: dict, alts: list[dict],
               prev: tuple[int, dict] | None, plan: dict | None = None) -> dict:
    """The page. With `plan` (diff_plan against the narration round plan['base_round'] approved, at least one paragraph
    shown) it asks only about the changed paragraphs and has no whole-narration question."""
    n_sent = sum(len(p["sentences"]) for p in paras)
    words = sum(len(g["text"].split()) for p in paras for g in p["sentences"])
    review = (f"voice review {vdoc['status']} (round {vdoc['round']}, score {vdoc['score']}, reviewer "
              f"{vdoc['reviewed_by']})")
    try:
        flags = voice_mod.gate_note(run)
    except voice_mod.Fail as e:
        raise Fail(f"voice: {e}") from e
    if plan is None:
        lead = (f"Round {rnd}. Read the narration the writer made from the source story, then approve or edit every "
                f"paragraph and the whole narration; say in the notes what must change. This page is text only: no "
                f"audio and no pictures yet. Nothing visual is written until this narration is approved.")
    else:
        n = len(plan["shown"])
        lead = (f"Round {rnd}. Round {plan['base_round']} approved the narration. Only what changed since then is "
                f"asked here: approve or edit each of the {n} changed paragraph{'s' if n != 1 else ''} below (each "
                f"lists only its changed sentences, old and new). The unchanged paragraphs keep their approval and are "
                f"listed collapsed at the end of this intro. Say in the notes what else must change. Text only: no "
                f"audio and no pictures yet.")
    intro = [lead, f"**Source story:** `{doc['source']}`",
             f"**Written by:** {html.escape(voice_mod.writer(doc))} · {review}"]
    if alts:
        intro.append("Alternative versions are shown side by side with the current narration under **Version**; "
                     "pick the one to go on with there.")
    asks = previous_asks(prev, vdoc["fingerprint"])   # voice_state: the review's fingerprint is the current one
    if asks:
        intro.append(asks)
    if plan is not None:
        if plan["removed"]:
            intro.append("**Removed since round {}:** ".format(plan["base_round"]) + "; ".join(
                f"paragraph {o['id']} ({html.escape(str(o.get('title', '')))})" for o in plan["removed"]))
        intro.append((flags + "\n\n" if flags else "") + f"Whole narration now: {len(paras)} paragraphs, {n_sent} "
                     f"sentences, {words} words.")
        if plan["unchanged"]:
            intro.append(unchanged_block(plan["unchanged"], plan["base_round"]))
    questions = []
    if alts:
        # plain paragraphs, not a "- " list: a list right above the options would merge into the radio list
        files = "\n\n".join(f"**{a['label']}**: `{a['path']}` (sha256 {a['sha256'][:12]}…)" for a in alts)
        questions.append({
            "id": "version", "type": "single", "prompt": "Version", "required": True, "allowNote": True,
            "body": versions_table(paras, alts) + "\n\n" + files,
            "options": [{"value": "current", "label": "Keep the current narration"}]
                       + [{"value": f"alt:{a['label']}", "label": f"Use {a['label']}"} for a in alts]
                       + [{"value": "merge", "label": "Merge (describe in the note)"}],
            "notePlaceholder": "For a merge: which parts to take from which version"})
    if plan is not None:
        for p, body in plan["shown"]:
            questions.append({
                "id": para_qid(p["id"]), "type": "single", "prompt": f"Paragraph {p['id']}: {p['title']}",
                "required": True, "allowNote": True, "options": DECISION, "body": body,
                "notePlaceholder": f"What should change in paragraph {p['id']} (name the sentence ids)?"})
        return {"title": f"Gate 0 · {doc['title']} · round {rnd}", "slug": f"gate0-r{rnd}", "stageLabel": "Gate 0",
                "intro": "\n\n".join(intro), "questions": questions}
    was_para, was_narration = previous_answers(run, prev, paras, vdoc["fingerprint"])
    for p in paras:
        body = "\n>\n".join(f"> **{g['id']}** {md_text(voice_mod.norm(g['text']))}" for g in p["sentences"])
        questions.append({
            "id": para_qid(p["id"]), "type": "single", "prompt": f"Paragraph {p['id']}: {p['title']}",
            "required": True, "allowNote": True, "options": decision_options(was_para.get(p["id"])), "body": body,
            "notePlaceholder": f"What should change in paragraph {p['id']} (name the sentence ids)?"})
    body = f"{len(paras)} paragraphs, {n_sent} sentences, {words} words."
    if flags:
        body = flags + "\n\n" + body
    questions.append({"id": "narration", "type": "single", "prompt": "Whole narration", "required": True,
                      "allowNote": True, "options": decision_options(was_narration), "body": body,
                      "notePlaceholder": "Notes on the whole narration (story, order, tone, length)"})
    return {"title": f"Gate 0 · {doc['title']} · round {rnd}", "slug": f"gate0-r{rnd}", "stageLabel": "Gate 0",
            "intro": "\n\n".join(intro), "questions": questions}


def cmd_build(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    doc, paras, parts, fp = load_narration(run)
    vdoc = voice_state(run, doc, parts, fp)
    prev = latest_applied(run, a.round)
    last = next((r for r in range(a.round - 1, 0, -1) if (gate_dir(run, GATE, r) / "gate.json").is_file()), None)
    setup_missing = setup_problem(run) if runsetup.required() else None
    if prev and prev[0] == last and prev[1].get("approved_fingerprint") == fp and not setup_missing \
            and not sheets_pending(run):
        # only the newest round counts (scribe.py stage 0c): a later unanswered round voids an earlier approval.
        # A setup run whose sheets are not all approved as they are now still needs a round (the sheets only).
        raise Fail(f"gate 0 round {prev[0]} approved this exact narration; gate 0 is complete")
    unanswered_stem(out)   # refuse an applied or answered round before any work
    alts = parse_alts(a.alt or [])
    base = approved_baseline(run, a.round)
    plan = None
    if base is not None:
        plan = {**diff_plan(base[1], paras, prev, base[0]), "base_round": base[0]}
        if not plan["shown"]:
            plan = None   # nothing to ask paragraph by paragraph (e.g. only voice settings changed): the full page
    spec = build_spec(run, a.round, doc, paras, vdoc, alts, prev, plan)
    record = [{k: x[k] for k in ("label", "path", "sha256", "copy")} for x in alts]
    shown = [p["id"] for p in paras] if plan is None else [p["id"] for p, _ in plan["shown"]]
    meta = {"round": a.round, "fingerprint": fp, "paragraphs": shown,
            "baseline_round": None if plan is None else plan["base_round"],
            "unchanged_paragraphs": [] if plan is None else [p["id"] for p in plan["unchanged"]],
            "voice_status": vdoc["status"], "alts": record}
    if runsetup.required():
        # the narration was recorded in the start page's voice before this page: each paragraph plays its sentences
        import narrate   # imported here: only setup runs play the takes on this page
        try:
            segs = narrate.load_script(run / "script.json", narrate.load_pronunciations(run))
            nst, nvoice = narrate.load_state(run), narrate.voice_key(narrate.engine(), run)
        except narrate.Fail as e:
            raise Fail(f"narration: {e}") from e
        ready = {s["id"] for s in segs if narrate.seg_state(run, s, nst, nvoice) == "ready"}
        by_pid = {p["id"]: p for p in paras}
        for q in spec["questions"]:
            if not q["id"].startswith("paragraph-"):
                continue
            p = by_pid[int(q["id"].split("-", 1)[1])]
            wavs = [(g["id"], run / "narration" / f"segment_{g['id']}.wav") for g in p["sentences"]]
            missing = [gid for gid, w in wavs if gid not in ready]   # recorded for this text and voice, and checked
            if missing:
                raise Fail(f"the narration of sentence(s) {', '.join(missing)} is not recorded for the current text and "
                           f"voice; Gate 1 plays it (follow scribe.py status: the narration takes come first)")
            q["media"] = [{"type": "audio", "path": str(w), "alt": f"sentence {gid}", "caption": f"{gid}"}
                          for gid, w in wavs]
            # what this page plays, so a later page can say which sentences were recorded again since
            meta.setdefault("audio_sha256", {}).update({gid: narrate.file_sha(w) for gid, w in wavs})
        before = runsetup.previous(run) or (prev[1].get("setup") if prev else None) or {}
        try:
            qs, meta["setup"] = runsetup.questions(run, doc, before)
        except runsetup.SetupError as e:
            raise Fail(f"setup: {e}") from e
        try:
            st_ = runsetup.load_start(run)
            v = runsetup.voice(run)
            heard = (f"Play each paragraph: the narration is recorded in **{v['label']}**'s voice (chosen on Gate 0). "
                     f"Your words were {'kept exactly' if st_['words'] == 'keep' else 'rewritten as narration'}.")
        except runsetup.SetupError as e:
            raise Fail(f"setup: {e}") from e
        for txt in (" This page is text only: no audio and no pictures yet.", " Text only: no audio and no pictures yet."):
            spec["intro"] = spec["intro"].replace(txt, "")
        # Gate 0 chose these: shown, not asked
        icat = runsetup.image_models()["models"]
        how = {"description": "a character sheet drawn from the description",
               "photos": "a character sheet drawn from {n} image file(s)", "none": "no character sheet"}
        cast_lines = "".join(f"\n- **{c['name']}**: " + how[c["mode"]].format(n=len(c.get("photos") or []))
                             for c in st_["cast"].values())
        parts = [f"title **{st_['title']}**", f"image model **{icat[st_['image_model']]['label']}**",
                 f"drawing style **{runsetup.styles()[st_['style']]['label']}**",
                 f"colours **{runsetup.palettes()[st_['palette']]['label']}**"]
        chosen = (f"\n\n**Chosen on Gate 0** (these do not change for this video): " + "; ".join(parts) +
                  (f"; characters:{cast_lines}" if cast_lines else "; no characters."))
        sheet_qs, meta["sheets"] = sheet_questions(run, doc, prev)   # drawn before this page; approved here
        spec["intro"] += ("\n\n" + heard + chosen + "\n\n**Setup.** Below the script, this page "
                          + ("shows each character sheet, drawn in the chosen style and colours, for you to approve, "
                             "and " if sheet_qs else "")
                          + "asks the credit budget. No scene is paid for until this page is answered."
                          + (f"\n\n**Setup still needed:** {setup_missing}" if setup_missing and prev else "")
                          + (f"\n\n**Round {prev[0]}'s setup answers need fixing:** " + "; ".join(prev[1]["setup_problems"])
                             if prev and prev[1].get("setup_problems") else ""))
        spec["questions"] += sheet_qs + qs
    result = build_bundle(spec, out, meta)
    shutil.copyfile(run / voice_mod.NARRATION, out / SHOWN)
    for f in out.glob("alt-*.md"):
        if f.name not in {x["copy"] for x in alts}:
            f.unlink()
    for x in alts:
        shutil.copyfile(x["path"], out / x["copy"])
    write_json(out / "alts.json", {"round": a.round, "alts": record})
    info = {"round": a.round, "html": result["html"], "url": result["url"], "bundle": result["bundle"],
            "expectedAnswers": result["expectedAnswers"], "fingerprint": fp, "paragraphs": meta["paragraphs"],
            "baseline_round": meta["baseline_round"], "unchanged_paragraphs": meta["unchanged_paragraphs"],
            "rebuilt": result["rebuilt"], "alts": [x["label"] for x in alts], "voice_status": vdoc["status"],
            **open_page(result["url"], a.no_open)}
    emit("GATE0", info)
    return 0


def sheets_pending(run: Path) -> bool:
    """Setup runs: is there a character sheet that no Gate 1 round approved as it is now (a redraw after an edit, or
    a sheet never shown)? Then Gate 1 needs another round even though the narration is approved."""
    if not runsetup.required():
        return False
    import gate4
    now = gate4.sheet_shas(run)
    if not now:
        return False
    rec = gate4.gate1_cast_record(run)
    if rec is None or rec[1].get("approved") is not True:
        return True
    approved = rec[1].get("approved_sha256") or {}
    return {k: approved.get(k) for k in now} != now


def sheet_questions(run: Path, doc: dict, prev) -> tuple[list[dict], dict]:
    """One approve / edit question per character sheet (drawn in the run's style and colours before this page), with
    the image files it was drawn from. Refused while a sheet is not ready (made and passed its sheet review):
    scribe.py routes the sheets first."""
    import gate4       # the sheet helpers
    import characters  # sheet_ready: made through Higgsfield, sheet review passed
    try:
        ids = runsetup.sheet_ids(run)
        cast = runsetup.choices(run)["cast"]
    except runsetup.SetupError as e:
        raise Fail(f"setup: {e}") from e
    notready = [cid for cid in ids if not characters.sheet_ready(run, cid)]
    if notready:
        raise Fail(f"character sheet(s) not ready (made and passed the sheet review): {', '.join(notready)}; Gate 1 "
                   f"shows them, so they come first (follow scribe.py status)")
    names = {c["id"]: c["name"] for c in doc.get("characters") or []}
    qs, qmap, shas = [], {}, {}
    for cid in ids:
        q = runsetup.qid("sheet", cid)
        qmap[q] = cid
        shas[cid] = gate4.sheet_sha(run, cid)
        name = names.get(cid, cast[cid]["name"])
        media, gone = gate4.sheet_media(run, cid, name, cast[cid].get("photos") or [])
        qs.append({"id": q, "type": "single", "prompt": f"Character sheet: {name}", "required": True,
                   "allowNote": True, "options": decision_options(gate4.previous_sheet_answer(run, cid, shas[cid])),
                   "media": media,
                   "body": (f"{cast[cid]['look']}\n\nDrawn in the chosen style and colours from "
                            + (f"{len(cast[cid]['photos'])} image file(s)" if cast[cid].get("photos") else
                               "the description")
                            + ". Every scene that shows this character is drawn from this sheet." + gone),
                   "notePlaceholder": "What must the next sheet change?"})
    return qs, {"questions": qmap, "shown_sha256": shas}


def cmd_apply(a) -> int:
    run = Path(a.run).resolve()
    out = gate_dir(run, GATE, a.round)
    responses, pointer = read_answers(out)
    paragraphs, missing = [], []
    for pid in pointer["paragraphs"]:
        action, note = decision(responses, para_qid(pid))
        if action == "edit" and not note.strip():
            missing.append(f"paragraph {pid} is marked edit without a note")
        paragraphs.append({"id": pid, "action": action, "note": note})
    notes = response(responses, "notes")["value"]
    sheet_rows = []
    for q, cid in ((pointer.get("sheets") or {}).get("questions") or {}).items():
        action, note = decision(responses, q)
        if action == "edit" and not note.strip():
            missing.append(f"the character sheet of {cid} is marked edit without a note")
        sheet_rows.append({"id": cid, "action": action, "note": note})
    if pointer.get("baseline_round"):   # a changes-only page has no whole-narration question
        n_action, n_note = "approved_earlier", ""
    else:
        n_action, n_note = decision(responses, "narration")
        if n_action == "edit" and not (n_note.strip() or notes.strip()):
            missing.append("the whole narration is marked edit without a note or general notes")
    version, version_note, version_file = "current", "", None
    if pointer["alts"]:
        r = response(responses, "version")
        version, version_note = r["value"], r.get("note", "")
        if version == "merge" and not version_note.strip():
            missing.append("version is merge without a note saying what to take from which version")
        if version.startswith("alt:"):
            label = version[4:]
            version_file = next((out / x["copy"]).as_posix() for x in pointer["alts"] if x["label"] == label)
    if missing:
        raise Fail("; ".join(missing) + ". The notes are the writer's instructions: ask the user for them, add them to "
                   "the answers and apply again")
    approved = (n_action in ("approve", "approved_earlier") and version == "current"
                and all(p["action"] == "approve" for p in paragraphs))
    setup, problems = None, []
    if runsetup.required():
        if "setup" not in pointer:
            raise Fail(f"gate 0 round {a.round} was built without the setup questions; build the next round")
        shown_doc = load_json(out / SHOWN, f"gate0 round {a.round} {SHOWN}")
        try:
            setup, problems = runsetup.from_answers(run, responses, pointer["setup"], shown_doc, a.round,
                                                    pointer["fingerprint"])
        except runsetup.SetupError as e:
            raise Fail(f"setup: {e}") from e
        if problems and approved:   # an approving round must leave a complete setup; never guess an answer
            raise Fail("setup answers: " + "; ".join(problems) + ". Ask the user to correct them on the page (never "
                       "guess an answer), paste the new answers and apply again")
    changes = {"round": a.round, "approved": approved, "approved_fingerprint": pointer["fingerprint"] if approved else None,
               "fingerprint": pointer["fingerprint"], "version": version, "version_note": version_note,
               "version_file": version_file, "narration": {"action": n_action, "note": n_note},
               "paragraphs": paragraphs, "baseline_round": pointer.get("baseline_round"),
               "unchanged_paragraphs": pointer.get("unchanged_paragraphs", []), "notes": notes}
    if setup is not None:
        changes["setup"] = setup
        changes["setup_problems"] = problems   # only on a round that asked for narration edits; the next round shows them
    if pointer.get("sheets"):   # setup runs: the sheets' approval (gate4.gate1_cast_record reads it)
        shown = pointer["sheets"]["shown_sha256"]
        ok = all(r["action"] == "approve" for r in sheet_rows)
        changes["cast"] = {"approved": ok, "approved_sha256": shown if ok else {}, "shown_sha256": shown,
                           "characters": sheet_rows}
    write_json(out / "changes.json", changes)
    if setup is not None and approved:
        runsetup.write(run, setup)   # after changes.json: setup.json must match an approved round's record (provenance)
    state = mark_gate(out, pointer, approved and all(r["action"] == "approve" for r in sheet_rows))
    emit("APPLY", {"round": a.round, "changes": (out / "changes.json").as_posix(), "approved": approved,
                   "version": version, "edit_paragraphs": [p["id"] for p in paragraphs if p["action"] == "edit"],
                   "narration": n_action, "has_notes": bool(notes.strip()), "gate_state": state,
                   "sheets": {r["id"]: r["action"] for r in sheet_rows},
                   "all_passed": approved and all(r["action"] == "approve" for r in sheet_rows)})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--run", required=True)
    b.add_argument("--round", type=int, required=True)
    b.add_argument("--alt", action="append", metavar="LABEL=PATH",
                   help="an alternative narration (Markdown) to show beside the current one; repeatable")
    b.add_argument("--no-open", action="store_true", help="do not open the page in Chrome")
    p = sub.add_parser("apply")
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        return {"build": cmd_build, "apply": cmd_apply}[a.cmd](a)
    except (voice_mod.Fail, voice_mod.rs().Fail) as exc:   # voice.py / render_scenes failures: same FAILED line
        raise Fail(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(run_main(main))
