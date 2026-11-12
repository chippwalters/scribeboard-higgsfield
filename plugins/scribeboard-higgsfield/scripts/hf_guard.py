r"""hf_guard.py - the plugin's PreToolUse / PostToolUse hook around every Higgsfield generate call.

  hf_guard.py pre     (stdin: the hook's JSON)  before the call
  hf_guard.py post    (stdin: the hook's JSON)  after the call

Before (PreToolUse):
  - a quote call (get_cost true) always passes: it is free.
  - a paid call passes only when every item's exact params match a request a scribe script printed and booked
    (hf.bind), still reserved and quoted; each is then marked `submitting` (hf.submitting re-checks the budget with the
    exact quote). Anything else is denied with the reason: a call the scripts did not book, a request already
    submitted, or one not yet quoted. When no scribe run is under way (no registered run's ledger changed within
    hf.ACTIVE_WINDOW_S) an unbooked call passes untouched, so the plugin never blocks other Higgsfield work.
After (PostToolUse):
  - a quote call: the price goes on the booked line (hf.quote).
  - a paid call: each job id goes on its line (hf.ledger_record, submitted, with the exact quote), and the agent is
    told to still run the script's own `submitted` command (it updates the job's own state).

The hook never stops a call because of its own failure (a crash prints to stderr and passes), except that a paid
call whose request IS booked is never let through unmarked.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GEN = re.compile(r"^mcp__.+__generate_(image|audio)(_batch)?$", re.I)


def out(obj: dict) -> None:
    print(json.dumps(obj))


def deny(reason: str) -> None:
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                "permissionDecisionReason": "scribe-higgsfield credit guard: " + reason}})


def context(event: str, text: str) -> None:
    out({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def items(tool_input: dict, batch: bool) -> list[tuple[int, dict]]:
    if batch:
        return [(int(r.get("index", i)), r.get("params") or {}) for i, r in enumerate(tool_input.get("requests") or [])]
    params = tool_input.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    return [(0, params)]


def walk(obj):
    """Every dict inside a tool response (MCP results arrive as text blocks holding JSON)."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def pre(ev: dict) -> None:
    import hf
    name, ti = ev.get("tool_name", ""), ev.get("tool_input") or {}
    m = GEN.match(name)
    if not m:
        return
    batch = bool(m.group(2))
    its = items(ti, batch)
    if not batch and its and its[0][1].get("get_cost"):
        return   # a quote is free
    found = []
    for idx, params in its:
        hit = hf.find_request(hf.request_sha(params))
        if hit is None:
            if hf.run_active():
                deny(f"item {idx} matches no request a scribe script booked. Run the prep command and send its "
                     f"REQUEST_JSON / PLAN_JSON arguments unchanged (never edit the prompt or params).")
                return
            continue   # no scribe run under way: not this plugin's call
        run, line = hit
        if line["state"] != "reserved":
            deny(f"{line['ref']} is already {line['state']}"
                 + (f" (job {line['job_id']})" if line.get("job_id") else "")
                 + "; never submit it again. Follow the script's status (record or reconcile).")
            return
        if not line.get("quote_exact"):
            deny(f"{line['ref']} has no exact quote yet: call the same tool with the same params plus "
                 f"\"get_cost\": true first" + (" (one generate_audio quote per batch item)" if batch else "") + ".")
            return
        found.append((run, line["ref"]))
    marked = []
    try:
        for run, ref in found:
            hf.submitting(run, ref)
            marked.append((run, ref))
    except hf.HFError as e:
        for run, ref in marked:
            hf.reopen(run, ref)
        deny(str(e))


def post(ev: dict) -> None:
    import hf
    name, ti = ev.get("tool_name", ""), ev.get("tool_input") or {}
    m = GEN.match(name)
    if not m:
        return
    batch = bool(m.group(2))
    its = items(ti, batch)
    resp = list(walk(ev.get("tool_response", ev.get("tool_result"))))
    notes = []
    if not batch and its and its[0][1].get("get_cost"):
        cost = next((d["cost"] for d in resp if isinstance(d.get("cost"), dict) and "credits" in d["cost"]), None)
        hit = hf.find_request(hf.request_sha(its[0][1]))
        if cost is not None and hit is not None and hit[1]["state"] == "reserved":
            hf.quote(hit[0], hit[1]["ref"], cost["credits"])
            notes.append(f"quote {cost['credits']} recorded for {hit[1]['ref']}")
    else:
        jobs: dict[int, str] = {}
        if batch:
            for d in resp:
                for j in d.get("jobs") or []:
                    if isinstance(j, dict) and j.get("job_id") and "index" in j:
                        jobs[int(j["index"])] = j["job_id"]
        else:
            for d in resp:
                res = d.get("results")
                if isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("id"):
                    jobs[0] = res[0]["id"]
                    break
        for idx, params in its:
            hit = hf.find_request(hf.request_sha(params))
            if hit is None:
                continue
            run, line = hit
            job = jobs.get(idx)
            if job:
                hf.ledger_record(run, line["kind"], line["ref"], job, None, "submitted")
                notes.append(f"{line['ref']} -> job {job} (quoted {line.get('quoted')})")
            elif line["state"] == "submitting":
                notes.append(f"{line['ref']}: no job id in the response; reconcile it (show_generations), never "
                             f"resubmit")
        if notes:
            notes.append("the ledger has these jobs; still run each script's `submitted --job ID --quoted N` now "
                         "(it updates the job's own state)")
    if notes:
        context("PostToolUse", "scribe-higgsfield credit guard: " + "; ".join(notes))


def log(mode: str, ev: dict) -> None:
    """One line per call in the app folder's guard_log.jsonl (tool, keys, a short response), for checking what
    Claude Code hands the hook."""
    try:
        import time
        import hf
        p = hf.registry_path().parent / "guard_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "tool": ev.get("tool_name"),
               "keys": sorted(ev), "input": json.dumps(ev.get("tool_input"))[:600],
               "response": json.dumps(ev.get("tool_response"))[:1500] if mode == "post" else None}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - logging never stops the guard
        pass


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
        if GEN.match(ev.get("tool_name", "")):
            log(mode, ev)
        {"pre": pre, "post": post}[mode](ev)
    except Exception as e:  # noqa: BLE001 - a hook crash must be visible but never wedge the session
        print(f"hf_guard {mode}: {type(e).__name__}: {e}", file=sys.stderr)
        if mode == "pre":
            # fail closed only for a call this plugin booked: if its request is known, refuse rather than let it
            # out unmarked
            try:
                import hf
                ti = (json.loads(raw) or {}).get("tool_input") or {}
                p = ti.get("params") or {}
                if not p.get("get_cost") and hf.find_request(hf.request_sha(p)):
                    deny(f"the guard failed ({type(e).__name__}: {e}); fix it before submitting")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
