#!/usr/bin/env python
r"""voice.py - the story's voice review: a separate review agent checks the spoken text against the voice
profile (settings voice_profile) before Gate 0 (and before narration). Nobody who wrote the text reviews it.

What is reviewed: RUN/narration.json when it exists (the Gate 0 flow: the writer's pass 1, narration text only), else
RUN/script.json (legacy runs written before Gate 0). The writer agent (references/SCRIPT-WRITER.md) names itself in
`written_by`. A review agent with a different name (references/VOICE-REVIEW.md) fills a checklist: nine global
checks, each with evidence, and a verdict on every sentence, with a rewritten sentence as the fix for each flagged
one. This script validates the checklist and computes the score; the reviewer never types a score.

  validate-narration --run RUN
               check RUN/narration.json (the writer's pass 1) against the contract; NARRATION_JSON.
  review-prep  --run RUN --agent NAME
               refuse NAME when it is the reviewed file's written_by or the written_by of any earlier round (names
               compared case- and whitespace-insensitively); write RUN/voice/r<K>/checklist.json (every
               sentence pre-listed) and RUN/voice/r<K>/narration.json (or script.json: the reviewed version). After a
               failed round, a revision that leaves any flagged sentence's text unchanged (whitespace aside) is
               refused, and `status` sends it back to the writer. `hints` lists sentences with spoken digits or
               "um": the verdict refuses numbers_spoken_as_words / fillers_performable = true while any exist.
  verdict      --run RUN --file FILLED
               validate the filled checklist (fingerprint, round, profile version, reviewer, check keys,
               sentence ids/texts, types, fix sentences) and score it; write RUN/voice/voice.json. VOICE_JSON.
  status       --run RUN     status and NEXT action; VOICE_JSON (also before the first review)

narration.json / script.json (writer-owned): `written_by` (required) and optional `voice`
  {"profile": DEFAULT_PROFILE, "opening": false, "signoff": false}; those are the defaults. opening/signoff true
  switch the profile's opening greeting / sign-off on. narration.json:
  {"title", "source", "written_by", "voice"?, "paragraphs": [{"id": int, "title", "sentences": [{"id": "P.S",
  "text"}]}]}; its sentences follow the script's segment rules (one sentence) and the segment balance band:
  render_scenes.SEGMENT_BALANCE_WORDS (6-16 words) each, aiming at SEGMENT_TARGET_WORDS (8-14), checked by
  validate-narration and review-prep (new text; status and Gate 0 check only the structure). A sentence the user
  already approved at Gate 0 (gate0.approved_baseline) is exempt. verdict holds a flag's fix to the same band.
  NARRATION_JSON's segment_words reports min/median/max, the sentences outside the target (inside the band;
  informational) and the exempt ones.

Fingerprint: what a review judged, the sha256 of canonical JSON of {sentences [[id, text], ...] in story order
(paragraphs / scenes by id) with each text whitespace-normalized (norm), voice (the effective profile path, opening,
signoff), source_sha256 (the source story file), profile_version (the profile's `Version:` line)}. voice.json holds
while the current fingerprint equals its `fingerprint`; written_by, non-sentence fields (title, title_text, elements,
layout hints) and whitespace do not affect it. So a script.json built from a reviewed narration.json (same sentence
ids and texts, voice and source) has the same fingerprint.

Score (VOICE_DEDUCT, the render_scenes.score_checklist style): start at 100 and subtract the check's weight for
every failed global check and FLAGGED_SENTENCE for every flagged sentence; floor 0. Pass = no failed check and
no flagged sentence (the score is the record of how far off a failed round was).

Rounds: a failed round sends the script back to its writer, and the revision is reviewed in the next round. A
revision that only changed the flagged sentences (same sentence ids, every unflagged sentence and the source
unchanged) continues the failed streak; any other change is a new script and restarts it. The MAX_ROUNDS-th
failed round in a row is `flagged`: the run continues and Gate 1's script question lists the flags. A pass or
flag ends the streak; a later change to the fingerprint makes voice.json stale and needs a new review.

voice.json (RUN/voice/, owned by this script; read-only for others):
  {"status": "passed"|"failed"|"flagged", "round", "fingerprint", "fingerprint_parts": {"sentences", "voice",
   "source_sha256", "profile_version"}, "source", "profile", "profile_version", "reviewed_by", "written_by",
   "score", "deductions", "failed_checks": [{"check", "evidence"}], "flags": [{"id", "text", "issue", "fix"}],
   "worstIssue", "failed_streak", "max_rounds", "time",
   "history": [{"round", "status", "score", "fingerprint", "reviewed_by", "written_by"}]}
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

import runsetup         # scripts/runsetup.py: with setup required, narration.json lists its characters
import settings as cfg   # scripts/settings.py: voice_profile

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = cfg.path("voice_profile").as_posix() if cfg.path("voice_profile") else None   # settings voice_profile
VOICE_DEFAULTS = {"profile": DEFAULT_PROFILE, "opening": False, "signoff": False}
MAX_ROUNDS = 3             # the 3rd failed review in a row flags the script and the run continues
FLAGGED_SENTENCE = 5       # points per flagged sentence
# Points per failed global check. Facts outweigh style: a story that drifts from its source is the worst miss.
VOICE_DEDUCT = {"cadence_natural": 15, "short_spoken_beats": 10, "no_hype_or_corporate": 15, "no_ai_polish": 15,
                "fillers_performable": 15, "facts_preserved": 25, "nothing_invented": 25,
                "opening_signoff_as_configured": 10, "numbers_spoken_as_words": 10}
CHECKS = {
    "cadence_natural": "Sounds like the narrator the voice profile describes: its cadence and spoken markers where "
                       "they fit; not stiff, scripted or parody.",
    "short_spoken_beats": "Short spoken beats; no long, stacked or written-style sentences.",
    "no_hype_or_corporate": "Plain, grounded enthusiasm; no hype (mind-blowing, revolutionary, insane, game-changing), "
                            "fake urgency or corporate phrasing.",
    "no_ai_polish": "No AI polish: no tidy triads, no 'it's not X, it's Y' flourishes, no over-smooth transitions.",
    "fillers_performable": "Fillers a TTS voice can perform: an occasional 'uh' at most, never 'um', no repeated "
                           "loops, no filler in every sentence.",
    "facts_preserved": "Every fact matches the source story: who, what, when, the order of events, numbers.",
    "nothing_invented": "Nothing is added that the source does not say: no new events, claims, names or details.",
    "opening_signoff_as_configured": "Opening and sign-off exactly as `config` says (false = none of the profile's "
                                     "opening greeting or sign-off); no channel or sponsor plugs, no screen-demo "
                                     "narration ('I'm going to click...'), no bracketed stage directions.",
    "numbers_spoken_as_words": "Every number the narrator says is written as words (twenty twenty-two, not 2022).",
}
STATUSES = ("passed", "failed", "flagged")
SPOKEN_DIGITS = re.compile(r"(?<![A-Za-z\d])\d+(?:[.,:]\d+)*(?![A-Za-z\d])")   # 2022, 5-year-old; not 3D or 4K
UM = re.compile(r"\bum\b", re.I)

_RS = None


def rs():
    """render_scenes.py as a module: its validator, sha, lock, log and sentence rules are reused, never copied."""
    global _RS
    if _RS is None:
        spec = importlib.util.spec_from_file_location("render_scenes", REPO / "scripts" / "render_scenes.py")
        _RS = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RS)
    return _RS


class Fail(Exception):
    pass


def vdir(run: Path) -> Path:
    return run / "voice"


def rdir(run: Path, k: int) -> Path:
    return vdir(run) / f"r{k}"


NARRATION = "narration.json"
SENTENCE_ID = re.compile(r"^(\d+)\.(\d+)$")


def reviewed_path(run: Path) -> Path:
    """What the voice review judges: RUN/narration.json (Gate 0 flow) when it exists, else RUN/script.json (legacy)."""
    for name in (NARRATION, "script.json"):
        if (run / name).is_file():
            return run / name
    raise Fail(f"no {(run / NARRATION).as_posix()}; the writer agent writes it first "
               f"(references/SCRIPT-WRITER.md, pass 1)")


def approved_norms(run: Path | None) -> set[str]:
    """The whitespace-normalized sentences of the run's newest Gate 0-approved narration, resolved by gate0.py's own
    approved_baseline (changes.json approved_fingerprint -> narration.shown.json, else voice/r<K>/narration.json).
    Empty without a run or an approved round."""
    rounds = [int(p.name.rsplit("-r", 1)[1]) for p in (run / "gates").glob("gate0-r*")
              if p.is_dir() and p.name.rsplit("-r", 1)[1].isdigit()] if run else []
    if not rounds:
        return set()
    import gate0   # lazy: gate0 imports this module
    try:
        base = gate0.approved_baseline(run, max(rounds) + 1)
    except gate0.Fail as e:
        raise Fail(f"validate-narration: {e}") from e
    return {norm(g["text"]) for g in sentences(base[1])} if base else set()


def out_of_band(text: str) -> bool:
    lo, hi = rs().SEGMENT_BALANCE_WORDS
    return not lo <= len(text.split()) <= hi


def keep_words(run: Path) -> bool:
    """The start page chose "keep my words": no rewriting, so no voice review; the text is checked word for word."""
    if not runsetup.required():
        return False
    try:
        return runsetup.load_start(run)["words"] == "keep"
    except runsetup.SetupError:
        return False


def validate_narration(doc, run: Path | None = None, balance: bool = False) -> list[dict]:
    """narration.json against the contract; its paragraphs sorted by id. Sentences follow the script's segment rules.
    balance: also hold every sentence to the segment balance band (the writer's command and review-prep: new text),
    except a sentence whose text (norm) the user already approved at Gate 0 in `run`. Without balance (status,
    Gate 0) only the structure is checked, so approved and legacy runs keep working."""
    R = rs()
    approved = approved_norms(run) if balance else set()
    if not isinstance(doc, dict):
        raise Fail("validate-narration: narration.json must be a JSON object")
    errs = []
    keep = run is not None and keep_words(run)
    if keep:   # "keep my words" (start page): the text must be the user's script word for word; no balance band
        balance = False
        try:
            errs += runsetup.verbatim_problems(doc, runsetup.source_text(run))
        except OSError as e:
            errs.append(f"the script file cannot be read: {e}")
    for key in ("title", "written_by"):
        if not (isinstance(doc.get(key), str) and doc[key].strip()):
            errs.append(f"{key} must be a non-empty string" + (" (the writer agent's name)" if key == "written_by" else ""))
    src = doc.get("source")
    if not (isinstance(src, str) and Path(src).is_absolute() and Path(src).is_file()):
        errs.append(f"source must be the source story's absolute path (an existing file): {src!r}")
    voice = doc.get("voice")
    if voice is not None:
        if not isinstance(voice, dict) or set(voice) - set(VOICE_DEFAULTS):
            errs.append("voice must be an object with only profile, opening and signoff")
        else:
            if "profile" in voice and not (isinstance(voice["profile"], str) and voice["profile"].strip()):
                errs.append("voice.profile must be the voice profile's path")
            errs += [f"voice.{k} must be true or false" for k in ("opening", "signoff")
                     if k in voice and not isinstance(voice[k], bool)]
    if runsetup.required():   # every recurring character, exactly as the start page asked about them
        try:
            chars = runsetup.narration_cast(doc)
            if run is not None:
                want = runsetup.start_cast(run)
                if chars != want:
                    errs.append("characters must be exactly the start page's, copied from start.json cast (same ids, "
                                "names and looks, same order): " + json.dumps(want, ensure_ascii=False))
                title = runsetup.start_title(run)
                if doc.get("title") != title:
                    errs.append(f"title must be the title the user gave on the start page, exactly: {title!r} "
                                f"(start.json title)")
        except runsetup.SetupError as e:
            errs.append(str(e))
    paras = doc.get("paragraphs")
    if not isinstance(paras, list) or not paras or not all(isinstance(p, dict) for p in paras):
        raise Fail("validate-narration:\n  " + "\n  ".join(errs + ["paragraphs must be a non-empty list of objects"]))
    pids = [p.get("id") for p in paras]
    if len(set(map(repr, pids))) != len(pids) or not all(type(i) is int and i > 0 for i in pids):
        errs.append("paragraph ids must be unique positive integers")
    seen = set()
    for p in paras:
        pid, sents = p.get("id"), p.get("sentences")
        if not (isinstance(p.get("title"), str) and p["title"].strip()):
            errs.append(f"paragraph {pid}: needs a title")
        if not isinstance(sents, list) or not sents or not all(isinstance(g, dict) for g in sents):
            errs.append(f"paragraph {pid}: sentences must be a non-empty list of objects")
            continue
        for g in sents:
            gid, text = g.get("id"), g.get("text")
            m = SENTENCE_ID.match(gid) if isinstance(gid, str) else None
            if not m or int(m.group(1)) != pid or int(m.group(2)) < 1:
                errs.append(f"paragraph {pid}: sentence id {gid!r} must be \"{pid}.<n>\" (n >= 1)")
            elif gid in seen:
                errs.append(f"sentence id {gid} is used twice")
            seen.add(gid)
            if not isinstance(text, str) or not R.one_sentence(text):
                errs.append(f"sentence {gid}: must be exactly one sentence: {text!r}")
            elif balance and out_of_band(text) and norm(text) not in approved:
                n, (lo, hi) = len(text.split()), R.SEGMENT_BALANCE_WORDS
                if n < lo:
                    errs.append(f"sentence {gid}: {n} word{'s' * (n != 1)}, under the segment balance band {lo}-{hi}; "
                                f"merge it into a neighbouring sentence")
                else:
                    errs.append(f"sentence {gid}: {n} words, over the segment balance band {lo}-{hi}; split it")
            elif len(text.split()) > R.MAX_SEGMENT_WORDS:
                errs.append(f"sentence {gid}: {len(text.split())} words, over {R.MAX_SEGMENT_WORDS}")
    if errs:
        raise Fail("validate-narration:\n  " + "\n  ".join(errs))
    return sorted(paras, key=lambda p: p["id"])


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def load(run: Path) -> dict | None:
    p = vdir(run) / "voice.json"
    if not p.is_file():
        return None
    doc = read_json(p)
    if "fingerprint" not in doc:
        raise Fail(f"{p.as_posix()} has no fingerprint (written before contract v1.5's fingerprint); migrate it")
    return doc


def save(run: Path, doc: dict) -> None:
    tmp = vdir(run) / "voice.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, vdir(run) / "voice.json")


def log(run: Path, event: str, **fields) -> None:
    rs().log_event(vdir(run), event, **fields)


def voice_config(script: dict) -> dict:
    return {**VOICE_DEFAULTS, **(script.get("voice") or {})}


def profile_version(path: str | None) -> str:
    if not path:
        raise Fail("no voice profile: set voice_profile in settings.local.json, or voice.profile in narration.json / "
                   "script.json")
    p = Path(path)
    if not p.is_file():
        raise Fail(f"voice profile {path} not found (narration.json / script.json voice.profile, default {DEFAULT_PROFILE} "
                   f"from {cfg.where('voice_profile')})")
    m = re.search(r"^Version:\s*(\S.*?)\s*$", p.read_text(encoding="utf-8"), re.M)
    if not m:
        raise Fail(f"voice profile {path} has no 'Version:' line")
    return m.group(1)


def writer(script: dict) -> str:
    return str(script.get("written_by") or "").strip()


def norm(text: str) -> str:
    """Text as spoken: whitespace runs collapsed, ends trimmed. A whitespace-only edit is no edit."""
    return " ".join(str(text).split())


def name_key(name: str) -> str:
    return norm(name).casefold()


def writers(doc: dict | None, script: dict, *more: str) -> list[str]:
    """Every name that wrote this script: written_by now, in every recorded round, and `more` (normalized, one per
    name_key)."""
    names = [writer(script), *(h["written_by"] for h in (doc or {}).get("history", [])), *more]
    return sorted({name_key(n): norm(n) for n in names if n and norm(n)}.values(), key=str.casefold)


def wrote(name: str, names: list[str]) -> bool:
    return name_key(name) in {name_key(n) for n in names}


def sentences(doc: dict) -> list[dict]:
    """Every sentence in story order: narration.json paragraphs or script.json scenes (by id), then as listed."""
    if "paragraphs" in doc:
        return [{"id": g["id"], "text": g["text"]} for p in sorted(doc["paragraphs"], key=lambda p: p["id"])
                for g in p["sentences"]]
    return [{"id": g["id"], "text": g["text"]} for s in sorted(doc["scenes"], key=lambda s: s["id"])
            for g in s["segments"]]


def source_path(doc: dict) -> Path:
    source = str(doc.get("source") or "")
    if not source or not Path(source).is_file():
        raise Fail(f"source {source!r} is not a file; the voice review checks facts against it")
    return Path(source)


def fingerprint_parts(script: dict) -> dict:
    cfg = voice_config(script)
    return {"sentences": [[g["id"], norm(g["text"])] for g in sentences(script)],
            "voice": {k: cfg[k] for k in ("profile", "opening", "signoff")},
            "source_sha256": rs().sha256(source_path(script)),
            "profile_version": profile_version(cfg["profile"])}


def fingerprint(parts: dict) -> str:
    canon = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def stale_parts(doc: dict | None, parts: dict) -> list[str]:
    return [] if doc is None else [k for k in parts if doc["fingerprint_parts"].get(k) != parts[k]]


def unapplied_flags(doc: dict | None, parts: dict) -> list[str]:
    """Ids the failed round flagged whose text (normalized) is still the same in `parts`: the writer's turn."""
    if doc is None or doc["status"] != "failed":
        return []
    now = {i: t for i, t in parts["sentences"]}
    return [f["id"] for f in doc["flags"] if f["id"] in now and norm(now[f["id"]]) == norm(f["text"])]


def continues_streak(doc: dict | None, parts: dict) -> bool:
    """Is `parts` the failed round's script with only its flagged sentences revised? Same sentence ids in the same
    order, every unflagged sentence (normalized) and the source unchanged. Anything else is a new script (a fresh
    streak)."""
    if doc is None or doc["status"] != "failed":
        return False
    prev = doc["fingerprint_parts"]
    flagged = {f["id"] for f in doc["flags"]}
    if [i for i, _ in prev["sentences"]] != [i for i, _ in parts["sentences"]]:
        return False
    if prev["source_sha256"] != parts["source_sha256"]:
        return False
    return all(norm(a) == norm(b) for (i, a), (_, b) in zip(prev["sentences"], parts["sentences"]) if i not in flagged)


def status_of(doc: dict | None, fp: str) -> str:
    if doc is None:
        return "not reviewed"
    return "stale" if doc["fingerprint"] != fp else doc["status"]


def next_action(doc: dict | None, parts: dict, script: dict, name: str = "script.json") -> str:
    """A failed round stays with the writer while any flagged sentence is unchanged, even if other edits made the
    fingerprint stale (review-prep would refuse the revision). `name` is the reviewed file."""
    if not writer(script):
        return f"set {name} written_by (the writer agent's name)"
    s = status_of(doc, fingerprint(parts))
    if s in ("passed", "flagged"):
        return "done"
    if unapplied_flags(doc, parts):
        return f"revise {name}: apply round {doc['round']}'s flags (writer agent)"
    return f"review round {(doc['round'] if doc else 0) + 1}"


def gate_note(run: Path) -> str:
    """Markdown for the Gate 0 narration / Gate 1 script question: the open flags when the reviewed text (narration.json,
    or script.json in a legacy run) was flagged, else ""."""
    doc = load(run)
    if doc is None or doc["status"] != "flagged":
        return ""
    if doc["fingerprint"] != fingerprint(fingerprint_parts(read_json(reviewed_path(run)))):
        return ""
    lines = [f"**Voice review flagged:** {doc['failed_streak']} review rounds in a row found problems (last score "
             f"{doc['score']}, reviewer {doc['reviewed_by']}, profile v{doc['profile_version']})."]
    lines += [f"- {c['check']}: {c['evidence']}" for c in doc["failed_checks"]]
    lines += [f"- {f['id']} \u201c{f['text']}\u201d: {f['issue'].rstrip('.')}. Suggested: \u201c{f['fix']}\u201d"
              for f in doc["flags"]]
    return "\n".join(lines)


# ------------------------------------------------------------------ commands

def cmd_validate_narration(a) -> int:
    run = Path(a.run).resolve()
    p = run / NARRATION
    if not p.is_file():
        raise Fail(f"validate-narration: no {p.as_posix()}")
    try:
        doc = read_json(p)
    except json.JSONDecodeError as e:
        raise Fail(f"validate-narration: {p.as_posix()} is not valid JSON: {e}") from e
    paras = validate_narration(doc, run, balance=True)
    R = rs()
    approved = approved_norms(run)
    words = [(g["id"], len(g["text"].split())) for q in paras for g in q["sentences"]]
    counts = [n for _, n in words]
    lo, hi = R.SEGMENT_TARGET_WORDS
    print("NARRATION_JSON " + json.dumps({
        "file": p.as_posix(), "title": doc["title"], "written_by": writer(doc), "paragraphs": len(paras),
        "sentences": len(words), "fingerprint": fingerprint(fingerprint_parts(doc)),
        "segment_words": {"min": min(counts), "median": statistics.median(counts), "max": max(counts),
                          "target": list(R.SEGMENT_TARGET_WORDS), "band": list(R.SEGMENT_BALANCE_WORDS),
                          "outside_target": [i for i, n in words if not lo <= n <= hi
                                             and R.SEGMENT_BALANCE_WORDS[0] <= n <= R.SEGMENT_BALANCE_WORDS[1]],
                          "exempt": [g["id"] for q in paras for g in q["sentences"]
                                     if out_of_band(g["text"]) and norm(g["text"]) in approved]}},
        ensure_ascii=False))
    return 0


def cmd_status(a) -> int:
    run = Path(a.run).resolve()
    sp = reviewed_path(run)
    script = read_json(sp)
    parts = fingerprint_parts(script)
    fp = fingerprint(parts)
    doc = load(run)
    s, nxt = status_of(doc, fp), next_action(doc, parts, script, sp.name)
    changed = stale_parts(doc, parts) if s == "stale" else []
    print(f"voice  {s}{' (' + ', '.join(changed) + ' changed)' if changed else ''}  round {doc['round'] if doc else 0}  "
          f"{sp.name}  written_by {writer(script) or '-'}  NEXT: {nxt}")
    print("VOICE_JSON " + json.dumps({
        "exists": doc is not None, "status": s, "recorded_status": doc["status"] if doc else None,
        "stale": s == "stale", "stale_parts": changed, "next": nxt, "round": doc["round"] if doc else 0,
        "file": sp.name, "fingerprint": fp, "written_by": writer(script) or None,
        "reviewed_by": doc["reviewed_by"] if doc else None,
        "score": doc["score"] if doc else None, "failed_streak": doc["failed_streak"] if doc else 0,
        "profile_version": parts["profile_version"],
        "failed_checks": doc["failed_checks"] if doc else [], "flags": doc["flags"] if doc else [],
        "voice_json": (vdir(run) / "voice.json").as_posix()}, ensure_ascii=False))
    return 0


def cmd_review_prep(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    sp = reviewed_path(run)
    script = read_json(sp)
    what = sp.name
    try:
        if what == NARRATION:
            validate_narration(script, run, balance=True)
        else:
            R.validate(read_json(sp))
    except (Fail, R.Fail) as e:
        raise Fail(f"review-prep: {what} does not validate; the writer fixes it first:\n{e}")
    wb, agent = writer(script), a.agent.strip()
    if not wb:
        raise Fail(f"review-prep: {what} has no written_by; the writer agent sets it to its own name")
    if not agent:
        raise Fail("review-prep: --agent is empty")
    cfg = voice_config(script)
    parts = fingerprint_parts(script)
    fp = fingerprint(parts)
    source = source_path(script)
    vdir(run).mkdir(exist_ok=True)
    with R.locked(vdir(run)):
        doc = load(run)
        names = writers(doc, script)
        if wrote(agent, names):
            raise Fail(f"review-prep: '{agent}' wrote this text (writers so far: {', '.join(names)}); a separate "
                       f"review agent reviews it")
        s = status_of(doc, fp)
        if s in STATUSES:
            raise Fail(f"review-prep: the voice review is '{s}' for this exact text; next: "
                       f"{next_action(doc, parts, script, what)}")
        same = unapplied_flags(doc, parts)
        if same:
            raise Fail(f"review-prep: round {doc['round']} flagged {', '.join(f['id'] for f in doc['flags'])}; the "
                       f"revision leaves {', '.join(same)} unchanged (whitespace aside). Apply each flag's fix "
                       f"(references/SCRIPT-WRITER.md)")
        streak = continues_streak(doc, parts)
        k = (doc["round"] if doc else 0) + 1
        d = rdir(run, k)
        d.mkdir(exist_ok=True)
        shutil.copyfile(sp, d / what)
        sents = sentences(script)
        checklist = {
            "round": k, "script": sp.as_posix(), "file": what, "fingerprint": fp, "source": source.as_posix(),
            "profile": cfg["profile"], "profile_version": parts["profile_version"],
            "config": {"opening": cfg["opening"], "signoff": cfg["signoff"]},
            "written_by": wb, "reviewed_by": agent,
            "hints": {"sentences_with_digits": [g["id"] for g in sents if SPOKEN_DIGITS.search(g["text"])],
                      "sentences_with_um": [g["id"] for g in sents if UM.search(g["text"])]},
            "checks": {key: {"question": q, "ok": None, "evidence": ""} for key, q in CHECKS.items()},
            "sentences": [{**g, "verdict": None, "issue": "", "fix": ""} for g in sents],
            "worstIssue": "",
        }
        (d / "checklist.json").write_text(json.dumps(checklist, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(run, "review_prep", round=k, reviewer=agent, written_by=wb, fingerprint=fp,
        profile_version=parts["profile_version"], continues_streak=streak)
    print("VOICE_REVIEW_JSON " + json.dumps({
        "round": k, "review_dir": d.as_posix(), "checklist": (d / "checklist.json").as_posix(),
        "filled": (d / "filled.json").as_posix(), "script": sp.as_posix(), "file": what, "source": source.as_posix(),
        "profile": cfg["profile"], "profile_version": parts["profile_version"], "config": checklist["config"],
        "sentences": len(sents), "written_by": wb, "reviewed_by": agent, "continues_streak": streak},
        ensure_ascii=False))
    return 0


def validate_filled(c: dict, t: dict, names: list[str]) -> None:
    R = rs()
    errs = []
    for key in ("round", "fingerprint", "profile_version"):
        if c.get(key) != t[key]:
            errs.append(f"{key} is {c.get(key)!r}, the review round has {t[key]!r}")
    who = str(c.get("reviewed_by") or "").strip()
    if who != t["reviewed_by"]:
        errs.append(f"reviewed_by is {who!r}; review-prep was run for {t['reviewed_by']!r}")
    if wrote(who, names):
        errs.append(f"reviewed_by {who!r} wrote this script (writers so far: {', '.join(names)}); a separate agent "
                    f"reviews it")
    checks = c.get("checks")
    if not isinstance(checks, dict) or list(checks) != list(CHECKS):
        errs.append(f"checks must keep exactly these keys, in order: {', '.join(CHECKS)}")
        checks = {}
    for key, v in checks.items():
        if not isinstance(v, dict) or not isinstance(v.get("ok"), bool):
            errs.append(f"checks.{key}.ok must be true or false")
        elif not str(v.get("evidence") or "").strip():
            errs.append(f"checks.{key}.evidence is empty: quote the sentence(s) or say what you checked")
    sents = c.get("sentences")
    if not isinstance(sents, list) or [(g.get("id"), g.get("text")) for g in sents if isinstance(g, dict)] != \
            [(g["id"], g["text"]) for g in t["sentences"]]:
        errs.append("sentences must keep every segment's id and text unchanged, in order")
        sents = []
    for g in sents:
        v, issue, fix = g.get("verdict"), str(g.get("issue") or "").strip(), str(g.get("fix") or "").strip()
        if v == "ok":
            if issue or fix:
                errs.append(f"sentence {g['id']}: verdict ok takes no issue or fix (flag it instead)")
        elif v == "flag":
            if not issue:
                errs.append(f"sentence {g['id']}: a flag needs the issue")
            if not fix:
                errs.append(f"sentence {g['id']}: a flag needs the fix, the rewritten sentence")
            elif not R.one_sentence(fix) or not (R.SEGMENT_BALANCE_WORDS[0] <= len(fix.split())
                                                 <= R.SEGMENT_BALANCE_WORDS[1]):
                errs.append(f"sentence {g['id']}: the fix must be exactly one sentence of "
                            f"{R.SEGMENT_BALANCE_WORDS[0]}-{R.SEGMENT_BALANCE_WORDS[1]} words (segment balance; "
                            f"{len(fix.split())} now): {fix!r}")
            elif norm(fix) == norm(g["text"]):
                errs.append(f"sentence {g['id']}: the fix repeats the sentence unchanged (whitespace aside)")
            elif SPOKEN_DIGITS.search(fix):
                errs.append(f"sentence {g['id']}: the fix has a number in digits; write spoken numbers as words: {fix!r}")
            elif UM.search(fix):
                errs.append(f"sentence {g['id']}: the fix says 'um', which the narration voice can't perform: {fix!r}")
        else:
            errs.append(f"sentence {g['id']}: verdict must be 'ok' or 'flag'")
    ok = {k: v.get("ok") for k, v in checks.items() if isinstance(v, dict)}
    if ok.get("numbers_spoken_as_words") is True and t["hints"]["sentences_with_digits"]:
        errs.append(f"numbers_spoken_as_words is true but sentences {t['hints']['sentences_with_digits']} contain digits")
    if ok.get("fillers_performable") is True and t["hints"]["sentences_with_um"]:
        errs.append(f"fillers_performable is true but sentences {t['hints']['sentences_with_um']} say 'um'")
    failed = [k for k, v in ok.items() if v is False]
    flagged = [g for g in sents if g.get("verdict") == "flag"]
    if failed and not flagged:
        errs.append(f"failed checks ({', '.join(failed)}) need at least one flagged sentence whose fix resolves them; "
                    f"the writer applies only sentence fixes")
    if (failed or flagged) and not str(c.get("worstIssue") or "").strip():
        errs.append("worstIssue is empty: one sentence naming the most serious problem")
    if errs:
        raise Fail("verdict: checklist rejected:\n  " + "\n  ".join(errs))


def score(c: dict) -> tuple[int, bool, list[str]]:
    ded = [(VOICE_DEDUCT[k], k, "") for k, v in c["checks"].items() if not v["ok"]]
    ded += [(FLAGGED_SENTENCE, "flagged_sentence", g["id"]) for g in c["sentences"] if g["verdict"] == "flag"]
    total = max(0, 100 - sum(p for p, _, _ in ded))
    return total, not ded, [f"-{p} {k} {x}".strip() for p, k, x in ded]


def cmd_verdict(a) -> int:
    run = Path(a.run).resolve()
    R = rs()
    c = read_json(Path(a.file))
    script = read_json(reviewed_path(run))
    parts = fingerprint_parts(script)
    fp = fingerprint(parts)
    with R.locked(vdir(run)):
        doc = load(run)
        if status_of(doc, fp) in STATUSES:
            raise Fail(f"verdict: the voice review is already '{doc['status']}' for this exact text")
        k = (doc["round"] if doc else 0) + 1
        tp = rdir(run, k) / "checklist.json"
        if not tp.is_file():
            raise Fail(f"verdict: no {tp.as_posix()}; run `voice.py review-prep --run {run.as_posix()} --agent NAME`")
        t = read_json(tp)
        if t["fingerprint"] != fp:
            raise Fail("verdict: the reviewed sentences, voice config, source or profile version changed after "
                       "review-prep; run review-prep again and review the new text")
        validate_filled(c, t, writers(doc, script, t["written_by"]))
        total, passed, deductions = score(c)
        streak = (doc["failed_streak"] if continues_streak(doc, parts) else 0) + (0 if passed else 1)
        status = "passed" if passed else "flagged" if streak >= MAX_ROUNDS else "failed"
        new = {"status": status, "round": k, "fingerprint": fp, "fingerprint_parts": parts, "source": t["source"],
               "profile": t["profile"], "profile_version": t["profile_version"], "reviewed_by": t["reviewed_by"],
               "written_by": writer(script), "score": total, "deductions": deductions,
               "failed_checks": [{"check": key, "evidence": v["evidence"].strip()} for key, v in c["checks"].items()
                                 if not v["ok"]],
               "flags": [{"id": g["id"], "text": g["text"], "issue": g["issue"].strip(), "fix": g["fix"].strip()}
                         for g in c["sentences"] if g["verdict"] == "flag"],
               "worstIssue": str(c.get("worstIssue") or "").strip(), "failed_streak": 0 if passed else streak,
               "max_rounds": MAX_ROUNDS, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        new["history"] = (doc["history"] if doc else []) + [
            {key: new[key] for key in ("round", "status", "score", "fingerprint", "reviewed_by", "written_by")}]
        (rdir(run, k) / "verdict.json").write_text(json.dumps({**new, "checklist": c}, indent=2, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        save(run, new)
    log(run, "verdict", round=k, status=status, score=total, deductions=deductions, reviewer=new["reviewed_by"],
        written_by=new["written_by"], failed_streak=new["failed_streak"])
    print("VOICE_JSON " + json.dumps({key: new[key] for key in
                                      ("status", "round", "fingerprint", "profile_version", "reviewed_by",
                                       "written_by", "score", "failed_streak", "deductions", "failed_checks", "flags")},
                                     ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("--run", required=True)
    sub.add_parser("validate-narration").add_argument("--run", required=True)
    p = sub.add_parser("review-prep")
    p.add_argument("--run", required=True)
    p.add_argument("--agent", required=True, help="name of the review agent (must not have written the script)")
    v = sub.add_parser("verdict")
    v.add_argument("--run", required=True)
    v.add_argument("--file", required=True, help="the reviewer's filled-in checklist")
    a = ap.parse_args(argv)
    fn = {"status": cmd_status, "review-prep": cmd_review_prep, "verdict": cmd_verdict,
          "validate-narration": cmd_validate_narration}
    try:
        return fn[a.cmd](a)
    except (Fail, rs().Fail) as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
