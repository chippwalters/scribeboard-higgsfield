#!/usr/bin/env python
r"""reveal.py - Scribeboard drawing analysis: how each segment of a scene image is drawn stroke by stroke, and the
shared video encoder. A library (no commands): stitch.py renders every segment onto the board from these reveal-time
maps, and render_scenes.py's density check uses the per-segment drawing times.

Contract: .orchestrator/contracts/run-folder.md. Style: the run's style's module.json (styles_dir in settings.json;
MODULE_KEYS), e.g. styles/rsa_animate/module.json.

Inputs per scene: scenes/scene_N_4k.png, scenes/scene_N_segments.json (+ its segment, residual and arrows masks),
alignment.json; narration/narration.wav for decode_narration.

Method (no hand; only the image's own pixels are revealed; connector-arrow ink is in no segment and never drawn)
  1. Ink = luminance (PIL "L") < ink_threshold, at 4K. A segment's ink = its mask AND ink.
  2. Fills: ink more than fill_width_px/2 from the nearest non-ink pixel, grown back by that radius
     (a morphological opening), per segment, minus its boundary band of OUTLINE_PX (about one line width): the
     band, i.e. the outline around a solid area, is drawn with the strokes; only the interior is wiped. Each fill
     is revealed after its segment's strokes as fill_wipes sequential wipes, one per diagonal band (x+y split into
     equal ranges); each wipe runs along its band (increasing x-y) at a constant area rate; a fill takes
     area / fill_speed_px2_s.
  3. Strokes: skeleton of the segment's non-fill ink, split into paths at junctions. Paths are drawn element by
     element (segment order; a path belongs to the element box holding most of it), inside an element by greedy
     nearest-endpoint chaining from its top-left-most endpoint (min x+y), each path entered at the end nearest
     the pen (closed loops at their pixel nearest the pen). Time along a path = cumulative length /
     drawing_speed_px_s; pen lifts take no time. A segment takes draw_s = strokes + fills.
  4. reveal_map: a stroke-ink pixel takes the time of its nearest skeleton pixel in the same segment; a fill pixel
     takes its wipe time; every other pixel of the requested box takes the time of its nearest ink pixel.
  5. encode: raw RGB frames + mono float audio piped to ffmpeg (H.264 crf 18 yuv420p BT.709 tv, AAC 192k,
     +faststart), probed before the file is put in place.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np

import settings as cfg   # scripts/settings.py: styles_dir
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

STYLES = cfg.path("styles_dir")
MODULE_KEYS = ("drawing_speed_px_s", "fill_speed_px2_s", "fill_width_px", "fill_wipes", "ink_threshold")
SCENE_W, SCENE_H = 3840, 2160
FPS = 30
SR = 48000
FULL_SIZE = (1920, 1080)
PREVIEW_SIZE = (960, 540)
FADE_S = 0.06                  # a pixel fades in over this long once its reveal time has passed
MISMATCH_MAX = 0.005           # max share of ink where the segment masks and the luminance threshold disagree
OUTLINE_PX = 18                # a fill's boundary band this wide (4K px, about one line width) is drawn as strokes
NB8 = ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (-1, 1), (1, -1), (1, 1))   # orthogonal first: walks keep staircases


class Fail(Exception):
    """A step failed. The message says which and why."""


def say(msg: str) -> None:
    print(f"[reveal] {msg}", flush=True)


def r3(x: float) -> float:
    return round(float(x), 3)


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Fail(f"missing {p}")
    except ValueError as e:
        raise Fail(f"bad JSON in {p}: {e}")


def write_json(p: Path, doc: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def run_style(run: Path) -> str:
    """The run's style, as render_scenes.py init recorded it in RUN/scenes/state.json."""
    style = read_json(run / "scenes" / "state.json").get("style")
    if not isinstance(style, str) or not style:
        raise Fail(f"{run / 'scenes' / 'state.json'} names no style")
    return style


def module_path(style: str) -> Path:
    return STYLES / style / "module.json"


def load_module(style: str) -> dict:
    mod = read_json(module_path(style))
    bad = [k for k in MODULE_KEYS if not isinstance(mod.get(k), (int, float)) or isinstance(mod.get(k), bool)]
    if bad:
        raise Fail(f"module: {module_path(style)} lacks numeric {', '.join(bad)}")
    for k in ("drawing_speed_px_s", "fill_speed_px2_s", "fill_width_px", "fill_wipes"):
        if mod[k] <= 0:
            raise Fail(f"module: {k} must be > 0, got {mod[k]}")
    if int(mod["fill_wipes"]) != mod["fill_wipes"]:
        raise Fail(f"module: fill_wipes must be an integer, got {mod['fill_wipes']}")
    return mod


# ------------------------------------------------------------------ inputs

def load_alignment(run: Path) -> dict:
    al = read_json(run / "alignment.json")
    for k in ("sample_rate", "duration_s", "segments", "scenes"):
        if k not in al:
            raise Fail(f"alignment.json: missing '{k}'")
    if al["sample_rate"] != SR:
        raise Fail(f"alignment.json: sample_rate {al['sample_rate']}, expected {SR}")
    return al


def scene_speech(al: dict, n: int) -> tuple[dict, list[dict]]:
    sc = [s for s in al["scenes"] if s.get("id") == n]
    if len(sc) != 1:
        raise Fail(f"alignment.json: scene {n} listed {len(sc)} times")
    sc = sc[0]
    segs = [s for s in al["segments"] if s.get("scene") == n]
    if not segs:
        raise Fail(f"alignment.json: scene {n} has no segments")
    if not sc["start_s"] <= segs[0]["start_s"] + 1e-6 or sc["end_s"] <= sc["start_s"]:
        raise Fail(f"alignment.json: scene {n} span {sc['start_s']}-{sc['end_s']} does not hold its first segment")
    last = sc["start_s"]
    for s in segs:
        if not last - 1e-6 <= s["start_s"] < sc["end_s"]:
            raise Fail(f"alignment.json: segment {s['id']} start {s['start_s']} is out of order or outside scene {n}")
        last = s["start_s"]
    return sc, segs


def open_png(p: Path, mode: str):
    from PIL import Image
    try:
        im = Image.open(p)
        im.load()
    except FileNotFoundError:
        raise Fail(f"missing {p}")
    except OSError as e:
        raise Fail(f"cannot read {p}: {e}")
    if im.mode != mode or im.size != (SCENE_W, SCENE_H):
        raise Fail(f"{p.name}: expected {mode} {SCENE_W}x{SCENE_H}, got {im.mode} {im.size[0]}x{im.size[1]}")
    return im


def check_box(box, where: str) -> list[int]:
    if (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, int) for v in box)
            or not (0 <= box[0] < box[2] <= SCENE_W and 0 <= box[1] < box[3] <= SCENE_H)):
        raise Fail(f"{where}: bad box {box}")
    return box


def load_scene(run: Path, n: int, mod: dict, speech_ids: list[str]) -> dict:
    """Scene image, ink, and the ink of every segment (+ residual) per the segments file. Connector-arrow ink
    (`arrows_mask`) is in no group, so it is never drawn; `arrow` marks it, and `ink` still includes it."""
    sdir = run / "scenes"
    doc = read_json(sdir / f"scene_{n}_segments.json")
    where = f"scene_{n}_segments.json"
    if doc.get("scene") != n:
        raise Fail(f"{where}: scene is {doc.get('scene')}, expected {n}")
    if doc.get("size") != [SCENE_W, SCENE_H]:
        raise Fail(f"{where}: size {doc.get('size')}, expected [{SCENE_W}, {SCENE_H}]")
    if doc.get("ink_threshold") != mod["ink_threshold"]:
        raise Fail(f"{where}: ink_threshold {doc.get('ink_threshold')} differs from the module's {mod['ink_threshold']}")
    ids = [s.get("id") for s in doc.get("segments", [])]
    if ids != speech_ids:
        raise Fail(f"{where}: segments {ids} do not match alignment.json scene {n} segments {speech_ids}")
    for key in ("residual", "arrows_mask"):
        if not isinstance(doc.get(key), dict):
            raise Fail(f"{where}: missing {key}" + ("; it predates the connector-arrow split: run "
                                                     f"`render_scenes.py rebox --scene {n}`" if key == "arrows_mask" else ""))

    im = open_png(sdir / f"scene_{n}_4k.png", "RGB")
    rgb = np.asarray(im)
    ink = np.asarray(im.convert("L")) < mod["ink_threshold"]
    total = int(ink.sum())
    if total == 0:
        raise Fail(f"scene_{n}_4k.png has no ink")

    taken = np.zeros((SCENE_H, SCENE_W), bool)
    groups = []
    arrow = np.zeros((SCENE_H, SCENE_W), bool)
    entries = [(s["id"], s, s.get("elements")) for s in doc["segments"]] + [("residual", doc["residual"], []),
                                                                             ("arrows", doc["arrows_mask"], [])]
    for gid, entry, elements in entries:
        mp = sdir / str(entry.get("mask"))
        m = np.asarray(open_png(mp, "L"))
        vals = np.unique(m)
        if not set(vals.tolist()) <= {0, 255}:
            raise Fail(f"{mp.name}: mask values must be 0 or 255, found {vals[:6].tolist()}")
        m = m == 255
        cnt = int(m.sum())
        if cnt != entry.get("ink_px"):
            raise Fail(f"{mp.name}: {cnt} ink px, {where} says {entry.get('ink_px')}")
        if (taken & m).any():
            raise Fail(f"{mp.name}: overlaps an earlier mask ({int((taken & m).sum())} px); masks must be disjoint")
        taken |= m
        if gid == "arrows":
            arrow = m & ink
            continue
        if gid != "residual":
            if not isinstance(elements, list) or not elements:
                raise Fail(f"{where}: segment {gid} has no elements")
            boxes = [check_box(e.get("box"), f"{where} segment {gid} element {e.get('id')}") for e in elements]
        else:
            boxes = []
        groups.append({"id": gid, "ink": m & ink, "boxes": boxes, "elements": [e.get("id") for e in elements]})
    orphan = int((ink & ~taken).sum())
    extra = int((taken & ~ink).sum())
    if orphan + extra > MISMATCH_MAX * total:
        raise Fail(f"{where}: masks disagree with ink (< {mod['ink_threshold']}): {orphan} ink px outside every mask, "
                   f"{extra} mask px that are not ink, of {total} ink px")
    return {"rgb": rgb, "ink": ink, "arrow": arrow, "groups": groups, "ink_px": total, "orphan_px": orphan, "extra_px": extra}


# ------------------------------------------------------------------ strokes and fills

def trace_paths(skel: np.ndarray) -> list[tuple[np.ndarray, bool]]:
    """Split a one-pixel skeleton into simple paths at junctions (pixels with 3+ neighbours, left out).
    Returns (points [N, 2] as (y, x), closed) per path. `skel` must have an empty 1-px border."""
    from scipy import ndimage
    k8 = np.ones((3, 3), np.uint8)
    k8[1, 1] = 0
    deg_s = ndimage.convolve(skel.astype(np.uint8), k8, mode="constant")
    chain = skel & (deg_s < 3)
    ys, xs = np.nonzero(chain)
    n = ys.size
    if n == 0:
        return []
    idx = np.full(skel.shape, -1, np.int64)
    idx[ys, xs] = np.arange(n)
    nbr = np.stack([idx[ys + dy, xs + dx] for dy, dx in NB8], axis=1)
    deg = (nbr >= 0).sum(1)
    nbr_l = nbr.tolist()
    seen = bytearray(n)

    def walk(start: int) -> list[int]:
        seq = [start]
        seen[start] = 1
        cur = start
        while True:
            nxt = -1
            for j in nbr_l[cur]:
                if j >= 0 and not seen[j]:
                    nxt = j
                    break
            if nxt < 0:
                return seq
            seen[nxt] = 1
            seq.append(nxt)
            cur = nxt

    pts = np.stack([ys, xs], axis=1).astype(np.int32)
    paths = []
    for s in np.nonzero(deg <= 1)[0].tolist():
        if not seen[s]:
            paths.append((pts[walk(s)], False))
    for s in range(n):                                   # what is left: closed loops (every pixel has 2 neighbours)
        if not seen[s]:
            seq = walk(s)
            closed = len(seq) > 2 and max(abs(int(ys[seq[-1]]) - int(ys[s])), abs(int(xs[seq[-1]]) - int(xs[s]))) <= 1
            paths.append((pts[seq], closed))
    return paths


def cumlen(p: np.ndarray, closed: bool) -> tuple[np.ndarray, float]:
    """Cumulative length at each pixel (first pixel counts 1) and the path's total length."""
    if len(p) > 1:
        steps = np.sqrt((np.diff(p, axis=0).astype(np.float64) ** 2).sum(1))
        cum = np.concatenate([[1.0], 1.0 + np.cumsum(steps)])
    else:
        cum = np.array([1.0])
    total = float(cum[-1])
    if closed:
        total += float(np.sqrt(((p[-1] - p[0]).astype(np.float64) ** 2).sum()))
    return cum, total


def element_of(pts: np.ndarray, emap: np.ndarray, boxes_local: list[list[int]]) -> int:
    """Element whose box holds most of these (local) points; none -> the box nearest their middle point."""
    if len(boxes_local) <= 1:
        return 0
    lab = emap[pts[:, 0], pts[:, 1]]
    counts = np.bincount(lab[lab >= 0], minlength=len(boxes_local))
    if counts.sum() > 0:
        return int(np.argmax(counts))
    y, x = pts[len(pts) // 2]
    d = [max(x0 - x, 0, x - (x1 - 1)) ** 2 + max(y0 - y, 0, y - (y1 - 1)) ** 2 for x0, y0, x1, y1 in boxes_local]
    return int(np.argmin(d))


def chain(items: list[dict], start_rule: bool, pen: np.ndarray | None) -> list[tuple[int, int]]:
    """Greedy nearest-entry ordering. Each item has 'entries' [M, 2] (y, x). Returns (item index, entry index)
    in drawing order. The first item is the one with the top-left-most entry (min x+y, then min y) when
    start_rule is set, else the entry nearest `pen`."""
    if not items:
        return []
    ent = np.concatenate([it["entries"] for it in items]).astype(np.float64)
    owner = np.concatenate([np.full(len(it["entries"]), i) for i, it in enumerate(items)])
    local = np.concatenate([np.arange(len(it["entries"])) for it in items])
    lo = np.concatenate([[0], np.cumsum([len(it["entries"]) for it in items])])
    alive = np.ones(len(ent), bool)
    order = []
    if start_rule or pen is None:
        j = int(np.lexsort((ent[:, 0], ent[:, 0] + ent[:, 1]))[0])
    else:
        j = int(np.argmin(((ent - pen) ** 2).sum(1)))
    for _ in range(len(items)):
        i = int(owner[j])
        order.append((i, int(local[j])))
        alive[lo[i]:lo[i + 1]] = False
        if not alive.any():
            break
        pen = items[i]["exit_of"](int(local[j])).astype(np.float64)
        d = ((ent - pen) ** 2).sum(1)
        d[~alive] = np.inf
        j = int(np.argmin(d))
    return order


def analyse_group(g: dict, mod: dict) -> dict:
    """Strokes and fills of one segment (or the residual), with drawing-order local times in seconds."""
    from scipy import ndimage
    from skimage.morphology import skeletonize
    ink = g["ink"]
    rows = np.nonzero(ink.any(1))[0]
    cols = np.nonzero(ink.any(0))[0]
    out = {"id": g["id"], "paths": 0, "stroke_len_px": 0.0, "fills": 0, "fill_px": 0, "stroke_s": 0.0,
           "fill_s": 0.0, "draw_s": 0.0, "ink_px": int(ink.sum())}
    if rows.size == 0:
        out.update(origin=(0, 0), stroke=None)
        return out
    y0, y1, x0, x1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    crop = np.pad(ink[y0:y1, x0:x1], 1)                  # empty border: image edges count as non-ink
    oy, ox = y0 - 1, x0 - 1                               # global = local + origin
    r = mod["fill_width_px"] / 2
    core = ndimage.distance_transform_edt(crop) > r
    if core.any():                                        # opening, then the outline band goes back to the pen
        grown = crop & (ndimage.distance_transform_edt(~core) <= r)
        fill = ndimage.distance_transform_edt(grown) > OUTLINE_PX
    else:
        fill = np.zeros_like(crop)
    stroke = crop & ~fill

    boxes = [[bx0 - ox, by0 - oy, bx1 - ox, by1 - oy] for bx0, by0, bx1, by1 in g["boxes"]]
    emap = np.full(crop.shape, -1, np.int16)
    for e in reversed(range(len(boxes))):                 # overlaps go to the earlier-listed element
        bx0, by0, bx1, by1 = boxes[e]
        emap[max(by0, 0):max(by1, 0), max(bx0, 0):max(bx1, 0)] = e
    n_el = max(len(boxes), 1)

    # strokes
    speed = float(mod["drawing_speed_px_s"])
    items = []
    for p, closed in trace_paths(skeletonize(stroke)):
        cum, total = cumlen(p, closed)
        ends = p if closed else p[[0, -1]]
        it = {"p": p, "closed": closed, "cum": cum, "len": total, "entries": ends, "el": element_of(p, emap, boxes)}
        if closed:
            it["exit_of"] = lambda k, p=p: p[k]          # a loop ends where it started
        else:
            it["exit_of"] = lambda k, p=p: p[-1] if k == 0 else p[0]
        items.append(it)
    skel_t = np.full(crop.shape, np.nan, np.float64)
    t = 0.0
    pen = None
    for e in range(n_el):
        sub = [it for it in items if it["el"] == e]
        for i, k in chain(sub, True, None):
            it = sub[i]
            p, cum = it["p"], it["cum"]
            if it["closed"]:
                p = np.roll(p, -k, axis=0)
                steps_closed = np.sqrt((np.diff(np.concatenate([p, p[:1]]), axis=0).astype(np.float64) ** 2).sum(1))
                cum = np.concatenate([[1.0], 1.0 + np.cumsum(steps_closed[:-1])])
            elif k == 1:
                p = p[::-1]
                cum = it["len"] + 1.0 - cum[::-1]
            skel_t[p[:, 0], p[:, 1]] = t + cum / speed
            t += it["len"] / speed
            pen = p[0] if it["closed"] else p[-1]
    out["paths"] = len(items)
    out["stroke_len_px"] = float(sum(it["len"] for it in items))
    out["stroke_s"] = t

    # fills: one wipe per diagonal band, bands in x+y order, each wipe along its band (x-y), constant area rate
    fspeed = float(mod["fill_speed_px2_s"])
    wipes = int(mod["fill_wipes"])
    lab, nf = ndimage.label(fill, structure=np.ones((3, 3), bool))
    fills = []
    if nf:
        fy, fx = np.nonzero(lab)
        fl = lab[fy, fx]
        srt = np.argsort(fl, kind="stable")
        bounds = np.searchsorted(fl[srt], np.arange(1, nf + 2))
        for c in range(nf):
            sel = srt[bounds[c]:bounds[c + 1]]
            ys, xs = fy[sel], fx[sel]
            s = xs + ys + ox + oy                         # global diagonal coordinate
            u = (xs + ox) - (ys + oy)
            band = (s - s.min()) * wipes // (s.max() - s.min() + 1)
            order = np.lexsort((s, u, band))
            pts = np.stack([ys[order], xs[order]], axis=1)
            fills.append({"p": pts, "entries": pts[:1], "exit_of": lambda k, pts=pts: pts[-1],
                          "el": element_of(pts, emap, boxes)})
    fill_t = np.full(crop.shape, np.nan, np.float64)
    tf = t
    for e in range(n_el):
        sub = [f for f in fills if f["el"] == e]
        for i, _ in chain(sub, False, pen):
            pts = sub[i]["p"]
            fill_t[pts[:, 0], pts[:, 1]] = tf + np.arange(1, len(pts) + 1) / fspeed
            tf += len(pts) / fspeed
            pen = pts[-1]
    out["fills"] = len(fills)
    out["fill_px"] = int(fill.sum())
    out["fill_s"] = tf - t
    out["draw_s"] = tf
    out.update(origin=(oy, ox), stroke=stroke, fill=fill, skel_t=skel_t, fill_t=fill_t)
    return out


def stats(a: dict) -> dict:
    return {"id": a["id"], "ink_px": a["ink_px"], "paths": a["paths"], "stroke_len_px": round(a["stroke_len_px"]),
            "fills": a["fills"], "fill_px": a["fill_px"], "stroke_s": r3(a["stroke_s"]), "fill_s": r3(a["fill_s"]),
            "draw_s": r3(a["draw_s"])}


# ------------------------------------------------------------------ reveal-time map

def reveal_map(a: dict, box: tuple[int, int, int, int]) -> np.ndarray:
    """Seconds after its draw start at which each pixel of one analysed group appears, over `box` (x0, y0, w, h in
    source 4K px, holding the group's ink bbox): stroke ink takes the time of its nearest skeleton pixel, fill ink
    its wipe time, every other pixel the time of its nearest timed ink pixel."""
    from scipy import ndimage
    if a.get("stroke") is None:
        raise Fail(f"segment {a['id']}: no ink to reveal")
    x0, y0, w, h = box
    oy, ox = a["origin"]
    hh, ww = a["stroke"].shape
    if not (x0 <= ox + 1 and y0 <= oy + 1 and ox + ww - 1 <= x0 + w and oy + hh - 1 <= y0 + h):
        raise Fail(f"segment {a['id']}: reveal box {box} does not hold its ink")
    tloc = np.full((hh, ww), np.inf, np.float64)
    st = a["stroke"]
    if st.any():
        timed = ~np.isnan(a["skel_t"])
        if not timed.any():
            raise Fail(f"segment {a['id']}: stroke ink but no skeleton path")
        iy, ix = ndimage.distance_transform_edt(~timed, return_distances=False, return_indices=True)
        tloc[st] = a["skel_t"][iy[st], ix[st]]
    fl = a["fill"]
    tloc[fl] = a["fill_t"][fl]
    T = np.full((h, w), np.inf, np.float32)
    T[oy + 1 - y0:oy + hh - 1 - y0, ox + 1 - x0:ox + ww - 1 - x0] = tloc[1:-1, 1:-1]   # the crop without its padding
    unset = ~np.isfinite(T)
    if unset.all():
        raise Fail(f"segment {a['id']}: no ink pixel received a reveal time")
    iy, ix = ndimage.distance_transform_edt(unset, return_distances=False, return_indices=True)
    return T[iy, ix]


# ------------------------------------------------------------------ audio + video

def decode_narration(run: Path, al: dict) -> np.ndarray:
    wav = run / "narration" / "narration.wav"
    if not wav.exists():
        raise Fail(f"missing {wav}")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-f", "f32le", "-ac", "1",
                        "-ar", str(SR), "-"], capture_output=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"ffmpeg could not decode {wav}: {p.stderr.decode(errors='replace').strip()[-400:]}")
    a = np.frombuffer(p.stdout, np.float32)
    if abs(a.size / SR - al["duration_s"]) > 0.05:
        raise Fail(f"{wav.name} is {a.size / SR:.3f} s, alignment.json says {al['duration_s']} s")
    return a


def probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,"
                        "color_range,r_frame_rate,nb_frames,sample_rate,channels,bit_rate:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise Fail(f"ffprobe {path.name}: {p.stderr.strip()[-400:]}")
    return json.loads(p.stdout)


def encode(frame_iter, size: tuple[int, int], frames: int, audio: np.ndarray, out: Path, preset: str) -> dict:
    """Pipe `frames` raw RGB frames of `size` plus mono float audio into out (H.264 crf 18 yuv420p BT.709 tv,
    AAC 192k, +faststart), via a .partial.mp4 that is probed before it replaces out."""
    w, h = size
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".partial.mp4")
    wav = out.with_name(out.stem + ".audio.f32")
    audio.astype(np.float32).tofile(wav)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", str(FPS), "-i", "-",
           "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(wav),
           "-map", "0:v:0", "-map", "1:a:0",
           "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           "-c:v", "libx264", "-preset", preset, "-crf", "18", "-pix_fmt", "yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    fsize = w * h * 3
    try:
        count = 0
        for buf in frame_iter:
            if len(buf) != fsize:
                raise Fail(f"frame {count} is {len(buf)} bytes, expected {fsize} ({w}x{h} RGB)")
            proc.stdin.write(buf)
            count += 1
        if count != frames:
            raise Fail(f"frame source gave {count} frames, expected {frames}")
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        rc = proc.wait()
    except BaseException as e:
        proc.kill()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait()
        wav.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
        if isinstance(e, OSError):
            raise Fail(f"ffmpeg stopped taking frames ({e}): {err.strip()[-600:]}")
        raise
    wav.unlink(missing_ok=True)
    if rc != 0:
        part.unlink(missing_ok=True)
        raise Fail(f"ffmpeg exit {rc}: {err.strip()[-600:]}")
    info = probe(part)
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    au = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    want = {"codec_name": "h264", "width": w, "height": h, "pix_fmt": "yuv420p", "color_space": "bt709",
            "color_transfer": "bt709", "color_primaries": "bt709", "r_frame_rate": f"{FPS}/1", "nb_frames": str(frames)}
    got = {k2: v[0].get(k2) for k2 in want} if v else {}
    if got != want or len(au) != 1 or au[0].get("codec_name") != "aac":
        part.unlink(missing_ok=True)
        raise Fail(f"encoded file check failed: video {got} (want {want}), audio {au}")
    os.replace(part, out)
    return {"video": {k2: got[k2] for k2 in ("width", "height", "pix_fmt", "color_space", "nb_frames")},
            "audio": {k2: au[0].get(k2) for k2 in ("codec_name", "sample_rate", "channels", "bit_rate")},
            "duration_s": float(info["format"]["duration"])}


# ------------------------------------------------------------------ analysis

def prepare(run: Path, n: int, mod: dict, al: dict) -> tuple[dict, dict, list[dict]]:
    """Scene n's alignment span, its loaded image/masks, and the analysis of every group (segments in order, then
    the residual)."""
    sc, segs = scene_speech(al, n)
    scene = load_scene(run, n, mod, [s["id"] for s in segs])
    t = time.time()
    anal = [analyse_group(g, mod) for g in scene["groups"]]
    say(f"scene {n}: analysed in {time.time() - t:.1f} s; " + "; ".join(
        f"{a['id']} {a['paths']} paths {a['stroke_len_px']:.0f} px + {a['fills']} fills {a['fill_px']} px = {a['draw_s']:.2f} s"
        for a in anal))
    return sc, scene, anal
