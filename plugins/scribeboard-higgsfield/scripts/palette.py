r"""palette.py - a style's colours, kept in one place: STYLES/<style>/palette.json (styles_dir in settings.json).

palette.json:
  {"colours": {"<role>": {"name": "<colour words the image model reads>", "hex": "#RRGGBB"}, ...},
   "arrows": {"<role>": weight, ...},
   "label": "<short name>", "description": "<one line>"}            (label and description optional)
Roles are free names (the house styles use ink, main, second); keys starting with "_" are comments, any other
unknown key is refused. The style guide's fenced prompt blocks (```image-prompt for scenes and character sheets,
```title-colour for the title card) name colours only through placeholders: {role} becomes the role's name, {role.cap}
the name with its first letter capitalised (a sentence start), and {role.hex} its hex code; a placeholder naming no
role is refused. stitch.py draws each flow arrow on the board in one of the roles under "arrows", picked with those
weights (seeded per arrow, in the listed order). Every colour must be darker (luminance, PIL "L") than the style's
module.json ink_threshold, or the pipeline would not see it as ink. The board itself is always white: the compositor
draws darkness on white.

Library only: prompt_block(styles, style, kind, run=None), arrow_colours(styles, style, run=None),
load(styles, style, run=None), load_file(path, threshold) for a palette file outside a style folder (the named
palettes in palettes/).

A run with a setup (scripts/runsetup.py: RUN/setup.json, required whenever Higgsfield makes the pictures or the
narration) takes its colours from the palette chosen on its setup page, never from the style's palette.json: pass
`run`. With setup required, a call without `run`, or for a run whose setup.json is missing or incomplete, is refused
(no silent fallback to the style's colours). A named palette has 2, 3 or 4 colours (roles ink, main, second, third).
A style guide may hold a block per colour count: ```<kind>:<N>``` is used for an N-colour palette when present, else
```<kind>```; the chosen block must name only the palette's roles.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import runsetup   # scripts/runsetup.py: the run's setup answers (the chosen palette)

PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)(\.hex|\.cap)?\}")
HEX = re.compile(r"#[0-9A-Fa-f]{6}")


class PaletteError(Exception):
    pass


def _read(p: Path) -> dict:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PaletteError(f"cannot read {p}: {e}")
    if not isinstance(doc, dict):
        raise PaletteError(f"{p} must hold a JSON object")
    return doc


def _keys(d: dict, allowed: tuple, where: str) -> None:
    bad = [k for k in d if not k.startswith("_") and k not in allowed]
    if bad:
        raise PaletteError(f"{where}: unknown key(s) {', '.join(bad)} (known: {', '.join(allowed)})")


def load(styles: Path, style: str, run: Path | None = None) -> dict:
    """The run's palette (see load_file), checked against the style's module.json ink_threshold: with setup required,
    the palette chosen on the run's setup page (RUN/setup.json); otherwise the style's own palette.json."""
    mod = styles / style / "module.json"
    thr = _read(mod).get("ink_threshold") if mod.is_file() else None
    if runsetup.required():
        if run is None:
            raise PaletteError("the colours come from the run's setup (RUN/setup.json), but no run was given; "
                               "this is a code bug: pass the run folder")
        try:
            p = runsetup.palette_file(run)
        except runsetup.SetupError as e:
            raise PaletteError(str(e)) from e
        return load_file(p, thr, f"{style}/{mod.name} ink_threshold")
    p = styles / style / "palette.json"
    if not p.is_file():
        raise PaletteError(f"no palette at {p}: a style keeps its colours there")
    return load_file(p, thr, f"{mod.name} ink_threshold")


def load_file(p: Path, threshold: float | None = None, threshold_from: str = "ink_threshold") -> dict:
    """{"colours": {role: {"name", "hex", "rgb"}}, "arrows": [(role, weight)], "label", "description"}, validated.
    Every colour must be darker (luminance) than threshold when one is given; threshold_from names its source."""
    doc = _read(p)
    _keys(doc, ("colours", "arrows", "label", "description"), str(p))
    for k in ("label", "description"):
        if k in doc and (not isinstance(doc[k], str) or not doc[k].strip()):
            raise PaletteError(f"{p}: {k} must be a non-empty string, not {doc[k]!r}")
    cols = doc.get("colours")
    if not isinstance(cols, dict) or not any(not k.startswith("_") for k in cols):
        raise PaletteError(f"{p}: colours must be an object of roles")
    out = {}
    for role, c in cols.items():
        if role.startswith("_"):
            continue
        where = f"{p}: colours.{role}"
        if not re.fullmatch(r"[A-Za-z_]\w*", role):
            raise PaletteError(f"{where}: a role is a plain identifier (letters, digits, _)")
        if not isinstance(c, dict):
            raise PaletteError(f"{where} must be an object with name and hex")
        _keys(c, ("name", "hex"), where)
        name, hx = c.get("name"), c.get("hex")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise PaletteError(f"{where}.name must be a non-empty string without outer spaces, not {name!r}")
        if not isinstance(hx, str) or not HEX.fullmatch(hx):
            raise PaletteError(f"{where}.hex must look like #RRGGBB, not {hx!r}")
        rgb = tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))
        lum = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000   # PIL "L"
        if isinstance(threshold, (int, float)) and lum >= threshold:
            raise PaletteError(f"{where}: {hx} has luminance {lum}, not below {threshold_from} {threshold}; "
                               f"the pipeline would not see it as ink (pick a darker colour)")
        out[role] = {"name": name, "hex": hx, "rgb": rgb}
    arr = doc.get("arrows")
    if not isinstance(arr, dict):
        raise PaletteError(f"{p}: arrows must be an object of role: weight")
    arrows = []
    for role, w in arr.items():
        if role.startswith("_"):
            continue
        if role not in out:
            raise PaletteError(f"{p}: arrows.{role} names no colour (roles: {', '.join(out)})")
        if not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
            raise PaletteError(f"{p}: arrows.{role} must be a weight > 0, not {w!r}")
        arrows.append((role, w))
    if not arrows:
        raise PaletteError(f"{p}: arrows must list at least one role")
    return {"colours": out, "arrows": arrows, "label": doc.get("label"), "description": doc.get("description")}


def arrow_colours(styles: Path, style: str, run: Path | None = None) -> tuple[tuple[str, tuple[int, int, int], float], ...]:
    """The flow arrows' colours: (colour name, rgb, share of the arrows), in palette order; shares sum to 1."""
    pal = load(styles, style, run)
    total = sum(w for _, w in pal["arrows"])
    return tuple((pal["colours"][r]["name"], pal["colours"][r]["rgb"], w / total) for r, w in pal["arrows"])


def _block(text: str, kind: str) -> str | None:
    m = re.search(rf"```{re.escape(kind)}[ \t]*\n(.*?)\n```", text, re.S)
    return " ".join(m.group(1).split()) if m else None


def prompt_block(styles: Path, style: str, kind: str, run: Path | None = None) -> str:
    """The style guide's ```<kind>:<N> block for an N-colour palette, else its ```<kind> block, whitespace collapsed to
    single spaces, its placeholders filled from the run's palette (read only when the block holds a placeholder)."""
    guide = styles / style / "style_guide.md"
    if not guide.is_file():
        raise PaletteError(f"no style guide at {guide}")
    text = guide.read_text(encoding="utf-8")
    generic = _block(text, kind)
    if generic is None:
        raise PaletteError(f"{guide} has no ```{kind} block")
    if not PLACEHOLDER.search(generic) and not runsetup.required():
        return generic
    pal = load(styles, style, run)
    cols = pal["colours"]
    block = _block(text, f"{kind}:{len(cols)}") or generic
    unused = [r for r in cols if r not in {x for x, _ in PLACEHOLDER.findall(block)}]
    if unused:   # a colour the prompt never names would never be drawn: the style needs a block for this count
        raise PaletteError(f"{guide} ```{kind}: the palette's colour role(s) {', '.join(unused)} are not named; add a "
                           f"```{kind}:{len(cols)} block for {len(cols)}-colour palettes")
    unknown = sorted({r for r, _ in PLACEHOLDER.findall(block) if r not in cols})
    if unknown:
        raise PaletteError(f"{guide} ```{kind}: placeholder(s) {', '.join('{' + r + '}' for r in unknown)} name no "
                           f"colour of the palette (roles: {', '.join(cols)}); add a ```{kind}:{len(cols)} block "
                           f"for {len(cols)}-colour palettes")

    def one(x: re.Match) -> str:
        c = cols[x.group(1)]
        return c["hex"] if x.group(2) == ".hex" else c["name"][:1].upper() + c["name"][1:] if x.group(2) else c["name"]
    return PLACEHOLDER.sub(one, block)
