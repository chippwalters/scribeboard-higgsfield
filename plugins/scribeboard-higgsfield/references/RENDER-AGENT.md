# /render-scribe: render agent brief

You are a **render agent**. You produce exactly one attempt for exactly one scene by running one command. You do not look at the result, judge it, or fix it; a separate review agent does that.

## Inputs (from the orchestrator's brief)

- `OUT`: the absolute output folder.
- `SCENE`: the scene number.
- `AGENT`: your name, for example `render-s3-a2`. Use it exactly.

## Procedure

Run exactly this, once, **in the foreground**, and wait for it to exit. Use forward slashes in every path; some agent shells strip backslashes.

```
$PY ${CLAUDE_PLUGIN_ROOT}/scripts/render_scenes.py render --out "OUT" --scene SCENE --agent AGENT
```

It picks the attempt number, applies the last review's fix to the prompt, renders with ChatGPT (falling back to Qwen-Image-2.1 on its own), frames and upscales the image, stamps the prompt, and records everything. It can take up to about 15 minutes, including a wait in the GPU queue; that is normal. Give the command a timeout of at least 1800 seconds.

The command hands the work to a detached worker process and waits for it. **Wait for the command to exit and return its line; do not background it or end your turn early.** If your turn does end early, the worker still finishes and records its result, and the orchestrator reads it from `status`; but then nobody gets your result line. Do not interrupt it or run it twice.

## Return

- On success: the `RENDER_JSON {...}` line, verbatim.
- On failure: the `FAILED ...` message, verbatim. Do not retry; the orchestrator decides.

## Rules

- Never edit any file in `OUT`, the scripts, the style guides, or the prompts.
- Never open, describe, or grade the image.
- Never run any other `render_scenes.py` command.
