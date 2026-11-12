r"""hf.py - the Higgsfield handoff shared by render_scenes.py, title.py, characters.py and narrate.py.

Python cannot call MCP tools; only the agent running the skill can. So every paid job is a three-step handoff:
  1. prep      the script reserves the job and prints the exact tool arguments (REQUEST_JSON): what to send, nothing else
  2. submit    the agent calls the tool once with those arguments plus "get_cost": true (the quote), then once without it
               (the job), and reports the job id and the quote back to the script (`... submitted --job ID --quoted N`).
               A submission whose outcome is unclear (timeout, error after sending) is reported with `--unknown`:
               never resubmitted until the user has checked the account
  3. ingest    after `jobs_wait` shows the job finished, the agent hands the result URL to the script (`... ingest
               --url URL`), which downloads, checks and files it; a failed job (refunded by Higgsfield) is reported with
               `--failed REASON`

Library only:
  on(kind)                 images.engine / narration.engine is 'higgsfield'
  image_request(prompt, run, refs)       generate_image arguments: the image model chosen on the run's setup page
                           (image-models/higgsfield.json params)
  audio_request(text, run) generate_audio arguments: the narrator voice chosen on the run's setup page
  ledger_check(run, kind, refs)  refuse without a complete setup (runsetup.py), and when the run's spent + reserved
                           credits + these jobs' estimate would pass the run's budget (the setup page's cap); otherwise
                           reserve the jobs' estimate in the same locked step
  ledger_record(run, kind, ref, job_id, quoted, state)   one line per submitted job in RUN/credits.json (replaces its
                           reservation; without a quote it keeps the reserved estimate)
  ledger_release(run, ref) drop the reservation of a prepared job that will never be submitted (`hf.py release`)
  ledger_lock(run)         the cross-process lock every ledger write runs under
  download(url, dest)      curl (the upload/CDN hosts refuse Python's urllib)

Every paid prep command (render_scenes.py render-prep, title.py prep, characters.py prep, narrate.py tts-plan) calls
ledger_check before it prints a request, so no paid job can be prepared for a run whose setup page is unanswered.

Write-ahead (no job is ever paid twice by a resumed agent): every printed request is bound to its reserved ledger line
by a hash of its exact params (bind). Before the paid call the line takes the exact quote (`hf.py quote`, from the
tool's get_cost answer) and is marked `submitting` (`hf.py submitting`), which re-checks the budget with that quote.
A `submitting` line with no job recorded is never "submit" again: status says reconcile (find the job in the account,
record it with `submitted --job`, or `--failed "not created"`). The plugin's hooks (hooks/hf_guard.py) do quote,
submitting and the job record automatically around every Higgsfield generate call and deny a call that matches no
booked request; without hooks (Codex, omp) the agent runs the two commands itself.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path

import oslib
import runsetup          # scripts/runsetup.py: RUN/setup.json (voice, budget), required with Higgsfield
import settings as cfg


class HFError(Exception):
    pass


def on(kind: str) -> bool:
    """kind 'images' or 'narration': is that engine Higgsfield?"""
    return cfg.get(f"{kind}.engine") == "higgsfield"


def image_model(run: Path) -> dict:
    """The picture model chosen on the run's setup page (runsetup.image_model): {"id", "label", "params", "credits"}."""
    try:
        return runsetup.image_model(run)
    except runsetup.SetupError as e:
        raise HFError(f"no picture model before the setup page is answered: {e}") from e


def image_request(prompt: str, run: Path, refs: list[dict] | None = None, aspect: str = "16:9",
                  model_id: str | None = None) -> dict:
    """The generate_image arguments for one picture, drawn with the run's image model (its catalog params, e.g. model,
    resolution, quality). refs = [{"value": job_id or media_id, "role": "image_references"}]. model_id: another
    catalog model, only for a character sheet the user asked to have drawn with it (characters.py prep --model)."""
    if model_id is None:
        m = image_model(run)
    else:
        try:
            cat = runsetup.image_models()["models"]
        except runsetup.SetupError as e:
            raise HFError(str(e)) from e
        if model_id not in cat:
            raise HFError(f"image model {model_id!r} is not one of {', '.join(cat)}")
        m = cat[model_id]
    params = {**m["params"], "prompt": prompt, "aspect_ratio": aspect, "count": 1, "use_unlim": False}
    if refs:
        params["medias"] = refs
    return {"tool": "generate_image", "arguments": {"params": params}}


def _voice(run: Path) -> dict:
    try:
        return runsetup.voice(run)
    except runsetup.SetupError as e:
        raise HFError(str(e)) from e


def audio_request(text: str, run: Path) -> dict:
    """The generate_audio arguments for one take, in the narrator voice chosen on the run's setup page."""
    v = _voice(run)
    params = {"model": v["model"], "prompt": text, "voice_type": v["voice_type"], "voice_id": v["voice_id"],
              "format": "wav", "sample_rate": 48000, "count": 1, "use_unlim": False}
    return {"tool": "generate_audio", "arguments": {"params": params}}


def voice_key(run: Path) -> str:
    """Who speaks a Higgsfield take (narrate.py voice tracking): a take in another voice counts as changed."""
    v = _voice(run)
    return f"higgsfield:{v['model']}:{v['voice_id']}"


def ledger_path(run: Path) -> Path:
    return run / "credits.json"


def ledger(run: Path) -> dict:
    """The job list. Never checks the setup: recording a job that was already submitted (paid) must always work."""
    p = ledger_path(run)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"version": 1, "cap": None, "jobs": []}


def paid_work_exists(run: Path) -> bool:
    """The run already holds work that was paid for (takes, pictures, sheets): its ledger can never be empty."""
    return any((run / p).is_file() for p in ("narration/state.json", "scenes/state.json", "characters/sheets.json",
                                              "title/title.json")) and any(
        (run / d).is_dir() and any((run / d).iterdir()) for d in ("narration/_hf", "scenes/attempts", "characters",
                                                                  "title/attempts"))


# ------------------------------------------------------------------ requests bound to ledger lines (write-ahead)

STALE_SUBMIT_S = 15 * 60   # a `submitting` line this old with no job recorded blocks new preps until reconciled


def params_of(arguments: dict) -> dict:
    return arguments.get("params", arguments) if isinstance(arguments, dict) else {}


def request_sha(params: dict) -> str:
    """The hash that binds a printed request to its ledger line: the exact params, without get_cost."""
    p = {k: v for k, v in (params or {}).items() if k != "get_cost"}
    return hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def registry_path() -> Path:
    """The runs with open requests, so a hook (which knows only the tool call) can find a request's ledger.
    SCRIBE_HF_REGISTRY names another file (the tests use a temporary one)."""
    if os.environ.get("SCRIBE_HF_REGISTRY"):
        return Path(os.environ["SCRIBE_HF_REGISTRY"])
    return oslib.data_dir("scribe-higgsfield") / "active_runs.json"


def register_run(run: Path) -> None:
    reg = registry_path()
    with file_lock(reg.with_suffix(".lock"), "the active-runs list"):
        runs = json.loads(reg.read_text(encoding="utf-8")) if reg.is_file() else []
        runs = [str(run)] + [r for r in runs if r != str(run)]
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps(runs[:50], indent=1), encoding="utf-8")


def active_runs() -> list[Path]:
    reg = registry_path()
    try:
        return [Path(r) for r in json.loads(reg.read_text(encoding="utf-8"))] if reg.is_file() else []
    except (OSError, ValueError):
        return []


def _newest(doc: dict, ref: str) -> dict | None:
    return next((j for j in reversed(doc["jobs"]) if j["ref"] == ref and j["state"] not in ("released",)), None)


def bind(run: Path, ref: str, req: dict) -> str:
    """Bind the request just printed for `ref` to its reserved ledger line (request_sha, tool, model)."""
    params = params_of(req.get("arguments", {}))
    sha = request_sha(params)
    with ledger_lock(run):
        doc = ledger(run)
        line = next((j for j in reversed(doc["jobs"]) if j["ref"] == ref and j["state"] == "reserved"), None)
        if line is None:
            raise HFError(f"bind: {ref} has no reserved line in {ledger_path(run)}")
        line.update(request_sha=sha, tool=req.get("tool"), model=params.get("model"))
        _save(run, doc)
    register_run(run)
    return sha


def find_request(sha: str) -> tuple[Path, dict] | None:
    """The newest ledger line (in any registered run) bound to this request hash, with its run."""
    for run in active_runs():
        if not ledger_path(run).is_file():
            continue
        line = next((j for j in reversed(ledger(run)["jobs"]) if j.get("request_sha") == sha), None)
        if line is not None:
            return run, line
    return None


def quote(run: Path, ref: str, credits: float) -> dict:
    """Record the exact price of `ref` (the tool's get_cost answer) on its reserved line: from now on it counts that
    price, not the estimate."""
    try:
        c = float(credits)
    except (TypeError, ValueError):
        raise HFError(f"quote: {credits!r} is not a number")
    if not math.isfinite(c) or c < 0:
        raise HFError(f"quote: {credits!r} is not a price")
    with ledger_lock(run):
        doc = ledger(run)
        line = _newest(doc, ref)
        if line is None or line["state"] != "reserved":
            raise HFError(f"quote: {ref} is {line['state'] if line else 'not booked'}; only a reserved request takes "
                          f"a quote")
        line.update(quoted=round(c, 4), quote_exact=True, quoted_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _save(run, doc)
        return line


def _cap(run: Path, doc: dict):
    return run_cap(run) if runsetup.required() else (doc.get("cap") if doc.get("cap") is not None
                                                     else cfg.get("higgsfield.max_credits"))


def submitting(run: Path, ref: str) -> dict:
    """Write-ahead, right before the paid call: the line must be reserved and quoted, the run's spend plus this exact
    price must fit its budget; the line becomes `submitting` (counted, never planned or submitted again)."""
    with ledger_lock(run):
        doc = ledger(run)
        line = _newest(doc, ref)
        if line is None:
            raise HFError(f"submitting: {ref} is not booked in {ledger_path(run)}: run its prep command first")
        if line["state"] != "reserved":
            raise HFError(f"submitting: {ref} is already {line['state']}"
                          + (f" (job {line['job_id']})" if line.get("job_id") else "")
                          + "; it is never submitted again (a `submitting` one is reconciled: find its job in the "
                            "account and record it, or `--failed \"not created\"`)")
        if not line.get("quote_exact"):
            raise HFError(f"submitting: {ref} has no exact quote: call the tool with get_cost first and record it "
                          f"(`hf.py quote`; the plugin's hook does it)")
        cap = _cap(run, doc)
        used = spent(doc, [ref])
        if cap is not None and used + line["quoted"] > cap + 1e-9:
            raise HFError(f"credits: {ref} is quoted at {line['quoted']}, and the run has used or reserved {used} of "
                          f"its {cap}-credit budget. STOP and ask the user whether to raise the budget")
        line.update(state="submitting", submitting_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _save(run, doc)
        return line


def reopen(run: Path, ref: str) -> None:
    """Undo `submitting` for a call that was then refused before it went out (one item of a batch failed its
    check): the line is reserved again, quote kept."""
    with ledger_lock(run):
        doc = ledger(run)
        line = _newest(doc, ref)
        if line is not None and line["state"] == "submitting" and not line.get("job_id"):
            line["state"] = "reserved"
            line.pop("submitting_at", None)
            _save(run, doc)


ACTIVE_WINDOW_S = 3 * 3600   # the guard refuses unbooked generate calls while a run was touched this recently


def run_active() -> bool:
    """A registered run's ledger changed within ACTIVE_WINDOW_S: a scribe run is under way on this machine."""
    now_s = time.time()
    for run in active_runs():
        p = ledger_path(run)
        try:
            if p.is_file() and now_s - p.stat().st_mtime < ACTIVE_WINDOW_S:
                return True
        except OSError:
            continue
    return False


def pending_note(run: Path, ref: str) -> str | None:
    """For a job its script still sees as requested: what the ledger knows that the script does not. None when the
    request is still only reserved (submit it)."""
    try:
        line = _newest(ledger(run), ref)
    except (OSError, ValueError):
        return None
    if line is None:
        return None
    if line["state"] == "submitting":
        return (f"reconcile {ref}: a submit started at {line.get('submitting_at')} with no job recorded; find it "
                f"(mcp__higgsfield__show_generations / transactions), then record it with its `submitted --job ID`, "
                f"or close it with `--failed \"not created\"`. Never submit it again")
    if line["state"] in ("submitted", "unknown") and line.get("job_id"):
        return (f"record {ref}: the ledger already holds job {line['job_id']} (quoted {line.get('quoted')}); run its "
                f"`submitted --job {line['job_id']} --quoted {line.get('quoted')}`. Never submit it again")
    return None


def run_cap(run: Path) -> float | None:
    """The run's credit budget: the setup page's cap (setup required), else settings higgsfield.max_credits."""
    if runsetup.required():
        try:
            return runsetup.cap(run)
        except runsetup.SetupError as e:
            raise HFError(str(e)) from e
    return cfg.get("higgsfield.max_credits")


_held: dict[str, int] = {}


def _os_lock(fh, on: bool) -> None:
    """An operating-system lock on the open lock file: the OS drops it when the process ends, so a crashed command
    never leaves a stale lock behind (no takeover race)."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(lock: Path, what: str = "the run's bookkeeping"):
    """One holder at a time across processes, re-entrant within a process. The lock file stays on disk; the lock is
    the OS lock on it."""
    key = str(lock)
    if _held.get(key):
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    deadline = time.time() + 120
    while True:
        try:
            _os_lock(fh, True)
            break
        except OSError:
            if time.time() > deadline:
                fh.close()
                raise HFError(f"{what}: {lock} has been locked by another command for over two minutes; wait for it "
                              f"to finish")
            time.sleep(0.1)
    _held[key] = 1
    try:
        yield
    finally:
        _held[key] = 0
        try:
            _os_lock(fh, False)
        finally:
            fh.close()


def ledger_lock(run: Path):
    """The lock every read-check-write of RUN/credits.json runs under, so two prep or submitted commands at the same
    moment can never both pass the budget or drop a line."""
    return file_lock(run / "credits.lock", "credits")


def _save(run: Path, doc: dict) -> None:
    p = ledger_path(run)
    tmp = p.with_suffix(".json.tmp")
    if runsetup.required():
        doc = {**doc, "cap": None, "_cap": "the run's budget lives in setup.json (budget.cap)"}
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    for i in range(50):   # Windows: a reader holding credits.json open makes the replace fail briefly
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if i == 49:
                raise
            time.sleep(0.1)


COUNTED = ("submitted", "unknown", "reserved", "submitting")


def spent(doc: dict, skip_refs: list[str] | None = None) -> float:
    """Credits of every job that may be charged: submitted, unknown, or reserved by a prep and not yet submitted
    (failed jobs are refunded; released reservations were never submitted). skip_refs: reservations about to be made
    again (a prep re-run for the same job) are not counted twice."""
    skip = set(skip_refs or [])
    return round(sum(j["quoted"] or 0 for j in doc["jobs"] if j["state"] in COUNTED
                     and not (j["state"] == "reserved" and j["ref"] in skip)), 2)


def estimate(doc: dict, kind: str, run: Path | None = None, model: str | None = None) -> float:
    """The next job's price: the run's latest real quote for that kind and model (a submitted job priced with
    get_cost; never a reservation, a release, a refund or an estimate standing in for a quote), else (a picture, with
    the run's setup) the model's catalog price, else the settings estimate."""
    if kind == "image" and model is None and run is not None and runsetup.required():
        try:
            model = image_model(run)["params"]["model"]
        except (HFError, KeyError):
            model = None
    quotes = [j["quoted"] for j in doc["jobs"] if j["kind"] == kind and j.get("quoted") and j["state"] == "submitted"
              and not j.get("quote_estimated") and (model is None or j.get("model") in (None, model))]
    if quotes:
        return quotes[-1]
    if kind == "image" and run is not None and runsetup.required():
        cat = runsetup.image_models()["models"]
        hit = next((m for m in cat.values() if m["params"].get("model") == model), None)
        return (hit or image_model(run))["credits"]
    return cfg.get("higgsfield.audio_credits" if kind == "audio" else "higgsfield.image_credits") or 0.0


def ledger_check(run: Path, kind: str, refs: list[str], model: str | None = None) -> dict:
    """Refuse without a complete setup, and when the jobs `refs` (one per job, the refs their `submitted` records
    will use) would take the run past its budget; otherwise RESERVE their estimated price at once, in the same locked
    step, so jobs prepared side by side (or by two commands at the same moment) can never pass the budget together.
    A reservation is replaced by the job's submitted / unknown / failed record, or dropped with `hf.py release`.
    Returns the ledger summary."""
    if not refs:
        raise HFError("ledger_check: no job refs given")
    try:   # narration takes need the start page (script, voice, budget); pictures need the setup page too, except
        #    the character sheets (runsetup.require_pictures)
        runsetup.require_start(run) if kind == "audio" else runsetup.require_pictures(run, refs)
    except runsetup.SetupError as e:
        raise HFError(f"no paid {kind} job before {'the start page' if kind == 'audio' else 'the setup page'} is "
                      f"answered: {e}") from e
    with ledger_lock(run):
        if not ledger_path(run).is_file() and paid_work_exists(run):
            raise HFError(f"credits: {ledger_path(run)} is missing but the run holds paid work; restore it (or "
                          f"rebuild it from the account's transactions) before anything new is prepared")
        doc = ledger(run)
        cap = _cap(run, doc)
        each = estimate(doc, kind, run, model)
        est = each * len(refs)
        used = spent(doc, refs)
        unknown = [j for j in doc["jobs"] if j["state"] == "unknown"]
        if unknown:
            raise HFError(f"credits: {len(unknown)} submission(s) with an unknown outcome "
                          f"({', '.join(j['ref'] for j in unknown)}); check the Higgsfield account (transactions / "
                          f"show_generations), then record each one with `... submitted --job ID` or `--failed`. "
                          f"Nothing new is prepared until then")
        now_s = time.time()
        stale = [j for j in doc["jobs"] if j["state"] == "submitting" and not j.get("job_id") and now_s - time.mktime(
            time.strptime(j.get("submitting_at") or j["at"], "%Y-%m-%d %H:%M:%S")) > STALE_SUBMIT_S]
        if stale:
            raise HFError(f"credits: {len(stale)} submit(s) started over {STALE_SUBMIT_S // 60} minutes ago with no "
                          f"job recorded ({', '.join(j['ref'] for j in stale)}); reconcile each (find its job in the "
                          f"account, record `submitted --job ID`, or `--failed \"not created\"`). Nothing new is "
                          f"prepared until then")
        if cap is not None and used + est > cap:
            raise HFError(f"credits: this run has used or reserved {used} of its {cap}-credit budget (RUN/credits.json); "
                          f"the next {len(refs)} {kind} job(s) are estimated at {round(est, 2)}. STOP and ask the user "
                          f"whether to raise the budget (`hf.py cap --run RUN --set N`, only with their answer) or stop")
        doc["jobs"] = [j for j in doc["jobs"] if not (j["ref"] in refs and j["state"] == "reserved")]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        doc["jobs"] += [{"kind": kind, "ref": r, "job_id": None, "quoted": round(each, 2), "state": "reserved",
                         "at": now, **({"model": model} if model else {})} for r in refs]
        _save(run, doc)
    return {"cap": cap, "spent": used, "next_estimate": round(est, 2), "reserved": refs}


def ledger_record(run: Path, kind: str, ref: str, job_id: str | None, quoted: float | None, state: str) -> None:
    """state: submitted | unknown | failed. A later record for the same ref replaces its `reserved` or `unknown` one.
    A record without a quote keeps the reservation's estimate (else the run's estimate), marked quote_estimated, so a
    paid job never counts as 0 against the budget."""
    with ledger_lock(run):
        doc = ledger(run)
        if job_id and any(j.get("job_id") == job_id for j in doc["jobs"]):
            return   # a job id is recorded once, under the first ref it came with: retries never count it again
        prior = [j for j in doc["jobs"] if j["ref"] == ref and j["state"] in ("unknown", "reserved", "submitting")]
        rec = {"kind": kind, "ref": ref, "job_id": job_id, "quoted": quoted, "state": state,
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        for j in reversed(prior):   # what the reservation knew: the request it printed, its model, its exact quote
            for k in ("request_sha", "tool", "model"):
                if j.get(k) and k not in rec:
                    rec[k] = j[k]
        exact = next((j for j in reversed(prior) if j.get("quote_exact")), None)
        if quoted is None and exact is not None:
            rec["quoted"], rec["quote_exact"] = exact["quoted"], True
        elif quoted is None and state != "failed":
            rec["quoted"] = next((j["quoted"] for j in reversed(prior) if j.get("quoted")), None) or \
                estimate(doc, kind, run)
            rec["quote_estimated"] = True
        elif quoted is not None and exact is not None and abs(float(quoted) - exact["quoted"]) > 1e-6:
            rec["quote_note"] = f"reported {quoted}, get_cost said {exact['quoted']}"
            rec["quoted"] = max(float(quoted), exact["quoted"])
        doc["jobs"] = [j for j in doc["jobs"] if j not in prior]
        doc["jobs"].append(rec)
        _save(run, doc)


def ledger_fail(run: Path, ref: str) -> None:
    """A job that failed on Higgsfield's side (refunded), or a submission that turned out never to have been created."""
    with ledger_lock(run):
        doc = ledger(run)
        for j in reversed(doc["jobs"]):
            if j["ref"] == ref and j["state"] in COUNTED:
                j["state"] = "failed"
                break
        _save(run, doc)


def ledger_job(run: Path, job_id: str) -> dict | None:
    """The ledger line holding this job id (each job id is recorded once), else None."""
    return next((j for j in ledger(run)["jobs"] if j.get("job_id") == job_id), None)


def ledger_fail_job(run: Path, job_id: str) -> bool:
    """A refund reported by job id (for a job recorded on its own `extra-` ref): that line counts 0 from now on."""
    with ledger_lock(run):
        doc = ledger(run)
        hit = False
        for j in doc["jobs"]:
            if j.get("job_id") == job_id and j["state"] in ("submitted", "unknown"):
                j["state"], hit = "failed", True
        _save(run, doc)
    return hit


def max_take(run: Path, sentence: str) -> int:
    """The highest take number the ledger has ever used for a sentence (refs narration/<id>/t<n>), 0 for none. Take
    numbers never restart, so a new take's ref can never collide with an old take's job."""
    n = 0
    for j in ledger(run)["jobs"]:
        parts = j["ref"].split("/")
        if len(parts) >= 3 and parts[0] == "narration" and parts[1] == sentence and parts[2][1:].isdigit():
            n = max(n, int(parts[2][1:]))
    return n


def ledger_close(run: Path, ref: str, attempt_state: str) -> None:
    """`--failed` on a picture attempt. An attempt that was submitted (or unknown) is refunded (`ledger_fail`). An
    attempt still `requested` was never submitted, so only its reservation is released, and only when the ledger
    agrees: when the ledger records a submitted / unknown job for the ref (the state file missed it), refuse rather
    than zero a paid job."""
    with ledger_lock(run):
        if attempt_state != "requested":
            ledger_fail(run, ref)
            return
        doc = ledger(run)
        newest = _newest(doc, ref)
        if newest is not None and newest["state"] == "submitting" and not newest.get("job_id"):
            # the reconcile of a submit that started: the account shows no such job, so it was never created
            newest.update(state="failed", note="not created (reconciled)")
            _save(run, doc)
            return
        jobs = [j for j in doc["jobs"] if j["ref"] == ref]
        paid = [j for j in jobs if j["state"] in ("submitted", "unknown")]
        if any(j["state"] == "reserved" for j in jobs):
            paid = []   # the newest prep's reservation is still open, so that prep's job was never submitted (a
            #             submitted record replaces the reservation); an older paid line with the same ref is another job
        if paid:
            raise HFError(f"{ref}: the ledger records job {paid[-1].get('job_id')!r} ({paid[-1]['state']}) for this "
                          f"attempt, so it was submitted: record it with `submitted --job ...` and ingest its result, "
                          f"never `--failed \"not submitted\"`")
        ledger_release(run, ref)


def ledger_release(run: Path, ref: str) -> bool:
    """Drop the reservation of a job that was prepared but will never be submitted (the user declined its price, the
    sentence was dropped). Only a `reserved` line is released; a submitted job is never touched. Returns whether one
    was released."""
    with ledger_lock(run):
        doc = ledger(run)
        hit = False
        for j in doc["jobs"]:
            if j["ref"] == ref and j["state"] == "reserved":
                j["state"], hit = "released", True
        _save(run, doc)
    return hit


def download(url: str, dest: Path, job_id: str | None = None) -> Path:
    """Fetch a job's result. With job_id, the URL must name that job (Higgsfield result URLs end in _<job id>.<ext>),
    so one job's picture can never be filed as another's."""
    if not url.startswith("https://"):
        raise HFError(f"download: not an https URL: {url!r}")
    if job_id and job_id.lower() not in url.lower():
        raise HFError(f"download: the URL does not belong to job {job_id} ({url}); give the URL jobs_wait returned "
                      f"for that job")
    dest.parent.mkdir(parents=True, exist_ok=True)
    curl = oslib.which("curl")
    if curl is None:
        raise HFError("download: curl not found (it ships with Windows 10+, macOS and Linux)")
    p = subprocess.run([str(curl), "-s", "-S", "-L", "--fail", "-o", str(dest), url], capture_output=True, text=True,
                       creationflags=oslib.NO_WINDOW, timeout=600)
    if p.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise HFError(f"download failed ({p.returncode}): {p.stderr.strip()[-300:]}")
    return dest


def ref_lead(style_ref: bool, sheets: list[str]) -> str:
    """The prompt's opening for the attached references, in the order they are attached."""
    if not style_ref and not sheets:
        return ""
    parts, i = [], 1
    if style_ref:
        parts.append(f"Reference image {i} is a finished scene from the same video, a STYLE reference only: match its "
                     f"line weight, lettering, colour use and drawing style, but draw none of its objects, characters' "
                     f"poses or layout.")
        i += 1
    for name in sheets:
        parts.append(f"Reference image {i} is the character sheet of {name}: draw {name} exactly as on the sheet "
                     f"(face, hair, clothing, proportions, and only the accessories the sheet shows), in the pose the "
                     f"scene needs; never copy the sheet's "
                     f"layout or draw several copies.")
        i += 1
    return " ".join(parts) + " "


def emit_request(kind: str, ref: str, req: dict, extra: dict | None = None, run: Path | None = None) -> None:
    """Print the request; with run, bind it to its reserved ledger line first (write-ahead, `bind`)."""
    sha = bind(run, ref, req) if run is not None else None
    print("REQUEST_JSON " + json.dumps({"kind": kind, "ref": ref, **req, **(extra or {}),
                                        **({"request_sha": sha} if sha else {})}))


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="RUN/credits.json: the run's Higgsfield credit ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    c = sub.add_parser("cap")
    c.add_argument("--run", required=True)
    c.add_argument("--set", type=float, required=True)
    r = sub.add_parser("release", help="drop the reservation of a prepared job that will never be submitted")
    r.add_argument("--run", required=True)
    r.add_argument("--ref", required=True, help="the job's ref, as its REQUEST_JSON / PLAN_JSON printed it")
    q = sub.add_parser("quote", help="record a request's exact price (the tool's get_cost answer) before submitting")
    q.add_argument("--run", required=True)
    q.add_argument("--ref", required=True)
    q.add_argument("--credits", type=float, required=True)
    w = sub.add_parser("submitting", help="write-ahead, right before the paid call: re-check the budget with the "
                                          "exact quotes and mark the requests submitting")
    w.add_argument("--run", required=True)
    w.add_argument("--ref", required=True, action="append", help="repeat for a batch")
    a = ap.parse_args(argv)
    run = Path(a.run).resolve()
    if a.cmd == "quote":
        try:
            line = quote(run, a.ref, a.credits)
        except HFError as e:
            print(f"FAILED quote: {e}", file=sys.stderr)
            return 1
        print("QUOTE_JSON " + json.dumps({"ref": a.ref, "quoted": line["quoted"]}))
        return 0
    if a.cmd == "submitting":
        done = []
        try:
            for ref in a.ref:   # each checks the budget with everything marked before it
                done.append(submitting(run, ref)["ref"])
        except HFError as e:
            print(f"FAILED submitting: {e} (marked before the failure: {', '.join(done) or 'none'}; submit only "
                  f"those)", file=sys.stderr)
            return 1
        print("SUBMITTING_JSON " + json.dumps({"refs": done, "next": "call the tool once now, then record each job "
                                                                       "with its submitted command at once"}))
        return 0
    if a.cmd == "release":
        if not a.ref.startswith("narration/"):
            print(f"FAILED release: {a.ref!r} is a picture; a picture job you will not submit is closed by its own "
                  f"script so its attempt does not stay open: `render_scenes.py render-ingest ... --failed \"not "
                  f"submitted\"`, `title.py ingest ... --failed ...` or `characters.py ingest ... --failed ...`",
                  file=sys.stderr)
            return 1
        if not ledger_release(run, a.ref):
            print(f"FAILED release: no reserved job {a.ref!r} in {ledger_path(run)} (a submitted job is never "
                  f"released; record it with `submitted` or `--failed`)", file=sys.stderr)
            return 1
    if a.cmd == "cap":
        if runsetup.required():
            try:
                runsetup.set_cap(run, a.set)
            except runsetup.SetupError as e:
                print(f"FAILED cap: {e}", file=sys.stderr)
                return 1
        else:
            with ledger_lock(run):
                doc = ledger(run)
                doc["cap"] = a.set
                _save(run, doc)
    doc = ledger(run)
    try:
        doc["cap"] = run_cap(run) if runsetup.required() else doc.get("cap") or cfg.get("higgsfield.max_credits")
    except HFError as e:
        doc["cap"] = None
        print(f"[hf] no budget yet: {e}", file=sys.stderr)
    print("CREDITS_JSON " + json.dumps({"cap": doc.get("cap"), "spent_quoted": spent(doc), "jobs": len(doc["jobs"]),
                                        "unknown": [j["ref"] for j in doc["jobs"] if j["state"] == "unknown"],
                                        "reserved": [j["ref"] for j in doc["jobs"] if j["state"] == "reserved"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
