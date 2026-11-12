# Style guide: Sketchnote

Same visual language as the RSA Animate style (black chisel-marker line art, cartoon figures with big expressive heads, visual metaphors, speech bubbles, arrows, short hand-lettered ALL-CAPS labels on a plain white board), coloured with the CHIPPWALTERS brand accents.

## Palette

The colours live in `palette.json` next to this guide, the one place to change them (`scripts/palette.py`). The prompt blocks below name colours only as `{role}` (the colour's name) and `{role.hex}` (its hex code); the board's flow arrows (`stitch.py board`) use the roles under `arrows` in `palette.json`. Drawing speeds, layout and camera are in `module.json` (copied from rsa_animate; see its `sketchnote_note`).

| Role | Use |
|---|---|
| Board | Always white `#FFFFFF` (not a palette colour: the compositor draws darkness on white) |
| `ink` | Line work |
| `main` | Main accent, about two-thirds of coloured ink: emphasis words, arrows, key highlights |
| `second` | Second accent, about one-third of coloured ink: secondary objects (a device body, a bag, clothing) and light shading |
| Lettering | About 80% `ink`; the other 20% of labels `main` or `second` |

Colours are targets for the image model. Nothing measures them, and no scene is re-rendered because of colour. The accent split varies a lot between renders of the same prompt (36–85% `main` in the September 2026 samples).

## Animation rules

- Reveal order within an element: outline, then interior detail, then text, then fills.
- Fills (small `ink`, `main` or `second` patches) are revealed as three wipes, one per diagonal band.
- No hand or pen overlay.

## Image prompt

The renderer (`scripts/render_scenes.py`) appends this block, its colour placeholders filled from `palette.json`, to every scene's element list; character sheets (`scripts/characters.py`) use it too. It was tested on the September 2026 samples in `samples/sketchnote-chatgpt/`; the lettering sentence was added afterwards. For engines that tend to draw hex codes as text (Qwen-Image), the renderer removes the bracketed hex codes.

```image-prompt
Drawn as a hand-drawn sketchnote-style whiteboard marker illustration inspired by RSA Animate explainers: confident {ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour palette: most line work is {ink}. Of all the coloured ink in the picture, about two-thirds is {main} ({main.hex}) and about one-third is {second} ({second.hex}): {main} for emphasis words, arrows, key highlights and the most important shapes; {second} for secondary accents and light shading. {main.cap} must clearly dominate the {second}. No other colours. Lettering is mostly {ink}: about four out of five labels are {ink}, and only the one or two most important words are {main} or {second}. Minimal hatching, no gradients, no grey wash; solid fills only for small {ink} areas such as hair and small {main} or {second} accent patches. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted.
```

The block above is for a three-colour palette (`ink`, `main`, `second`). A run whose chosen palette has two or four colours (`scripts/palette.py`, the setup page) uses the matching block below; only the colour sentences differ.

```image-prompt:2
Drawn as a hand-drawn sketchnote-style whiteboard marker illustration inspired by RSA Animate explainers: confident {ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour palette: most line work is {ink}, and all the coloured ink in the picture is {main} ({main.hex}): emphasis words, arrows, key highlights, the most important shapes and light shading. No other colours. Lettering is mostly {ink}: about four out of five labels are {ink}, and only the one or two most important words are {main}. Minimal hatching, no gradients, no grey wash; solid fills only for small {ink} areas such as hair and small {main} accent patches. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted.
```

```image-prompt:4
Drawn as a hand-drawn sketchnote-style whiteboard marker illustration inspired by RSA Animate explainers: confident {ink} chisel-tip marker line art with consistent line weight, cartoon figures with big expressive heads and simple bodies, clear visual metaphors, and short hand-lettered ALL-CAPS labels. Colour palette: most line work is {ink}. Of all the coloured ink in the picture, about half is {main} ({main.hex}), about a third is {second} ({second.hex}) and the rest is {third} ({third.hex}): {main} for emphasis words, arrows, key highlights and the most important shapes; {second} for secondary accents; {third} for small fills, ticks and light shading. {main.cap} must clearly dominate. No other colours. Lettering is mostly {ink}: about four out of five labels are {ink}, and only the one or two most important words are {main}, {second} or {third}. Minimal hatching, no gradients, no grey wash; solid fills only for small {ink} areas such as hair and small {main}, {second} or {third} accent patches. Every element is separated by clear white space. Pure flat white background with no paper texture, no whiteboard frame or border, no cast shadows, no hand, and no marker or pen visible anywhere. Render it clean and sharp — no noise, no speckle, no halos. Spell every label exactly as quoted.
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
