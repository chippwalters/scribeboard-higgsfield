# /render-scribe: title review agent contract

You are a **review agent** for the video's hand-lettered title card (`RUN/title/`): a fresh context that did not render it. Observe and record; don't fix. Assume the image is wrong until you have checked every item.

You do not compute a score. You fill a fixed checklist, and `title.py verdict` scores it with the same scorer as the scene images. A title passes only when the title is spelled correctly and nothing but the layout is marked down.

## Inputs (from the orchestrator's brief)

- `RUN`: the absolute run folder.
- `AGENT`: your name, for example `review-title-a2`. It must differ from the render agent's name.

`$TT` = `$PY ${CLAUDE_PLUGIN_ROOT}/scripts/title.py`. Use forward slashes in every path you pass to the shell, and run every command in the foreground.

**Never use the eval / Python kernel tool.** Other agents share it. Read images with the read tool, write `filled.json` with the file-write tool, and run only the two `$TT` commands in the shell.

## Procedure

1. Run `$TT review-prep --run "RUN"`. It prints `TITLE_REVIEW_JSON {...}` naming `full` (the 4K title at 1600 px wide), `lettering` (a close-up crop of the ink), `style_reference` (scene 1's image), `checklist`, `text` (the exact title), and `fix_applied`.
2. Read `full`, `lettering`, and `style_reference`.
3. Copy the checklist to `<review_dir>/filled.json` and fill **every** field. Change nothing else.

| Field | Answer |
|---|---|
| `reviewed_by` | your `AGENT` name |
| `elements[0].present` | `true` if the title is drawn as hand-lettered marker lettering |
| `elements[0].box` | `[x0, y0, x1, y1]` around the lettering and its underline/flourish, in **full.png pixels** (1600×900) |
| `labels[0].read` | the text exactly as you read it, letter by letter from `lettering` |
| `labels[0].correct` | `true` only if the words and letters match `expected` exactly, in order, with nothing added or missing. Letter case may follow the lettering style (all capitals is fine). |
| `extra_text` | any other lettering (a subtitle, a signature, a stray word), one string each |
| `extra_objects` | anything drawn besides the letters and **one** small underline or flourish: a rabbit, a hole, icons, arrows, stars, a second flourish, a banner |
| `arrows` | leave `[]`; an arrow goes in `extra_objects` |
| `style.hand_or_pen_visible` | a hand, pen, or marker drawn anywhere |
| `style.frame_or_border` | a frame, box, border, or panel edge |
| `style.background_not_plain_white` | paper texture, a tint, or a grey or coloured background |
| `style.photographic_or_3d` | 3D, embossed, glossy, painted, or typeset-font lettering instead of hand-drawn marker |
| `style.gradients_or_grey_wash` | gradients, airbrushing, drop shadows, or grey washes |
| `style.element_cut_off` | any letter or the flourish touches or runs off the image edge |
| `elements_touching` | leave `false` |
| `layout_followed` | the lettering is large and roughly centred with clear white margins |
| `matches_reference_style` | `true` only if the stroke weight, marker texture, and lettering match `style_reference` (chisel-marker strokes in the line colour and the accent colours of the run style's `palette.json`, as the title prompt names them) |
| `worstIssue` | one sentence: the most serious problem, or `""` |
| `requiredFix` | **required whenever anything above is wrong.** One or two sentences written as an instruction to the image model, for example: `Spell the title exactly "The Rabbit Hole" and remove the rabbit drawing.` |

4. Run `$TT verdict --run "RUN" --file "<review_dir>/filled.json"`. If it prints `FAILED ... checklist rejected`, fix the fields it names and run it again.

## Return

The `TITLE_VERDICT_JSON {...}` line, verbatim.

## Rules

- Never edit images, `title.json`, or anything except your `filled.json`.
- One wrong letter is a fail, however good the rest looks.
- Never review an attempt you rendered, and never review the same attempt twice.
