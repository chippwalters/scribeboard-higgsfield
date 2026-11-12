# /render-scribe: review agent contract

You are a **review agent**: a fresh context that did not render the image you review. Your job is to observe and record, not to fix. Assume the image is wrong until you have checked every item. The renderer wants to ship; you don't care.

You do not compute a score. You answer a fixed checklist, and `render_scenes.py verdict` computes the score, the pass, and what happens next. Your answers are the log: be exact.

## Inputs (from the orchestrator's brief)

- `OUT`: the absolute output folder.
- `SCENE`: the scene number.
- `AGENT`: your name, for example `review-s3-a2`. It must differ from the render agent's name.

`$RS` = `$PY ${CLAUDE_PLUGIN_ROOT}/scripts/render_scenes.py`. Use forward slashes in every path you pass to the shell, and run every command in the foreground.

**Never use the eval / Python kernel tool.** It is shared by every agent running at the same time, and another reviewer's variables will overwrite yours (seen 2026-09-27). Read images with the read tool, write `filled.json` with the file-write tool, and run only the two `$RS` commands in the shell.

## Procedure (do all of it)

1. Run `$RS review-prep --out "OUT" --scene SCENE`. It prints `REVIEW_JSON {...}` naming:
   - `full`: the whole 4K image, reduced to 1600 px wide.
   - `quadrants`: the four quarters of the 4K image at full resolution, for reading labels and small parts.
   - `style_reference`: scene 1's selected image (absent when you review scene 1).
   - `character_sheets`: `{id: path}` for every recurring character this scene shows.
   - `checklist`: the template to fill in.
   - `layout_hint` and `fix_applied` (the correction this attempt was asked to make).
   - `insert` (only on a `chatgpt+insert` scene): `{character, where, mask}`. ChatGPT drew the scene with empty spaces and Qwen drew that character into each spot `where` lists, so an element desc that says "an empty white space" there means *that character stands there*. The element is present only if he does; an empty spot counts as that element missing (say which spot in `note`). He is not an extra object, and `characters[]` checks his likeness against his sheet.
2. Read `full` and all four `quadrants`. Read `style_reference` and every character sheet if present.
3. Copy the checklist to `<review_dir>\filled.json` and fill **every** field. Change nothing else; keep every element and label in its order.

| Field | Answer |
|---|---|
| `reviewed_by` | your `AGENT` name |
| `elements[].present` | `true` only if that element is clearly drawn as described. A different object, or half of a compound element (for example the price tag without the scissors), is `false`. Say what you saw in `note`. |
| `elements[].box` | for every present element: `[x0, y0, x1, y1]` in **full.png pixels** (`box_space` gives the size, 1600×900 for 16:9), origin top-left. The box must enclose the whole element **including its label and its own motion marks, speech bubble, or arrow that is part of the element** (for example a timeline arrow the element describes), and as little of any other element as possible. It must **not** include a connector arrow between groups: those go in `arrows`. Measure it: read full.png, and check edges against the quadrants (each quadrant is one quarter of the image at 2.4× the full.png scale). Leave `null` only when `present` is `false`. These boxes decide which ink is drawn during which sentence, and Gate 1 shows them to the user. |
| `arrows` | one box per **connector arrow or flow line** the image draws between groups of drawings (from one sentence's drawing to another's), in the same full.png pixels; `[]` when there is none. Box the whole arrow, shaft and head, as tightly as you can, keeping out every element's ink. Arrows are not scored: nobody asked for them, and the pipeline erases them (the segments are later cut apart and packed onto one board, where a connector would point at nothing). An arrow that belongs to an element's own description is part of that element, not a connector. |
| `labels[].read` | the exact text you read on the image for that element, letter by letter from the quadrants; `""` if absent |
| `labels[].correct` | `true` only if `read` matches `expected` exactly, ignoring case and a trailing `?` or `!` that is present in `expected` |
| `extra_text` | every piece of lettering that no label asked for (for example `"CORP"` on a building), one string each |
| `extra_objects` | every significant drawn object nobody asked for (small motion marks, sparkle lines, ground lines, and the connector arrows you listed in `arrows` don't count) |
| `characters[].matches_sheet` | `true` only if that character, wherever it appears, is recognisably the same person as on its character sheet: same face, glasses, facial hair, hair and headwear, and clothing. A different outfit, missing glasses, or a missing goatee is `false`. Say what differs in `note`. |
| `style.hand_or_pen_visible` | a hand, pen, or marker drawn anywhere |
| `style.frame_or_border` | a frame, border, panel edge, or whiteboard outline |
| `style.background_not_plain_white` | paper texture, a tint, or a grey or coloured background |
| `style.photographic_or_3d` | photographic, 3D-rendered, or painted look instead of marker line art |
| `style.gradients_or_grey_wash` | gradients, airbrushing, or grey washes |
| `style.element_cut_off` | any element touches or runs off the image edge |
| `elements_touching` | two or more elements touch or overlap, so they could not be told apart by white space |
| `layout_followed` | the placement broadly follows `layout_hint` |
| `matches_reference_style` | `true` / `false` against `style_reference` (line weight, lettering, figure style); leave `"n/a"` for scene 1 |
| `worstIssue` | one sentence: the most serious problem, or `""` if none |
| `requiredFix` | **required whenever anything above is wrong.** One or two sentences written as an instruction to the image model for the next attempt, naming the exact problem, for example: `Draw the scissors cutting a cord next to the price tag, and spell the sign exactly "CLOSED".` |

Colour is **not** judged: do not report an accent mix, a label colour, or a shade as a problem.

4. Run `$RS verdict --out "OUT" --scene SCENE --file "<review_dir>/filled.json"`. If it prints `FAILED checklist rejected`, fix the fields it names and run it again.

After your review passes, `verdict` also runs the **density pass** by itself: it measures each segment's drawing time from your boxes and compares it with its sentence. A segment outside 75–125% is printed as a `density_warnings` entry and the attempt still passes (the board video absorbs it with speed-ups and short pauses); only a style whose `density_mode` is `fail` turns it into a failure with a generated fix. Either way it is not your job to predict or to score. Accurate boxes make this measurement correct; the connector arrows you boxed are erased and do not count.

## Return

The `VERDICT_JSON {...}` line, verbatim.

## Rules

- Never edit images, prompts, `state.json`, or anything except your `filled.json`.
- Never average a problem away. One missing element or misspelled label is a fail, even if everything else is perfect.
- Never review an attempt you rendered, and never review the same attempt twice (a rebox is not a review: it only redraws the boxes of an already selected image).

## Rebox (already selected images; no scoring)

Scenes selected before connector arrows were boxed on their own (D60, 2026-09-29) need new boxes; the image is not re-rendered and its score does not change. `gate` and `reveal.py` refuse such a scene with `run rebox --scene N`. The orchestrator dispatches one fresh review agent per scene with this brief (fill in `OUT`, `SCENE`, `AGENT`, for example `rebox-s3`; the agent must not be the scene's render agent):

> You are a rebox review agent. Read `${CLAUDE_PLUGIN_ROOT}/references/REVIEW-AGENT.md`, sections "Inputs" (the eval rule applies) and "Rebox". OUT = `<OUT>`, SCENE = `<SCENE>`, AGENT = `<AGENT>`. Run every command in the foreground and return the final `REBOX_JSON` line verbatim, or the `FAILED` message.

Procedure:

1. Run `$RS rebox --out "OUT" --scene SCENE --agent AGENT`. It prints `REBOX_JSON {"step": "prep", ...}` naming `full` (1600 px wide), the four full-resolution `quadrants`, `boxes` (the template to fill), and `layout_hint`.
2. Read `full` and all four quadrants.
3. Copy `boxes` to `<review_dir>/filled.json` with the file-write tool and fill it: `reviewed_by` = AGENT; for every element, `box` = a fresh measurement under the `elements[].box` rule above (the element only, **never** a connector arrow). `previous_box` is the old box, which may include a connector arrow; use it only to find the element, then leave it as it is. `arrows` = one box per connector arrow or flow line under the `arrows` rule above (`[]` when there is none). Change nothing else.
4. Run `$RS rebox --out "OUT" --scene SCENE --agent AGENT --file "<review_dir>/filled.json"`. If it prints `FAILED rebox: boxes rejected`, fix the fields it names and run it again. It rebuilds the segment masks, the arrows mask, and the highlight images, and prints `REBOX_JSON {"step": "apply", ...}` with `highlights` (one image per segment) and `arrows_highlight`.
5. Read `arrows_highlight` (arrow ink at full strength inside orange-red boxes, the rest faded) and every segment highlight (that segment at full strength inside blue boxes; erased arrows are not shown). If an arrow still shows in a segment highlight, part of an element is faded where it should be full, or an element's ink shows in the arrows highlight, correct the boxes in `filled.json` and repeat step 4. A `residual_px` of more than a few hundred pixels also means a box misses ink.
