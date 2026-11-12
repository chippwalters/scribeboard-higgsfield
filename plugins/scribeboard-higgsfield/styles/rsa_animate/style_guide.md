# Style guide: RSA Animate

Visual language taken from the RSA Animate video *Drive* (Cognitive Media, 2010): black chisel-marker line art, cartoon figures with big expressive heads, visual metaphors, speech bubbles, arrows, and short hand-lettered ALL-CAPS labels on a plain white board. Colours are the CHIPPWALTERS brand accents, with pure black instead of the brand's dark grey (user, 2026-09-27).

## Palette

The colours live in `palette.json` next to this guide, the one place to change them (`scripts/palette.py`). The prompt blocks below name colours only as `{role}` (the colour's name) and `{role.hex}` (its hex code); the board's flow arrows (`stitch.py board`) use the roles under `arrows` in `palette.json`.

| Role | Use |
|---|---|
| Board | Always white `#FFFFFF` (not a palette colour: the compositor draws darkness on white) |
| `ink` | Line work and most lettering |
| `main` | Main accent: emphasis words, arrows, key highlights |
| `second` | Second accent: secondary objects and light shading, used less than `main` |

Colours are targets for the image model. Nothing measures them, and no scene is re-rendered because of colour.

## Flow between segments

A scene is narrated sentence by sentence, and each sentence reveals its own group of drawings. The groups are placed in reading order, each standing on its own with clear white space around it. No connector arrows or lines lead from one group to the next (user, 2026-09-29, D60): after Gate 1 the segments are cut apart and packed onto one board, where a connector would point at nothing. If the model draws one anyway, the reviewer boxes it as an arrow, and the pipeline erases it.

## Animation rules

- Reveal order within an element: outline, then interior detail, then text, then fills.
- Fills (small solid areas) are revealed as three wipes, one per diagonal band.
- No hand or pen overlay.

## Image prompt

The renderer (`scripts/render_scenes.py`) appends this block, its colour placeholders filled from `palette.json`, to every scene's element list; character sheets (`scripts/characters.py`) use it too.

```image-prompt
Drawn as a hand-drawn whiteboard marker illustration in the style of an RSA Animate explainer: confident pure-{ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour: {ink} line work, one main accent in {main} ({main.hex}) for emphasis words, arrows and key highlights, and a second accent in {second} ({second.hex}) used less often for secondary objects and light shading. No other colours. Lettering is mostly {ink}; only the most important word may be {main} or {second}. The groups of drawings are arranged in reading order, each standing on its own; no connector arrows or lines lead from one group to another. Minimal hatching, no gradients, no grey wash, no grey fills; solid fills only for small areas such as hair or a small accent patch. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted, with no added punctuation.
```

The block above is for a three-colour palette (`ink`, `main`, `second`). A run whose chosen palette has two or four colours (`scripts/palette.py`, the setup page) uses the matching block below; only the colour sentences differ.

```image-prompt:2
Drawn as a hand-drawn whiteboard marker illustration in the style of an RSA Animate explainer: confident pure-{ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour: {ink} line work and one accent in {main} ({main.hex}) for emphasis words, arrows, key highlights and light shading. No other colours. Lettering is mostly {ink}; only the most important word may be {main}. The groups of drawings are arranged in reading order, each standing on its own; no connector arrows or lines lead from one group to another. Minimal hatching, no gradients, no grey wash, no grey fills; solid fills only for small areas such as hair or a small accent patch. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted, with no added punctuation.
```

```image-prompt:4
Drawn as a hand-drawn whiteboard marker illustration in the style of an RSA Animate explainer: confident pure-{ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour: {ink} line work, one main accent in {main} ({main.hex}) for emphasis words, arrows and key highlights, a second accent in {second} ({second.hex}) used less often for secondary objects, and a third accent in {third} ({third.hex}) used least, for small fills and light shading. No other colours. Lettering is mostly {ink}; only the most important word may be {main}, {second} or {third}. The groups of drawings are arranged in reading order, each standing on its own; no connector arrows or lines lead from one group to another. Minimal hatching, no gradients, no grey wash, no grey fills; solid fills only for small areas such as hair or a small accent patch. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted, with no added punctuation.
```

## Title colours

The title card prompt (`scripts/title.py`) takes its colour sentence from this block (`title-colour:2` / `title-colour:4` for two- and four-colour palettes).

```title-colour
confident pure-{ink} marker strokes, with the most important word(s) in {main} ({main.hex}); a touch of {second} ({second.hex}) is allowed but optional. No other colours.
```

```title-colour:2
confident pure-{ink} marker strokes, with the most important word(s) in {main} ({main.hex}). No other colours.
```

```title-colour:4
confident pure-{ink} marker strokes, with the most important word(s) in {main} ({main.hex}); a touch of {second} ({second.hex}) or {third} ({third.hex}) is allowed but optional. No other colours.
```
