#!/usr/bin/env python
r"""stitch.py - Scribeboard board and final video: every segment and the title redrawn on one tight 16:9 board, one
pen, a camera following the pen.

Contract: .orchestrator/contracts/run-folder.md, section "Board and videos". Style: the run's styles/<style>/module.json
(drawing values, lead_in_s, segment_overlap_s, title_draw_s, title_gap_s, "layout" incl. "layout.arrows",
"camera"). Reuses reveal.py (analysis, reveal-time maps, narration decode, encoder).

Commands
  board --run RUN
      The layout step: packs the board and places the flow arrows, then writes
      videos/board.png             the whole board fully drawn, 3840 wide, 16:9 (the image Gate 2 approves)
      videos/board_layout.json     every piece's placement, every arrow, the layout values and the fingerprint of
                                   the inputs, arrow colours included (layout_fingerprints); version LAYOUT_VERSION
  build --run RUN [--preview]
      Renders the video FROM videos/board_layout.json (never packs again; refuses when it is missing or its
      fingerprint is no longer the current one):
      videos/final.mp4             1920x1080, 30 fps, H.264 crf 18 yuv420p BT.709 tv, AAC 192k, +faststart
      videos/final_timeline.json   version 2, per contract
      videos/Scene-NN.mp4          scene n's stretch of final.mp4, re-encoded with the same spec (cut rule below)
      --preview renders 960x540 (veryfast) final.mp4 + final_timeline.json to videos/preview/ only.
  plan --run RUN
      Schedule only, from the saved layout: videos/preview/final_timeline.json, no video.

Inputs: alignment.json, narration/narration.wav, scenes/scene_N_4k.png + scene_N_segments.json (segment, residual
and arrows masks), title/title_4k.png + title/title.json (status "passed" or "flagged"; Gate 1 approval is sequenced
by scribe.py).

Method
  1. Pieces: one per segment (its mask ink; connector-arrow ink removed) plus the title (all ink of title_4k.png,
     drawn as segment `title`). A piece owns every non-white pixel within PIECE_REACH px whose nearest labelled ink
     is its own (so anti-aliasing and pale tints follow their ink; pixels nearest an arrow vanish; residual specks
     join their nearest segment). Each piece has its own reveal-time map (reveal.py) in piece-local seconds.
  2. Schedule (one pen; see schedule()): the title draws from 0 in exactly title_draw_s (its stroke/fill times
     scaled; speed factor = natural draw time / title_draw_s), in a silent lead-in of title_draw_s + title_gap_s;
     the narration starts after it. Segments in story order start at max(speech start, previous draw end + gap)
     (+ lead_in_s for the first): the gap before a shot's first segment is camera.dwell_s (+ (move_s +
     arrow_draw_s)/2 when a flow arrow leads into it), waiting (at most to mid-glide) until the glide frames its
     first strokes; inside a shot (step 4) it is 0, or arrow_draw_s when a flow arrow is drawn between the two.
     A segment that would end later than the next sentence's start - the next gap draws faster, just enough, at most
     camera.max_speedup x drawing_speed_px_s.
     When a start is more than segment_overlap_s after its speech start, the excess is inserted as silence right
     before that sentence and all later speech shifts. Ending: zoom-out from max(last draw end, last speech end) +
     max(end_hold_s, dwell_s), end_zoom_s long, then end_final_hold_s.
  3. Layout: seeded scale and tilt per piece (title scale 1), ink masks at 1/DS dilated by gap_px/2.
     Compounds (glue_groups): segments of one scene drawn on or inside each other in the scene image (one's
     mask ink dilated by layout.glue.touch_px covers >= glue.min_contact_px of the other's, or >= glue.inside_share
     of the smaller ink bbox lies inside the other's; chains glue) form one rigid packing unit: the first member's
     scale and tilt, one pivot (so every member keeps its place from the scene image), one joint mask, placed at its
     first member's place in story order. Members stay separate pieces for drawing, schedule, shots and camera.
     Units are split into ROW_COUNTS contiguous rows balanced by box area and packed in story order along a serpentine
     (row 0 left to right from the title at the top-left, row 1 right to left, ...) inside a 16:9 frame with one
     band per row: each unit takes the free position touching its predecessor (mask correlation), centre in its
     band, ahead of it, closest along the row (small pieces stack, large ones march on); then a gravity pass slides
     every unit toward the middle until it touches. Every frame width in FRAME_FACTORS x every row count is
     packed; the smallest 16:9 board (all ink + margin_px, widened to 16:9) wins.
  3b. Flow arrows (board-level; connector arrows stay out of the scene images): in the
     blank gaps between two pieces consecutive in story order (piece i -> i+1; a scene change is the last piece of
     scene n -> the first of n+1), a hand-drawn marker arrow from the earlier piece toward the later one. The gap
     of a pair is its widest point M on the boundary between the two pieces' nearest-ink regions (distance
     transform of the dilated ink of every piece); pairs whose gap is at least layout.arrows.min_gap_px wide (and
     that are not two members of one compound) get an arrow, the widest gaps first, at most layout.arrows.max. The
     arrow runs from piece i's side through M to piece i+1's side, at most max_len_px long, with a seeded slight
     wobble, a seeded colour (the style's palette.json `arrows`, scripts/palette.py; the colour every gap would get
     is part of the layout fingerprint, see layout_current) and an open or filled head; every point of it (shaft
     and head, plus half its width) keeps
     clearance_px from the dilated ink, from the board edge and from earlier arrows, else it is shortened toward M
     (never below min_len_px) or dropped. The pen draws arrow i -> i+1 in camera.arrow_draw_s (shaft, then head):
     between two shots centred on the middle of the glide into piece i+1, where the window holds both pieces (that
     glide starts arrow_draw_s/2 earlier than a plain one, so the arrow is finished before piece i+1 starts, and the
     window is widened, eased in from the move's start and out by its end, where needed to hold the whole arrow);
     inside a shot right before piece i+1, in the held frame.
  4. Camera (shots), window (cx, cy, w), h = w*9/16, never narrower than MIN_WINDOW_W x the largest piece
     scale (no source px ever above 1 output px). Shots (plan_shots): the title alone, then each scene's segments
     split into runs of camera.shot_size (2 or 3) consecutive segments, never across a scene change, whose window
     (all their ink + the arrows between them, + hold_pad) is at most camera.max_shot_window_frac x the board width;
     the tightest valid split wins (single-segment shots only where the scene has no split into fitting runs, as few
     as possible). The window holds still on a shot while its segments draw and rests at least dwell_s after the last
     one; the move into shot k starts at
     max(previous draw end + dwell_s, its first draw start - move_s/2) and lasts move_s (cut short by the next move
     or the zoom-out): a quadratic Bezier in (cx, cy, log w) through a window centred on the two pieces either side
     of the cut, at least the geometric mean of the two shot widths wide, smoothstep-eased, widened where needed to
     hold every stroke of the shot revealed so far (tracked on pyramid level TRACK_LEVEL) and, in a move with a
     flow arrow, the whole arrow (3b). Ending: smoothstep zoom (log width) to the whole board.
  5. Frames: each started piece's (and arrow's) darkness (255 - rgb) and reveal map warped (cv2.warpAffine) from a
     pyramid level never downsampled more than 2x; alpha = clip((t - start - T)/FADE_S, 0, 1); layers multiplied
     onto white.
  6. Scene-NN.mp4 cut rule: scene 1 from 0 (the title opening); scene n > 1 from min(start of the camera move into
     its first segment, that segment's speech start), floored to a frame; each runs to the next scene's cut, the
     last scene to the end (zoom-out included).

Prints one `STITCH_JSON {...}` line on success; `FAILED <reason>` on stderr and exit 1 on any failure.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

import palette   # scripts/palette.py: the style's flow-arrow colours
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window

REPO = Path(__file__).resolve().parents[1]


def _load_reveal():
    spec = importlib.util.spec_from_file_location("reveal", REPO / "scripts" / "reveal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load_reveal()
Fail = rv.Fail
r3 = rv.r3
FPS, SR = rv.FPS, rv.SR
EXCERPT_TAIL_S = 0.8          # a scene excerpt runs at least this long past its last word (last frame held, silent)
EXCERPT_FADE_S = 0.03         # ...and its audio fades out over this much where the cut falls
ASPECT = 16 / 9
TITLE_ID = "title"
PIECE_REACH = 64               # 4K px: non-white pixels this close to a piece's ink belong to the piece
CROP_MARGIN = 4                # 4K px kept around a piece's pixels
LEVELS = 5                     # pyramid levels 1, 1/2 .. 1/16
ALIGN = 1 << (LEVELS - 1)      # crop sizes are multiples of this so every level halves exactly
DS = 8                         # layout masks at 1/DS scale
CONTACT = 2                    # layout cells: a piece's dilated ink comes this close to its predecessor's
ROW_COUNTS = (2, 3, 4, 5)      # serpentine bands tried
BAND_PULL = 0.3                # weight of a piece's distance from its band centre against progress
COMPACT_PASSES = 4             # gravity passes after packing
FRAME_FACTORS = tuple(float(f) for f in np.round(np.arange(0.80, 1.51, 0.05), 2))  # frame widths, x sqrt(area*16/9)
MIN_WINDOW_W = rv.FULL_SIZE[0]  # x the largest piece scale = the narrowest window (D61: no source px above 1 output px)
EDGE_PX = 24                   # board px kept between a piece being drawn and the frame edge during a move
BOARD_PNG_W = 3840             # board.png width (16:9)
LAYOUT_VERSION = 2             # 2: compounds (glued segments packed as one rigid unit)
LAYOUT_FILE = "board_layout.json"
EMPTY_PX = 400                 # board px: a cell this far from every piece's dilated ink counts as empty (fill report)
ARROW_HEAD_SHARE = 0.25        # share of camera.arrow_draw_s spent on the head (the shaft draws first)
ARROW_SHRINK = (1.0, 0.85, 0.7, 0.55, 0.4)   # tries: the arrow's ends pulled toward the gap's widest point
TRACK_LEVEL = 2               # pyramid level the camera tracks revealed ink on
TRACK_DARK = 40               # darkness (255 - rgb) that counts as visible ink for the camera
FIRST_S = 0.25                # the ink a piece draws in its first FIRST_S must be in frame when its pen starts
SHOT_SINGLE_PENALTY = 100.0  # plan_shots: extra cost of a one-segment shot in a scene of more than one segment
STITCH_KEYS = ("lead_in_s", "segment_overlap_s", "title_draw_s", "title_gap_s")
ARROW_KEYS = ("max", "min_gap_px", "clearance_px", "width_px", "min_len_px", "max_len_px", "seed")
GLUE_KEYS = ("touch_px", "min_contact_px", "inside_share")


def say(msg: str) -> None:
    print(f"[stitch] {msg}", flush=True)


# ------------------------------------------------------------------ config

def stitch_config(mod: dict) -> tuple[dict, dict]:
    """The module's schedule values, layout and camera objects, validated."""
    lay, cam = mod.get("layout"), mod.get("camera")
    if not isinstance(lay, dict) or not isinstance(cam, dict):
        raise Fail("module: the style's module.json needs 'layout' and 'camera' objects")

    def num(d: dict, where: str, k: str, lo: float, lo_ok: bool = True, hi: float | None = None) -> float:
        v = d.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise Fail(f"module: {where}{k} must be a number, got {v!r}")
        if v < lo or (v == lo and not lo_ok) or (hi is not None and v > hi):
            raise Fail(f"module: {where}{k} = {v} out of range ({'>=' if lo_ok else '>'} {lo}"
                       f"{'' if hi is None else f', <= {hi}'})")
        return float(v)

    for k in STITCH_KEYS:
        num(mod, "", k, 0)
    num(mod, "", "title_draw_s", 1 / FPS)
    num(lay, "layout.", "gap_px", 0)
    num(lay, "layout.", "tilt_deg", 0, hi=30)
    num(lay, "layout.", "margin_px", 0)
    sc = lay.get("scale")
    if (not isinstance(sc, list) or len(sc) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                              for v in sc) or not 0 < sc[0] <= sc[1]):
        raise Fail(f"module: layout.scale must be [lo, hi] with 0 < lo <= hi, got {sc!r}")
    if not isinstance(lay.get("seed"), int) or isinstance(lay.get("seed"), bool):
        raise Fail(f"module: layout.seed must be an integer, got {lay.get('seed')!r}")
    arr = lay.get("arrows")
    if not isinstance(arr, dict):
        raise Fail(f"module: layout.arrows must be an object with {', '.join(ARROW_KEYS)}")
    for k in ("max", "seed"):
        if not isinstance(arr.get(k), int) or isinstance(arr.get(k), bool) or arr[k] < 0:
            raise Fail(f"module: layout.arrows.{k} must be an integer >= 0, got {arr.get(k)!r}")
    num(arr, "layout.arrows.", "min_gap_px", 0, lo_ok=False)
    num(arr, "layout.arrows.", "clearance_px", 0)
    num(arr, "layout.arrows.", "width_px", 1)
    num(arr, "layout.arrows.", "min_len_px", 1)
    num(arr, "layout.arrows.", "max_len_px", arr["min_len_px"])
    glue = lay.get("glue")
    if not isinstance(glue, dict):
        raise Fail(f"module: layout.glue must be an object with {', '.join(GLUE_KEYS)}")
    num(glue, "layout.glue.", "touch_px", 0)
    num(glue, "layout.glue.", "min_contact_px", 1)
    num(glue, "layout.glue.", "inside_share", 0, lo_ok=False, hi=1)
    num(cam, "camera.", "move_s", 0, lo_ok=False)
    num(cam, "camera.", "hold_pad", 0, hi=0.5)
    num(cam, "camera.", "end_hold_s", rv.FADE_S)
    num(cam, "camera.", "dwell_s", 0)
    num(cam, "camera.", "max_speedup", 1)
    num(cam, "camera.", "end_zoom_s", 0, lo_ok=False)
    num(cam, "camera.", "end_final_hold_s", 1 / FPS)
    num(cam, "camera.", "arrow_draw_s", 1 / FPS, hi=cam["move_s"])
    num(cam, "camera.", "max_shot_window_frac", 0, lo_ok=False, hi=1)
    ss = cam.get("shot_size")
    if (not isinstance(ss, list) or not ss or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                                                        for v in ss) or sorted(set(ss)) != ss):
        raise Fail(f"module: camera.shot_size must be a sorted list of distinct integers >= 1, got {ss!r}")
    return lay, cam


def module_used(mod: dict, lay: dict, cam: dict) -> dict:
    """Every module.json value this build depends on (scribe.py compares them for staleness)."""
    return ({k: mod[k] for k in rv.MODULE_KEYS} | {k: mod[k] for k in STITCH_KEYS}
            | {"layout": dict(lay), "camera": dict(cam)})


# ------------------------------------------------------------------ pieces

def crop_box(m: np.ndarray) -> tuple[int, int, int, int]:
    rows = np.nonzero(m.any(1))[0]
    cols = np.nonzero(m.any(0))[0]
    x0, y0 = int(cols[0]) - CROP_MARGIN, int(rows[0]) - CROP_MARGIN
    w = -(-(int(cols[-1]) + 1 + CROP_MARGIN - x0) // ALIGN) * ALIGN
    h = -(-(int(rows[-1]) + 1 + CROP_MARGIN - y0) // ALIGN) * ALIGN
    return x0, y0, w, h


def cut(a: np.ndarray, box: tuple[int, int, int, int], fill) -> np.ndarray:
    """a[y0:y0+h, x0:x0+w], padded with `fill` where the box leaves the image."""
    x0, y0, w, h = box
    out = np.full((h, w) + a.shape[2:], fill, a.dtype)
    sx0, sy0 = max(x0, 0), max(y0, 0)
    sx1, sy1 = min(x0 + w, a.shape[1]), min(y0 + h, a.shape[0])
    out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = a[sy0:sy1, sx0:sx1]
    return out


def ownership(rgb: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per pixel: the label of its nearest labelled ink pixel, and whether it is a non-white pixel within
    PIECE_REACH of that ink (labels: -1 = unlabelled, 0..K-1 = pieces, K = dropped ink, e.g. arrows)."""
    from scipy import ndimage
    dist, (iy, ix) = ndimage.distance_transform_edt(labels < 0, return_indices=True)
    owner = labels[iy, ix]
    del iy, ix
    keep = (dist <= PIECE_REACH) & (rgb.min(axis=2) < 255)
    return owner, keep


def make_piece(pid: str, scene: int | None, source: Path, rgb: np.ndarray, region: np.ndarray, ink: np.ndarray,
               a: dict) -> dict:
    """Cropped darkness and reveal-map pyramids, layout mask and hull of one piece."""
    import cv2
    box = crop_box(region)
    reg = cut(region, box, False)
    dark = cut(255 - rgb, box, 0)
    dark[~reg] = 0
    pyr = [dark]
    for _ in range(LEVELS - 1):
        p = pyr[-1]
        pyr.append(cv2.resize(p, (p.shape[1] // 2, p.shape[0] // 2), interpolation=cv2.INTER_AREA))
    tmax = float(a["draw_s"])
    T = rv.reveal_map(a, box)
    if float(T.max()) > tmax + 1e-3:
        raise Fail(f"piece {pid}: reveal map ends at {T.max():.3f} s, after its draw time {tmax:.3f} s")
    tpyr = [T]
    for _ in range(LEVELS - 1):
        p = tpyr[-1]
        tpyr.append(np.ascontiguousarray(p.reshape(p.shape[0] // 2, 2, p.shape[1] // 2, 2).min(axis=(1, 3))))
    ic = cut(ink, box, False)
    pts = cv2.findNonZero(ic.astype(np.uint8))
    hull = cv2.convexHull(pts).reshape(-1, 2).astype(np.float64) + (box[0], box[1])
    hull = np.concatenate([hull + d for d in ((-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5))])
    return {"id": pid, "scene": scene, "source": source, "box": box, "dark": pyr, "T": tpyr, "tmin": 0.0,
            "tmax": tmax, "draw_s": tmax, "ink8": ic.reshape(box[3] // DS, DS, box[2] // DS, DS).any(axis=(1, 3)),
            "hull": hull, "ink_px": int(ic.sum()), "stats": rv.stats(a),
            "centre": ((hull[:, 0].min() + hull[:, 0].max()) / 2, (hull[:, 1].min() + hull[:, 1].max()) / 2)}


def load_title(run: Path, mod: dict) -> dict:
    tdir = run / "title"
    doc = rv.read_json(tdir / "title.json")
    if doc.get("status") not in ("passed", "flagged"):
        raise Fail(f"{tdir / 'title.json'} status is {doc.get('status')!r}; the title must be reviewed (passed or "
                   "flagged) before it goes on the board")
    src = tdir / "title_4k.png"
    rgb = np.asarray(rv.open_png(src, "RGB"))
    from PIL import Image
    ink = np.asarray(Image.fromarray(rgb).convert("L")) < mod["ink_threshold"]
    if not ink.any():
        raise Fail(f"{src} has no ink")
    t = time.time()
    a = rv.analyse_group({"id": TITLE_ID, "ink": ink, "boxes": []}, mod)
    labels = np.where(ink, 0, -1).astype(np.int8)
    owner, keep = ownership(rgb, labels)
    p = make_piece(TITLE_ID, None, src, rgb, keep & (owner == 0), ink, a)
    k = mod["title_draw_s"] / a["draw_s"]                  # the title draws in exactly title_draw_s
    p["T"] = [np.ascontiguousarray(T * np.float32(k)) for T in p["T"]]
    p["draw_s"] = p["tmax"] = float(mod["title_draw_s"])
    p["speed"] = a["draw_s"] / mod["title_draw_s"]
    say(f"title: {a['paths']} paths {a['stroke_len_px']:.0f} px + {a['fills']} fills = {a['draw_s']:.2f} s at "
        f"drawing_speed_px_s, drawn in title_draw_s {mod['title_draw_s']} s (x{k:.3f}) ({time.time() - t:.1f} s)")
    return p


def glue_groups(inks: list[np.ndarray], glue: dict) -> tuple[list[list[int]], list[dict]]:
    """Segments of one scene drawn on or inside each other in the scene image (step 3, layout.glue): i and j are
    glued when i's ink dilated by touch_px covers at least min_contact_px of j's ink, or when at least inside_share
    of the smaller ink bbox lies inside the other's. Returns the glued groups of 2+ (connected components, members in
    story order, groups ordered by their first member) and the gluing pairs with their numbers."""
    import cv2
    n = len(inks)
    bbs = []
    for m in inks:
        rows, cols = np.nonzero(m.any(1))[0], np.nonzero(m.any(0))[0]
        bbs.append((int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1))
    r = math.ceil(glue["touch_px"])
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    root = list(range(n))

    def find(i: int) -> int:
        while root[i] != i:
            i = root[i]
        return i

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = bbs[i], bbs[j]
            iw, ih = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
            inside = max(iw, 0) * max(ih, 0) / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
            contact = 0
            if iw > -2 * r and ih > -2 * r:              # only then can i's dilated ink reach j's ink
                x0, y0 = max(max(a[0], b[0]) - 2 * r, 0), max(max(a[1], b[1]) - 2 * r, 0)
                win = (slice(y0, min(a[3], b[3]) + 2 * r), slice(x0, min(a[2], b[2]) + 2 * r))
                contact = int(((cv2.dilate(inks[i][win].astype(np.uint8), kern) > 0) & inks[j][win]).sum())
            if contact >= glue["min_contact_px"] or inside >= glue["inside_share"]:
                pairs.append({"pair": [i, j], "contact_px": contact, "inside": round(inside, 3)})
                root[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0]), pairs


def load_pieces(run: Path, mod: dict, al: dict) -> list[dict]:
    """The title, then every segment in story order, each with its alignment row and its compound (the ids of the
    segments it is glued to, itself included, story order; None when it is glued to none)."""
    pieces = [load_title(run, mod)]
    pieces[0]["compound"] = None
    for n in sorted(s["id"] for s in al["scenes"]):
        sc, scene, anal = rv.prepare(run, n, mod, al)
        _, segs = rv.scene_speech(al, n)
        groups = scene["groups"]
        if [g["id"] for g in groups[:-1]] != [s["id"] for s in segs] or groups[-1]["id"] != "residual":
            raise Fail(f"scene {n}: groups {[g['id'] for g in groups]} do not match its segments")
        t = time.time()
        k = len(segs)
        labels = np.full(scene["ink"].shape, -1, np.int8)
        labels[scene["arrow"]] = k
        for i, g in enumerate(groups[:-1]):
            labels[g["ink"]] = i
        owner, keep = ownership(scene["rgb"], labels)
        for seg, g in zip(segs, groups):
            if not g["ink"].any():
                raise Fail(f"segment {seg['id']} has no ink")
        compounds, pairs = glue_groups([g["ink"] for g in groups[:-1]], mod["layout"]["glue"])
        member = {i: tuple(segs[j]["id"] for j in grp) for grp in compounds for i in grp}
        for i, (seg, g, a) in enumerate(zip(segs, groups, anal)):
            if not isinstance(seg.get("speech_end_s"), (int, float)) or not seg["start_s"] <= seg["speech_end_s"]:
                raise Fail(f"alignment.json: segment {seg['id']} lacks a valid speech_end_s")
            p = make_piece(seg["id"], n, run / "scenes" / f"scene_{n}_4k.png", scene["rgb"], keep & (owner == i),
                           g["ink"], a)
            p["speech_src"] = (float(seg["start_s"]), float(seg["speech_end_s"]))
            p["compound"] = member.get(i)
            pieces.append(p)
        glued = "; glued " + ", ".join("+".join(segs[j]["id"] for j in grp) for grp in compounds) + " (" + ", ".join(
            f"{segs[q['pair'][0]]['id']}~{segs[q['pair'][1]]['id']} contact {q['contact_px']} px inside {q['inside']}"
            for q in pairs) + ")" if compounds else ""
        say(f"scene {n}: {k} pieces cut in {time.time() - t:.1f} s ({int(scene['arrow'].sum())} arrow px dropped){glued}")
        del scene, anal, owner, keep, labels
    return pieces


# ------------------------------------------------------------------ schedule

def speed_up(p: dict, k: float) -> None:
    """Draw piece p k times faster than its natural (drawing_speed_px_s) time: reveal maps and track scaled."""
    p["speed"] = k
    if k == 1.0:
        return
    s = np.float32(1 / k)
    p["T"] = [np.ascontiguousarray(T * s) for T in p["T"]]
    p["track"] = (p["track"][0] * s, *p["track"][1:])
    p["draw_s"] = p["tmax"] = p["draw_s"] / k


def arrow_lead(p: dict, cam: dict) -> float:
    """How long after the previous draw end + dwell_s a shot's first piece p may start at the earliest:
    (move_s + arrow_draw_s)/2 when a flow arrow leads into it (the arrow draws across mid-glide), else 0."""
    return (cam["move_s"] + cam["arrow_draw_s"]) / 2 if p.get("arrow_in") else 0.0


def pen_gap(p: dict, cam: dict) -> float:
    """Least pen time between the previous piece's draw end and piece p's start: into a new shot dwell_s +
    arrow_lead (the camera rests, then glides); inside a shot arrow_draw_s when a flow arrow leads into p (the pen
    draws it in the held frame right before p), else 0 (the camera stays put, so it needs no rest)."""
    if p["shot_first"]:
        return cam["dwell_s"] + arrow_lead(p, cam)
    return cam["arrow_draw_s"] if p.get("arrow_in") else 0.0


def move_start(pen: float, start: float, p: dict, cam: dict) -> float:
    """Start of the glide into shot-first piece p: max(previous draw end + dwell_s, p's start - move_s/2),
    arrow_draw_s/2 earlier when a flow arrow leads into p, so the arrow (centred on mid-glide) ends by p's start."""
    return max(pen, start - cam["move_s"] / 2 - (cam["arrow_draw_s"] / 2 if p.get("arrow_in") else 0.0))


def schedule(pieces: list[dict], mod: dict, cam: dict, al: dict) -> dict:
    """Global pen: draw windows, speed factors, speech windows, inserted pauses, ending (final-video seconds). Needs
    the layout and frame_prep (the shots).
      - A piece starts at max(its speech start, previous draw end + pen_gap) (+ lead_in_s for the first segment):
        the first piece of a shot after dwell_s (+ arrow_lead), the others of a shot right after the previous one
        (+ arrow_draw_s for a flow arrow, drawn just before the piece). The glide into a shot starts at
        move_start().
      - When a shot's first piece would start in the first half of the glide into it, the pen waits (frame by
        frame, at most to mid-glide) until the glide window holds the ink it draws in its first FIRST_S (at the
        fastest allowed speed), so the stroke being drawn is always in frame and the camera never jumps.
      - A segment that would end later than the next sentence's start - the next piece's pen_gap draws faster,
        just enough to end then, at most max_speedup times its natural speed; beyond that the next piece starts
        late (and the pause rule may insert silence)."""
    move = cam["move_s"]
    title = pieces[0]
    title["start"], title["end"] = 0.0, title["draw_s"]
    title["speech"] = None
    lead = title["draw_s"] + mod["title_gap_s"]
    dwell = cam["dwell_s"]
    shift = 0.0
    pauses = []
    for j, p in enumerate(pieces[1:]):
        src0, src1 = p["speech_src"]
        speech = lead + src0 + shift
        prev = pieces[j]
        start = max(speech, prev["end"] + pen_gap(p, cam)) + (mod["lead_in_s"] if j == 0 else 0.0)
        if p["shot_first"]:
            a = move_start(prev["end"] + dwell, start, p, cam)     # the glide into p's shot starts here
            if start < a + move / 2:
                first = revealed(p, float(p["track"][0][0]) + FIRST_S * cam["max_speedup"])
                while start < a + move / 2 and not holds(glide(prev, p, a, a + move, start), first):
                    start = min(start + 1 / FPS, a + move / 2)
        elif p.get("arrow_in"):
            p["arrow_in"]["start"] = start - p["arrow_in"]["draw_s"]   # drawn in the held frame right before p
        late = start - speech
        if late > mod["segment_overlap_s"] + 1e-6:
            ln = r3(late - mod["segment_overlap_s"])
            pauses.append({"before_segment": p["id"], "at_s": r3(speech), "length_s": ln, "_src_s": src0})
            shift += ln
            speech += ln
        nxt = pieces[j + 2] if j + 2 < len(pieces) else None
        if nxt is not None:
            room = lead + nxt["speech_src"][0] + shift - pen_gap(nxt, cam) - start
            k = 1.0 if p["draw_s"] <= room else min(cam["max_speedup"], p["draw_s"] / max(room, 1e-9))
            speed_up(p, k)
        else:
            speed_up(p, 1.0)
        p["start"], p["end"] = start, start + p["draw_s"]
        p["speech"] = (speech, speech + (src1 - src0))
        p["late"] = max(start - speech, 0.0)
    last = pieces[-1]
    audio_end = lead + shift + float(al["duration_s"])
    zoom_start = max(last["end"], last["speech"][1]) + max(cam["end_hold_s"], dwell)
    zoom_end = zoom_start + cam["end_zoom_s"]
    end = zoom_end + cam["end_final_hold_s"]
    if audio_end > end + 1e-6:
        raise Fail(f"narration runs to {audio_end:.3f} s, after the video end {end:.3f} s")
    frames = math.ceil(round(end * FPS, 6))
    return {"lead": lead, "pauses": pauses, "zoom_start": zoom_start, "zoom_end": zoom_end, "end": end,
            "frames": frames, "audio_end": audio_end}


# ------------------------------------------------------------------ layout

def local_matrix(p: dict) -> np.ndarray:
    """3x3: source px -> piece-local board px (scaled, tilted about its pivot: the ink bbox centre, or for a member
    of a compound the compound's, so every member gets the same matrix and keeps its place in the scene image)."""
    th = math.radians(p["tilt_deg"])
    c, si = math.cos(th) * p["scale"], math.sin(th) * p["scale"]
    cx, cy = p["pivot"]
    return np.array([[c, -si, -(c * cx - si * cy)], [si, c, -(si * cx + c * cy)], [0, 0, 1]])


def raster(p: dict, gap: float) -> None:
    """Layout mask at 1/DS: the piece's ink, scaled + tilted, dilated by gap/2; origin r0 in local px."""
    import cv2
    L = local_matrix(p)
    pts = p["hull"] @ L[:2, :2].T + L[:2, 2]
    r = gap / 2 / DS
    pad = math.ceil(r) + 2
    rx0 = math.floor(pts[:, 0].min() / DS) * DS - pad * DS
    ry0 = math.floor(pts[:, 1].min() / DS) * DS - pad * DS
    rw = math.ceil((pts[:, 0].max() - rx0) / DS) + pad + 1
    rh = math.ceil((pts[:, 1].max() - ry0) / DS) + pad + 1
    ox, oy = p["box"][0], p["box"][1]
    C = np.array([[DS, 0, ox + (DS - 1) / 2], [0, DS, oy + (DS - 1) / 2], [0, 0, 1]])
    Rr = np.array([[1 / DS, 0, -rx0 / DS], [0, 1 / DS, -ry0 / DS], [0, 0, 1]])
    m = cv2.warpAffine(p["ink8"].astype(np.uint8) * 255, (Rr @ L @ C)[:2], (rw, rh), flags=cv2.INTER_LINEAR) > 0
    if r > 0:
        k = 2 * math.ceil(r) + 1
        yy, xx = np.mgrid[:k, :k] - k // 2
        m = cv2.dilate(m.astype(np.uint8), ((xx * xx + yy * yy) <= r * r).astype(np.uint8)) > 0
    p["mask"], p["r0"] = m, np.array([rx0, ry0], np.int64)


def correlate(src: np.ndarray, k: np.ndarray) -> np.ndarray:
    """out[y, x] = sum(src[y:y+kh, x:x+kw] * k) for every top-left (x, y) that keeps the kernel inside src."""
    import cv2
    kh, kw = k.shape
    out = cv2.filter2D(src.astype(np.float32), -1, k.astype(np.float32), anchor=(0, 0),
                       borderType=cv2.BORDER_CONSTANT)
    return out[:src.shape[0] - kh + 1, :src.shape[1] - kw + 1]


def pack(pieces: list[dict], frame_w: int, rows: list[list[int]]) -> list[tuple[int, int]] | None:
    """Serpentine packing in story order of the packing units (each a dict with its layout `mask`: a piece, or a
    compound's joint mask) inside a 16:9 frame frame_w cells wide with one band per row (row r runs
    left to right when r is even). Each piece takes the free position (its dilated mask overlapping no placed one)
    within CONTACT cells of its predecessor, centre inside its row's band and ahead of the predecessor, that is
    closest to the predecessor along the row (cost d*cx + BAND_PULL*|cy - band centre|): small pieces stack, large
    ones march on. A row's first piece goes as far toward the row's starting edge as it can while touching the
    turn. Constraints relax (band, then frame) only when nothing fits. Then a gravity compaction. Returns each
    piece's mask top-left (grid cells, frame origin); None if a piece is boxed in."""
    import cv2
    masks = [p["mask"].astype(np.uint8) for p in pieces]
    pad = max(max(m.shape) for m in masks) + CONTACT + 2
    gw = frame_w + 2 * pad
    gh = sum(m.shape[0] for m in masks) + 2 * pad
    occ = np.zeros((gh, gw), np.uint8)
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * CONTACT + 1, 2 * CONTACT + 1))

    def stamp(k: int, x: int, y: int) -> None:
        h, w = masks[k].shape
        occ[y:y + h, x:x + w] |= masks[k]

    tl = [(pad, pad)]
    stamp(0, pad, pad)
    row_of = {k: r for r, row in enumerate(rows) for k in row}
    first = {row[0] for row in rows}
    band = frame_w / ASPECT / len(rows)
    for k in range(1, len(pieces)):
        m = masks[k]
        h, w = m.shape
        px, py = tl[k - 1]
        ph, pw = masks[k - 1].shape
        xl, xh = max(0, px - w - CONTACT), min(gw - w, px + pw + CONTACT)
        yl, yh = max(0, py - h - CONTACT), min(gh - h, py + ph + CONTACT)
        sub = occ[yl:yh + h, xl:xh + w]
        hit = correlate(sub, m) > 0.5
        near = np.zeros_like(sub)
        pd = cv2.dilate(np.pad(masks[k - 1], CONTACT), kern)
        near[:] = cut(pd, (xl - (px - CONTACT), yl - (py - CONTACT), near.shape[1], near.shape[0]), 0)
        ok = ~hit & (correlate(near, m) > 0.5)
        ys, xs = np.nonzero(ok)
        gx, gy = xs + xl, ys + yl
        inside = (gx >= pad) & (gx + w <= pad + frame_w) & (gy >= pad)
        cx, cy = gx + w / 2, gy - pad + h / 2
        pcx = px + pw / 2
        r = row_of[k]
        d = 1 if r % 2 == 0 else -1
        in_band = (cy >= r * band) & (cy < (r + 1) * band)
        pick = inside & in_band & (True if k in first else d * (cx - pcx) > 0)
        if not pick.any():
            pick = inside & in_band
        if not pick.any():
            pick = inside
        if not pick.any():
            pick = gy >= pad                                   # the piece does not fit the frame: let it stick out
        if not pick.any():
            return None                                        # the predecessor is boxed in
        cost = d * cx + BAND_PULL * np.abs(cy - (r + 0.5) * band)
        idx = np.nonzero(pick)[0]
        j = idx[np.lexsort((gx[idx], gy[idx], cost[idx]))[0]]
        tl.append((int(gx[j]), int(gy[j])))
        stamp(k, int(gx[j]), int(gy[j]))
    compact(masks, tl, occ)
    return [(x - pad, y - pad) for x, y in tl]


def compact(masks: list[np.ndarray], tl: list[tuple[int, int]], occ: np.ndarray) -> None:
    """Gravity pass: every piece (nearest the middle first) slides toward the middle of all pieces, along the
    axis it is further off first, until its dilated mask would touch another's; repeated COMPACT_PASSES times."""
    cnt = occ.astype(np.int16)

    def put(k: int, sgn: int) -> None:
        x, y = tl[k]
        h, w = masks[k].shape
        cnt[y:y + h, x:x + w] += sgn * masks[k].astype(np.int16)

    def free(k: int, x: int, y: int) -> bool:
        h, w = masks[k].shape
        if x < 0 or y < 0 or y + h > cnt.shape[0] or x + w > cnt.shape[1]:
            return False
        return not (cnt[y:y + h, x:x + w][masks[k] > 0] > 0).any()

    for _ in range(COMPACT_PASSES):
        cs = np.array([(x + m.shape[1] / 2, y + m.shape[0] / 2) for (x, y), m in zip(tl, masks)])
        lo = np.array([min(x for x, _ in tl), min(y for _, y in tl)])
        hi = np.array([max(x + m.shape[1] for (x, _), m in zip(tl, masks)),
                       max(y + m.shape[0] for (_, y), m in zip(tl, masks))])
        mid = (lo + hi) / 2
        for k in np.argsort(((cs - mid) ** 2).sum(1)):
            put(k, -1)
            off = mid - cs[k]
            for axis in sorted((0, 1), key=lambda a: -abs(off[a])):
                steps, sgn = int(abs(off[axis])), (1 if off[axis] > 0 else -1)
                x, y = tl[k]
                d, bad = 0, None
                while d < steps:                               # coarse scan, then bisect the first contact
                    nd = min(d + 4, steps)
                    if free(k, x + sgn * nd * (axis == 0), y + sgn * nd * (axis == 1)):
                        d = nd
                    else:
                        bad = nd
                        break
                if bad is not None:
                    while bad - d > 1:
                        m2 = (d + bad) // 2
                        if free(k, x + sgn * m2 * (axis == 0), y + sgn * m2 * (axis == 1)):
                            d = m2
                        else:
                            bad = m2
                tl[k] = (x + sgn * d * (axis == 0), y + sgn * d * (axis == 1))
            put(k, 1)


def board_frame(pieces: list[dict], pos: dict, margin: float) -> tuple[int, int, float, float]:
    """16:9 board (W, H) around every piece's ink + margin, and the origin shift (ox, oy)."""
    allp = []
    for k, p in enumerate(pieces):
        L = local_matrix(p)
        allp.append(p["hull"] @ L[:2, :2].T + L[:2, 2] + pos[k])
    allp = np.concatenate(allp)
    bx0, by0 = allp.min(0) - margin
    bx1, by1 = allp.max(0) + margin
    W = int(math.ceil(max(bx1 - bx0, (by1 - by0) * ASPECT) / 16) * 16)
    H = W * 9 // 16
    return W, H, math.floor((bx0 + bx1) / 2 - W / 2), math.floor((by0 + by1) / 2 - H / 2)


def balanced_rows(weights: list[float], n_rows: int) -> list[list[int]]:
    """Split 0..n-1 into n_rows contiguous non-empty rows minimising the heaviest row (linear partition)."""
    n = len(weights)
    pre = np.concatenate([[0.0], np.cumsum(weights)])
    best = np.full((n_rows + 1, n + 1), np.inf)
    arg = np.zeros((n_rows + 1, n + 1), int)
    best[0, 0] = 0.0
    for r in range(1, n_rows + 1):
        for i in range(r, n + 1):
            for j in range(r - 1, i):
                v = max(best[r - 1, j], pre[i] - pre[j])
                if v < best[r, i]:
                    best[r, i], arg[r, i] = v, j
    rows, i = [], n
    for r in range(n_rows, 0, -1):
        j = int(arg[r, i])
        rows.append(list(range(j, i)))
        i = j
    return rows[::-1]


def units_of(pieces: list[dict]) -> list[list[int]]:
    """Packing units in story order: each compound (its members' indices) at its first member's place, every other
    piece alone."""
    units, at = [], {}
    for k, p in enumerate(pieces):
        key = p.get("compound") or (p["id"],)
        if key in at:
            units[at[key]].append(k)
        else:
            at[key] = len(units)
            units.append([k])
    return units


def unit_mask(pieces: list[dict], members: list[int]) -> dict:
    """One packing mask for a unit: its members' layout masks (same local matrix) OR-ed on one grid; origin r0."""
    if len(members) == 1:
        return {"mask": pieces[members[0]]["mask"], "r0": pieces[members[0]]["r0"]}
    r0 = np.min([pieces[k]["r0"] for k in members], axis=0)
    offs = [(pieces[k]["r0"] - r0) // DS for k in members]        # r0 values are multiples of DS
    h = max(int(o[1]) + pieces[k]["mask"].shape[0] for o, k in zip(offs, members))
    w = max(int(o[0]) + pieces[k]["mask"].shape[1] for o, k in zip(offs, members))
    m = np.zeros((h, w), bool)
    for o, k in zip(offs, members):
        mk = pieces[k]["mask"]
        m[int(o[1]):int(o[1]) + mk.shape[0], int(o[0]):int(o[0]) + mk.shape[1]] |= mk
    return {"mask": m, "r0": r0}


def layout(pieces: list[dict], lay: dict, seed: int) -> dict:
    """Place every piece on the board: scale, tilt, board affine A3, ink bbox. A compound (segments glued in their
    scene image, load_pieces) is one rigid unit: its first member's scale and tilt for all, one pivot, one packing
    mask, one position, so its members keep their places from the scene image. Returns board size + rows."""
    rng = np.random.default_rng(seed)
    for p in pieces:
        s = float(rng.uniform(lay["scale"][0], lay["scale"][1]))
        p["scale"] = 1.0 if p["id"] == TITLE_ID else round(s, 4)
        p["tilt_deg"] = round(float(rng.uniform(-lay["tilt_deg"], lay["tilt_deg"])), 4)
        p["pivot"] = p["centre"]
    units = units_of(pieces)
    for u in units:
        if len(u) > 1:
            hull = np.concatenate([pieces[k]["hull"] for k in u])
            pivot = ((hull[:, 0].min() + hull[:, 0].max()) / 2, (hull[:, 1].min() + hull[:, 1].max()) / 2)
            for k in u:
                pieces[k].update(scale=pieces[u[0]]["scale"], tilt_deg=pieces[u[0]]["tilt_deg"], pivot=pivot)
    for p in pieces:
        raster(p, lay["gap_px"])
    umasks = [unit_mask(pieces, u) for u in units]
    area = sum(m["mask"].shape[0] * m["mask"].shape[1] for m in umasks)
    widest = max(m["mask"].shape[1] for m in umasks)
    w0 = math.sqrt(area * ASPECT)
    best = None
    for f in FRAME_FACTORS:
        fw = max(widest, int(round(w0 * f)))
        for n_rows in ROW_COUNTS:
            rows = balanced_rows([m["mask"].shape[0] * m["mask"].shape[1] for m in umasks], n_rows)
            tl = pack(umasks, fw, rows)
            if tl is None:
                continue
            pos = {k: np.array([x * DS, y * DS]) - m["r0"] for u, m, (x, y) in zip(units, umasks, tl) for k in u}
            W, H, ox, oy = board_frame(pieces, pos, lay["margin_px"])
            if best is None or W * H < best[0]:
                best = (W * H, rows, pos, W, H, ox, oy, f, n_rows)
    if best is None:
        raise Fail(f"layout: every packing frame boxed a piece in (frames {FRAME_FACTORS}, rows {ROW_COUNTS})")
    _, rows, pos, W, H, ox, oy, f, n_rows = best
    rows = [[k for ui in row for k in units[ui]] for row in rows]
    ink = sum(int(m["mask"].sum()) for m in umasks) * DS * DS
    say(f"layout: frame {f:.2f} x sqrt(area*16/9), {n_rows} bands -> board {W}x{H}, rows {[len(r) for r in rows]}; "
        f"piece boxes {area * DS * DS / (W * H):.0%}, dilated ink {ink / (W * H):.0%} of the board; "
        f"{sum(len(u) > 1 for u in units)} compounds packed rigid")
    for k, p in enumerate(pieces):
        A = local_matrix(p)
        A[:2, 2] += pos[k] - np.array([ox, oy])
        p["A3"] = A
        pts = p["hull"] @ A[:2, :2].T + A[:2, 2]
        p["ink_bbox"] = [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]
        if p["ink_bbox"][0] < 0 or p["ink_bbox"][1] < 0 or p["ink_bbox"][2] > W or p["ink_bbox"][3] > H:
            raise Fail(f"layout: piece {p['id']} ink {p['ink_bbox']} leaves the {W}x{H} board")
    return {"width": W, "height": H, "rows": [[pieces[k]["id"] for k in row] for row in rows],
            "min_w": MIN_WINDOW_W * max(p["scale"] for p in pieces), "frame_factor": f, "bands": n_rows}


# ------------------------------------------------------------------ flow arrows

def occupancy(pieces: list[dict], board: dict) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Board grid at 1/DS: the index of the piece whose dilated ink mask covers each cell (-1 = free), and each
    piece's mask top-left cell. Needs layout() (masks, r0, A3)."""
    gh, gw = math.ceil(board["height"] / DS), math.ceil(board["width"] / DS)
    lab = np.full((gh, gw), -1, np.int16)
    tls = []
    for k, p in enumerate(pieces):
        off = p["A3"][:2, 2] - local_matrix(p)[:2, 2] + p["r0"]       # board px of the mask's top-left
        x, y = int(round(off[0] / DS)), int(round(off[1] / DS))
        m = p["mask"]
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + m.shape[1], gw), min(y + m.shape[0], gh)
        lab[y0:y1, x0:x1][m[y0 - y:y1 - y, x0 - x:x1 - x]] = k
        tls.append((x, y))
    return lab, tls


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.hypot(v[0], v[1]))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def arrow_shape(P: np.ndarray, M: np.ndarray, Q: np.ndarray, st: dict, width: float) -> dict:
    """Shaft (quadratic Bezier P -> through M -> Q with a tapered seeded wobble) and head (open V or filled
    triangle at Q) in board px; `st` holds the arrow's seeded style draws."""
    C = 2 * M - (P + Q) / 2
    chord = float(np.hypot(*(Q - P)))
    n = max(24, int(chord / 6))
    s = np.linspace(0, 1, n)[:, None]
    pts = (1 - s) ** 2 * P + 2 * s * (1 - s) * C + s ** 2 * Q
    seg = np.hypot(*np.diff(pts, axis=0).T)
    L = float(seg.sum())
    nrm = np.array([-(Q - P)[1], (Q - P)[0]]) / max(chord, 1e-9)
    amp = min(6.0, st["wobble"] * L)
    pts = pts + nrm * (amp * np.sin(2 * np.pi * (st["freq"] * s + st["phase"])) * np.sin(np.pi * s))
    head_len = float(np.clip(0.2 * L, 3.5 * width, 9 * width))
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
    back = pts[int(np.searchsorted(cum, max(cum[-1] - head_len / 2, 0.0)))]
    d = unit(pts[-1] - back)
    th = math.radians(st["half_angle"])
    rot = lambda a: np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])   # noqa: E731
    tip = pts[-1]
    w1, w2 = tip - head_len * (rot(th) @ d), tip - head_len * (rot(-th) @ d)
    if st["head"] == "filled":                     # the shaft stops inside the triangle's base
        keep = cum <= cum[-1] - 0.6 * head_len * math.cos(th)
        pts = pts[keep] if keep.sum() >= 2 else pts[:2]
    return {"path": pts, "head": np.array([tip, w1, w2]), "length": L}


def shape_samples(sh: dict) -> np.ndarray:
    """Points along the shaft and every head edge (about every 4 px) for the clearance test."""
    out = [sh["path"]]
    tip, w1, w2 = sh["head"]
    for a, b in ((tip, w1), (tip, w2), (w1, w2)):
        k = max(2, int(np.hypot(*(b - a)) / 4))
        out.append(a + (b - a) * np.linspace(0, 1, k)[:, None])
    return np.concatenate(out)


def arrow_colours(run: Path) -> tuple:
    """The run's flow-arrow colours (the setup page's palette when setup is required, else the style's palette.json):
    ((name, rgb, share), ...), shares summing to 1."""
    try:
        return palette.arrow_colours(rv.STYLES, rv.run_style(run), run)
    except palette.PaletteError as e:
        raise Fail(f"palette: {e}") from e


def pick_colour(colours: tuple, u: float) -> tuple:
    """The colour whose cumulative share first exceeds u (the last one if rounding leaves the shares short of 1)."""
    return next((c for c, cum in zip(colours, np.cumsum([w for *_, w in colours])) if u < cum), colours[-1])


def arrow_picks(colours: tuple, seed: int, n: int) -> list[list]:
    """[name, [r, g, b]] of the arrow each gap k (piece k -> k+1 of n pieces) would get: the first draw of
    plan_arrows' generator default_rng([seed, k]), so the picks fix every arrow's colour before the board is built."""
    out = []
    for k in range(n - 1):
        name, rgb, _ = pick_colour(colours, np.random.default_rng([seed, k]).random(3)[0])
        out.append([name, list(rgb)])
    return out


def plan_arrows(pieces: list[dict], board: dict, arr: dict, colours: tuple) -> tuple[list[dict], dict]:
    """Flow arrows in the widest blank gaps between story-consecutive pieces (method 3b), and the fill report.
    Deterministic: the same layout, layout.arrows values and arrow colours (arrow_colours) give the same arrows."""
    import cv2
    from scipy import ndimage
    lab, tls = occupancy(pieces, board)
    labp = np.pad(lab, 1, constant_values=-2)                    # the board edge counts as ink
    free = labp == -1
    dist, (iy, ix) = ndimage.distance_transform_edt(free, return_indices=True)
    near = labp[iy, ix][1:-1, 1:-1]                               # the piece whose dilated ink is nearest (-2 edge)
    iy, ix = iy[1:-1, 1:-1] - 1, ix[1:-1, 1:-1] - 1                # that nearest ink cell
    dist = dist[1:-1, 1:-1] * DS                                  # board px to the nearest dilated ink or edge
    free = free[1:-1, 1:-1]
    W, H = board["width"], board["height"]
    fill = {"piece_boxes": round(sum(p["mask"].size for p in pieces) * DS * DS / (W * H), 3),
            "dilated_ink": round(float((~free).mean()), 3), "empty_px": EMPTY_PX,
            "empty": round(float((ndimage.distance_transform_edt(free) * DS > EMPTY_PX).mean()), 3)}
    # Every gap between piece k and k+1: neighbouring free cells, one nearest k's ink and one nearest k+1's, whose
    # nearest ink cells a (of k) and b (of k+1) lie on opposite sides (angle a-M-b >= 120 deg), so the gap is
    # between the two pieces, not beside them. Per pair, the widest such point M wins.
    cand = {}
    for c1, c2 in (((slice(None), slice(None, -1)), (slice(None), slice(1, None))),
                   ((slice(None, -1), slice(None)), (slice(1, None), slice(None)))):
        A, B = near[c1], near[c2]
        sel = free[c1] & free[c2] & (A >= 0) & (B >= 0) & (np.abs(A.astype(np.int32) - B) == 1)
        Y, X = np.nonzero(sel)
        dy, dx = (0, 1) if c1[0] == slice(None) else (1, 0)
        first = A[sel] < B[sel]                                   # c1's nearest ink is piece k
        n1 = np.stack([ix[c1][sel], iy[c1][sel]], 1).astype(np.float64)
        n2 = np.stack([ix[c2][sel], iy[c2][sel]], 1).astype(np.float64)
        a = (np.where(first[:, None], n1, n2) + 0.5) * DS
        b = (np.where(first[:, None], n2, n1) + 0.5) * DS
        Mxy = (np.stack([X + dx / 2, Y + dy / 2], 1) + 0.5) * DS
        va, vb = a - Mxy, b - Mxy
        cos = (va * vb).sum(1) / np.maximum(np.hypot(*va.T) * np.hypot(*vb.T), 1e-9)
        ok = cos <= -0.5
        ks, gaps, Mo, ao, bo = np.minimum(A[sel], B[sel])[ok], np.hypot(*(a - b).T)[ok], Mxy[ok], a[ok], b[ok]
        o = np.lexsort((Mo[:, 0], Mo[:, 1], -gaps, ks))         # per pair: widest first, then top-left
        ks_sorted = ks[o]
        for i in o[np.unique(ks_sorted, return_index=True)[1]]:
            k = int(ks[i])
            key = (float(gaps[i]), -float(Mo[i, 1]), -float(Mo[i, 0]))
            if k not in cand or key > cand[k][0]:
                cand[k] = (key, Mo[i], ao[i], bo[i])
    order = sorted((k for k, c in cand.items() if c[0][0] - DS >= arr["min_gap_px"]
                    and not (pieces[k].get("compound") and pieces[k]["compound"] == pieces[k + 1].get("compound"))),
                   key=lambda k: (-cand[k][0][0], k))   # never an arrow between two members of one compound
    hw, clear = arr["width_px"] / 2, arr["clearance_px"]
    need = clear + hw
    blocked = np.zeros(lab.shape, np.uint8)                       # placed arrows, + clearance, at 1/DS
    out = []
    for k in order:
        if len(out) >= arr["max"]:
            break
        (gap, _, _), M, ea, eb = cand[k]
        gap -= DS
        ends = [e + unit(M - e) * (need + DS) for e in (ea, eb)]   # + one cell: the grid's rounding
        rng = np.random.default_rng([arr["seed"], k])
        u = rng.random(3)
        colour = pick_colour(colours, u[0])
        st = {"head": "filled" if u[1] < 0.4 else "open", "half_angle": round(float(24 + 8 * u[2]), 2),
              "wobble": round(float(rng.uniform(0.004, 0.009)), 5), "freq": round(float(rng.uniform(1.0, 2.0)), 4),
              "phase": round(float(rng.random()), 4)}
        okd = dist if not blocked.any() else np.minimum(
            dist, ndimage.distance_transform_edt(blocked == 0) * DS)
        placed = None
        for f in ARROW_SHRINK:
            P, Q = M + (ends[0] - M) * f, M + (ends[1] - M) * f
            sh = arrow_shape(P, M, Q, st, arr["width_px"])
            if sh["length"] > arr["max_len_px"]:
                g = arr["max_len_px"] / sh["length"]
                P, Q = M + (P - M) * g, M + (Q - M) * g
                sh = arrow_shape(P, M, Q, st, arr["width_px"])
            if sh["length"] < arr["min_len_px"]:
                break
            pts = shape_samples(sh)
            cx, cy = np.floor(pts[:, 0] / DS).astype(int), np.floor(pts[:, 1] / DS).astype(int)
            inside = (cx >= 0) & (cy >= 0) & (cx < lab.shape[1]) & (cy < lab.shape[0])
            if inside.all() and float(okd[cy, cx].min()) >= need:
                placed = sh
                break
        if placed is None:
            continue
        cv2.polylines(blocked, [np.round(shape_samples(placed) / DS).astype(np.int32)], False, 1,
                      thickness=max(1, math.ceil((arr["width_px"] + 2 * clear) / DS)))
        out.append({"from": pieces[k]["id"], "to": pieces[k + 1]["id"], "gap_px": round(gap, 1),
                    "at": [round(float(v), 1) for v in M], "length_px": round(placed["length"], 1),
                    "width_px": arr["width_px"], "colour": colour[0], "rgb": list(colour[1]), "head": st["head"],
                    "path": [[round(float(x), 2), round(float(y), 2)] for x, y in placed["path"]],
                    "head_points": [[round(float(x), 2), round(float(y), 2)] for x, y in placed["head"]]})
    out.sort(key=lambda a: next(i for i, p in enumerate(pieces) if p["id"] == a["to"]))
    inked = (~free).astype(np.uint8)                               # dilated ink + the arrows' own strokes
    for a in out:
        cv2.polylines(inked, [np.round(np.array(a["path"]) / DS).astype(np.int32),
                              np.round(np.array(a["head_points"])[[1, 0, 2]] / DS).astype(np.int32)], False, 1,
                      thickness=max(1, math.ceil(a["width_px"] / DS)))
    fill["empty_with_arrows"] = round(float((ndimage.distance_transform_edt(inked == 0) * DS > EMPTY_PX).mean()), 3)
    fill["arrows"] = len(out)
    say(f"arrows: {len(out)} placed of {len(order)} gaps >= {arr['min_gap_px']} px (max {arr['max']}): "
        + ", ".join(f"{a['from']}->{a['to']} {a['length_px']:.0f} px {a['colour']}" for a in out))
    return out, fill


def arrow_layer(a: dict, draw_s: float) -> dict:
    """A recorded flow arrow as a compositor layer in board px (A3 = identity, scale 1): marker darkness and a
    reveal map drawn along the shaft in (1 - ARROW_HEAD_SHARE) x draw_s, then the head; plus its board-px bbox."""
    import cv2
    path, head = np.array(a["path"], np.float64), np.array(a["head_points"], np.float64)
    w = float(a["width_px"])
    m = w + 4
    allp = np.vstack([path, head])
    x0, y0 = math.floor(allp[:, 0].min() - m), math.floor(allp[:, 1].min() - m)
    Wd = -(-math.ceil(allp[:, 0].max() + m - x0) // ALIGN) * ALIGN
    Hd = -(-math.ceil(allp[:, 1].max() + m - y0) // ALIGN) * ALIGN

    def loc(q: np.ndarray) -> np.ndarray:
        return np.round((q - (x0, y0)) * 16).astype(np.int32)            # cv2 shift=4 sub-pixel coordinates

    th = max(1, int(round(w)))
    cov = np.zeros((Hd, Wd), np.uint8)
    cv2.polylines(cov, [loc(path)], False, 255, thickness=th, lineType=cv2.LINE_AA, shift=4)
    tip, w1, w2 = head
    if a["head"] == "filled":
        cv2.fillPoly(cov, [loc(head)], 255, lineType=cv2.LINE_AA, shift=4)
        cv2.polylines(cov, [loc(head)], True, 255, thickness=max(1, th // 2), lineType=cv2.LINE_AA, shift=4)
    else:
        cv2.polylines(cov, [loc(np.array([w1, tip, w2]))], False, 255, thickness=th, lineType=cv2.LINE_AA, shift=4)
    t_shaft = (1 - ARROW_HEAD_SHARE) * draw_s
    T = np.full((Hd, Wd), np.float32(draw_s), np.float32)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(path, axis=0).T))])
    ts = t_shaft * cum / max(cum[-1], 1e-9)
    if a["head"] == "filled":
        mask = np.zeros((Hd, Wd), np.uint8)
        cv2.fillPoly(mask, [loc(head)], 1, shift=4)
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        base = (w1 + w2) / 2
        ax = tip - base
        yy, xx = np.nonzero(mask)
        u = (((xx + x0) - base[0]) * ax[0] + ((yy + y0) - base[1]) * ax[1]) / max(float(ax @ ax), 1e-9)
        T[yy, xx] = t_shaft + np.clip(u, 0, 1).astype(np.float32) * (draw_s - t_shaft)
    else:
        half = (draw_s - t_shaft) / 2
        for wing, t0 in ((w2, t_shaft + half), (w1, t_shaft)):            # later wing first: earlier times win
            q = tip + (wing - tip) * np.linspace(0, 1, 9)[:, None]
            tq = t0 + half * np.linspace(0, 1, 9)
            for i in range(len(q) - 2, -1, -1):
                cv2.line(T, tuple(loc(q[i])), tuple(loc(q[i + 1])), float(tq[i]), thickness=th + 4, shift=4)
    for i in range(len(path) - 2, -1, -1):                              # the shaft last: its times are earliest
        cv2.line(T, tuple(loc(path[i])), tuple(loc(path[i + 1])), float(ts[i]), thickness=th + 4, shift=4)
    dark = np.round(cov.astype(np.float32)[..., None] / 255 * (255 - np.array(a["rgb"], np.float32))).astype(np.uint8)
    pyr, tpyr = [dark], [T]
    for _ in range(LEVELS - 1):
        p, q = pyr[-1], tpyr[-1]
        pyr.append(cv2.resize(p, (p.shape[1] // 2, p.shape[0] // 2), interpolation=cv2.INTER_AREA))
        tpyr.append(np.ascontiguousarray(q.reshape(q.shape[0] // 2, 2, q.shape[1] // 2, 2).min(axis=(1, 3))))
    hw = w / 2
    allp = np.vstack([path, head])
    bbox = [float(allp[:, 0].min() - hw), float(allp[:, 1].min() - hw), float(allp[:, 0].max() + hw),
            float(allp[:, 1].max() + hw)]
    return {"id": f"arrow {a['from']}>{a['to']}", "from": a["from"], "to": a["to"], "scene": None,
            "box": (x0, y0, Wd, Hd), "dark": pyr, "T": tpyr, "tmin": 0.0, "tmax": draw_s, "draw_s": draw_s,
            "scale": 1.0, "A3": np.eye(3), "bbox": bbox, "rec": a}


# ------------------------------------------------------------------ layout file

def layout_inputs(run: Path, al: dict) -> list[Path]:
    """Every file the board layout depends on: the title card, each scene image, its segments file and masks."""
    out = [run / "title" / "title_4k.png"]
    for n in sorted(s["id"] for s in al["scenes"]):
        seg = run / "scenes" / f"scene_{n}_segments.json"
        doc = rv.read_json(seg)
        out += [run / "scenes" / f"scene_{n}_4k.png", seg]
        out += [run / "scenes" / str(e.get("mask")) for e in [*doc.get("segments", []), doc.get("residual") or {},
                                                                doc.get("arrows_mask") or {}]]
    return out


def file_sha(p: Path) -> str:
    if not p.is_file():
        raise Fail(f"board input missing: {p}")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def layout_fingerprints(run: Path, mod: dict, al: dict | None = None) -> tuple[str, str, dict, list]:
    """(fingerprint, legacy fingerprint, {file: sha}, arrow picks). The fingerprint is the sha256 of what the board
    is made from: every layout_inputs file (by content), the pieces in story order, module.json `layout` (arrows
    included) + `ink_threshold`, and the colour the style's palette.json gives the arrow of every gap (arrow_picks).
    The legacy fingerprint leaves the picks out: layouts built before palette.json record it (see layout_current)."""
    al = rv.load_alignment(run) if al is None else al
    files = {rel(run, p): file_sha(p) for p in layout_inputs(run, al)}
    order = [TITLE_ID] + [s["id"] for n in sorted(s["id"] for s in al["scenes"]) for s in rv.scene_speech(al, n)[1]]
    used = {"layout": mod.get("layout"), "ink_threshold": mod.get("ink_threshold")}
    seed = ((mod.get("layout") or {}).get("arrows") or {}).get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise Fail(f"module: layout.arrows.seed must be an integer >= 0, got {seed!r}")
    picks = arrow_picks(arrow_colours(run), seed, len(order))
    legacy = sha({"files": files, "order": order, "module": used})
    return sha({"files": files, "order": order, "module": used, "arrow_colours": picks}), legacy, files, picks


def layout_fingerprint(run: Path, mod: dict, al: dict | None = None) -> tuple[str, dict]:
    """(fingerprint, {file: sha}) of the current inputs (layout_fingerprints)."""
    fp, _, files, _ = layout_fingerprints(run, mod, al)
    return fp, files


def layout_current(run: Path, mod: dict, doc: dict, al: dict | None = None) -> tuple[str | None, str]:
    """(the fingerprint the saved layout `doc` holds, when it is current, else None; why it is stale). Current = its
    fingerprint is the inputs' one, or (a layout built before palette.json) its legacy fingerprint is, and every
    arrow it holds has the colour the palette now picks for its gap, so a rebuild would draw the same board; such a
    layout keeps its legacy fingerprint, and with it its Gate 2 approval and video."""
    fp, legacy, files, picks = layout_fingerprints(run, mod, al)
    have = doc.get("fingerprint")
    if have == fp:
        return fp, ""
    if have == legacy:
        ids = [p.get("id") for p in doc.get("pieces") or []]
        off = sorted({str(a.get("colour")) for a in doc.get("arrows") or []
                      if a.get("from") not in ids[:-1] or [a.get("colour"), a.get("rgb")] != picks[ids.index(a["from"])]})
        if not off:
            return legacy, ""
        return None, f"arrow colour(s) {', '.join(off)} differ from what the style's palette.json now picks"
    old = doc.get("inputs") or {}
    why = [k for k in sorted(set(files) | set(old)) if files.get(k) != old.get(k)]
    why += [f"module {k}" for k in ("layout", "ink_threshold") if (doc.get("module") or {}).get(k) != mod.get(k)]
    return None, (", ".join(why[:8]) + (" ..." if len(why) > 8 else "")) or "segment order or arrow colours"


def load_layout(run: Path, mod: dict, al: dict) -> dict:
    """videos/board_layout.json, refused when missing, of another version, or stale (layout_current)."""
    path = run / "videos" / LAYOUT_FILE
    if not path.is_file():
        raise Fail(f"{path} missing: run `stitch.py board` and approve the board at Gate 2 first")
    doc = rv.read_json(path)
    if doc.get("version") != LAYOUT_VERSION:
        raise Fail(f"{path} is version {doc.get('version')!r}, expected {LAYOUT_VERSION}: run `stitch.py board`")
    fp, why = layout_current(run, mod, doc, al)
    if fp is None:
        raise Fail(f"{path} is stale (changed: {why}): run `stitch.py board` and approve the new board at Gate 2")
    return doc


def apply_layout(pieces: list[dict], doc: dict) -> dict:
    """Put every piece where the saved layout has it (scale, tilt, affine, ink bbox). Returns the board."""
    got = [q["id"] for q in doc["pieces"]]
    if got != [p["id"] for p in pieces]:
        raise Fail(f"{LAYOUT_FILE} pieces {got[:6]}... do not match the run's pieces; run `stitch.py board`")
    for p, q in zip(pieces, doc["pieces"]):
        p["scale"], p["tilt_deg"] = q["scale"], q["tilt_deg"]
        A = np.eye(3)
        A[:2] = np.array(q["affine"], np.float64)
        p["A3"] = A
        pts = p["hull"] @ A[:2, :2].T + A[:2, 2]
        p["ink_bbox"] = [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]
    b = doc["board"]
    return {"width": b["width"], "height": b["height"], "rows": b["rows"], "min_w": b["min_w"]}


def link_arrows(pieces: list[dict], arrows: list[dict]) -> None:
    """Each arrow leads into its `to` piece, from that piece's predecessor in story order."""
    idx = {p["id"]: i for i, p in enumerate(pieces)}
    for ar in arrows:
        i = idx.get(ar["to"])
        if i is None or i == 0 or pieces[i - 1]["id"] != ar["from"]:
            raise Fail(f"{LAYOUT_FILE}: arrow {ar['from']} -> {ar['to']} does not join two consecutive pieces")
        pieces[i]["arrow_in"] = ar


# ------------------------------------------------------------------ camera

def smooth(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def fit(bbox: list[float], pad: float, board: dict) -> tuple[float, float, float]:
    """16:9 window framing bbox with pad on every side, >= the board's min_w, kept inside the board where it fits."""
    x0, y0, x1, y1 = bbox
    w = max(max(x1 - x0, (y1 - y0) * ASPECT) * (1 + 2 * pad), board["min_w"])
    h = w / ASPECT
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    W, H = board["width"], board["height"]
    if w <= W:
        cx = min(max(cx, w / 2), W - w / 2)
    if h <= H:
        cy = min(max(cy, h / 2), H - h / 2)
    return (cx, cy, w)


def union(a: list[float], b: list[float]) -> list[float]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def contain(win: tuple, bbox: list[float], min_w: float) -> tuple[float, float, float]:
    """Widen win about its centre until bbox (+ EDGE_PX) is inside (and it is at least min_w wide)."""
    cx, cy, w = win
    x0, y0, x1, y1 = bbox[0] - EDGE_PX, bbox[1] - EDGE_PX, bbox[2] + EDGE_PX, bbox[3] + EDGE_PX
    need = max(2 * max(cx - x0, x1 - cx), 2 * max(cy - y0, y1 - cy) * ASPECT, min_w)
    return (cx, cy, max(w, need))


def through(a: tuple, u: tuple, b: tuple, tau: float) -> tuple[float, float, float]:
    """Quadratic Bezier in (cx, cy, log w) from a (tau 0) through u (tau 0.5) to b (tau 1)."""
    A = np.array([a[0], a[1], math.log(a[2])])
    U = np.array([u[0], u[1], math.log(u[2])])
    B = np.array([b[0], b[1], math.log(b[2])])
    C = 2 * U - (A + B) / 2
    P = (1 - tau) ** 2 * A + 2 * tau * (1 - tau) * C + tau ** 2 * B
    return (float(P[0]), float(P[1]), math.exp(P[2]))


def reveal_track(p: dict) -> tuple:
    """Board-px bbox of the ink revealed so far, as cumulative arrays over the piece's sorted reveal times (from
    pyramid level TRACK_LEVEL)."""
    f = 1 << TRACK_LEVEL
    T, d = p["T"][TRACK_LEVEL], p["dark"][TRACK_LEVEL]
    ys, xs = np.nonzero(d.max(axis=2) > TRACK_DARK)
    if ys.size == 0:
        raise Fail(f"piece {p['id']}: no visible ink at pyramid level {TRACK_LEVEL} for the camera to track")
    src = np.stack([p["box"][0] + xs * f + (f - 1) / 2, p["box"][1] + ys * f + (f - 1) / 2], axis=1)
    pts = src @ p["A3"][:2, :2].T + p["A3"][:2, 2]
    t = T[ys, xs]
    o = np.argsort(t, kind="stable")
    t, pts = t[o], pts[o]
    return (t, np.minimum.accumulate(pts[:, 0]), np.minimum.accumulate(pts[:, 1]),
            np.maximum.accumulate(pts[:, 0]), np.maximum.accumulate(pts[:, 1]))


def revealed(p: dict, tau: float) -> list[float] | None:
    """Board-px bbox of piece p's ink visible tau s after its draw start (None before its first pixel)."""
    tr = p["track"]
    k = int(np.searchsorted(tr[0], tau, side="left")) - 1   # a pixel shows once tau > T
    return None if k < 0 else [float(tr[1][k]), float(tr[2][k]), float(tr[3][k]), float(tr[4][k])]


def shot_bbox(pieces: list[dict], idx: list[int]) -> list[float]:
    """Board-px bbox of the pieces idx (consecutive) and of every flow arrow between two of them."""
    bb = pieces[idx[0]]["ink_bbox"]
    for i in idx[1:]:
        bb = union(bb, pieces[i]["ink_bbox"])
        if pieces[i].get("arrow_in") is not None:
            bb = union(bb, pieces[i]["arrow_in"]["bbox"])
    return bb


def plan_shots(pieces: list[dict], cam: dict, board: dict) -> list[dict]:
    """Shots: the title alone, then each scene's segments (story order) split into runs of camera.shot_size
    (2 or 3) consecutive segments; a shot never spans a scene change (each scene change stays a glide, so Gate 3's
    scene-change clips and the Scene-NN cuts keep their move; a one-segment scene is a shot of its own). A run whose
    window (fit of shot_bbox + hold_pad) is wider than camera.max_shot_window_frac x the board width is not a valid
    shot. Among the valid splits of a scene (dynamic programming) the tightest framing wins: least sum over the
    segments of log(shot window width); a shot of one segment in a scene of more than one costs
    SHOT_SINGLE_PENALTY extra (only used when no split into shot_size runs fits)."""
    sizes, wmax = cam["shot_size"], cam["max_shot_window_frac"] * board["width"]
    shots = [{"idx": [0], "scene": None, "window": fit(pieces[0]["ink_bbox"], cam["hold_pad"], board)}]
    i = 1
    while i < len(pieces):
        j = i
        while j < len(pieces) and pieces[j]["scene"] == pieces[i]["scene"]:
            j += 1
        n = j - i
        best: list[tuple[float, int, tuple]] = [(0.0, 0, ())] + [(math.inf, 0, ())] * n   # (cost, size, window)
        for k in range(1, n + 1):
            for s in sorted(set(sizes) | {1}):
                if s > k or best[k - s][0] == math.inf:
                    continue
                win = fit(shot_bbox(pieces, list(range(i + k - s, i + k))), cam["hold_pad"], board)
                if s > 1 and win[2] > wmax:
                    continue
                c = best[k - s][0] + s * math.log(win[2]) + (SHOT_SINGLE_PENALTY if s not in sizes and n > 1 else 0)
                if c < best[k][0] - 1e-9:
                    best[k] = (c, s, win)
        runs, k = [], n
        while k:
            runs.append((list(range(i + k - best[k][1], i + k)), best[k][2]))
            k -= best[k][1]
        shots += [{"idx": idx, "scene": pieces[i]["scene"], "window": win} for idx, win in reversed(runs)]
        i = j
    return shots


def frame_prep(pieces: list[dict], cam: dict, board: dict) -> list[dict]:
    """Per piece (after layout and link_arrows): reveal track, its shot (plan_shots), the shot's window as its hold
    window, and for a shot's first piece the window the glide into it passes through (`union`): centred on the
    previous piece and this one (the flow arrow between them sits there), as wide as both or at least the geometric
    mean of the two shot windows, so the glide eases in log width (a gentle push-in or pull-out between shots of
    different sizes, never a zoom out and back in larger than the two pieces need)."""
    pad = cam["hold_pad"]
    for p in pieces:
        p["track"] = reveal_track(p)
    shots = plan_shots(pieces, cam, board)
    for k, sh in enumerate(shots):
        for n, i in enumerate(sh["idx"]):
            p = pieces[i]
            p["shot"], p["shot_first"], p["window"] = k, n == 0, sh["window"]
            if not holds(sh["window"], p["ink_bbox"]):
                raise Fail(f"camera: shot {k} window {sh['window']} does not hold piece {p['id']}")
        if k:
            prev, first = pieces[sh["idx"][0] - 1], pieces[sh["idx"][0]]
            u = fit(union(prev["ink_bbox"], first["ink_bbox"]), pad, board)
            first["union"] = (u[0], u[1], max(u[2], math.sqrt(prev["window"][2] * sh["window"][2])))
    return shots


def glide(prev: dict, nxt: dict, a: float, b: float, t: float) -> tuple[float, float, float]:
    """The glide window from prev's hold to nxt's (through their shared window) at time t of a move a..b."""
    return through(prev["window"], nxt["union"], nxt["window"], smooth((t - a) / (b - a)))


def holds(win: tuple, bbox: list[float]) -> bool:
    cx, cy, w = win
    h = w / ASPECT
    return (bbox[0] - EDGE_PX >= cx - w / 2 - 1e-6 and bbox[2] + EDGE_PX <= cx + w / 2 + 1e-6
            and bbox[1] - EDGE_PX >= cy - h / 2 - 1e-6 and bbox[3] + EDGE_PX <= cy + h / 2 + 1e-6)


def camera_plan(pieces: list[dict], shots: list[dict], sch: dict, cam: dict, board: dict) -> tuple:
    """Moves (shot k-1 -> shot k: from the previous shot's last piece to shot k's first) and the camera function
    at(t). Inside a shot the window holds still on the shot window; a flow arrow between two pieces of one shot was
    placed by schedule() (drawn right before its piece, in the held frame). A move starts at max(previous draw
    end + dwell_s, next draw start - move_s/2) and lasts move_s (cut short by the following move or the
    zoom-out); the window follows the glide and is widened to hold every stroke revealed so far of the shot's
    pieces. A flow arrow into a shot's first piece draws across mid-glide (method 3b; finished before the piece
    starts); the window is widened where needed to hold the whole arrow, eased in (smoothstep, log width) from
    the move's start and out by the move's end."""
    firsts = [sh["idx"][0] for sh in shots[1:]]
    starts = [move_start(pieces[i - 1]["end"] + cam["dwell_s"], pieces[i]["start"], pieces[i], cam) for i in firsts]
    moves = []
    for k, i in enumerate(firsts):
        a = starts[k]
        b = min(a + cam["move_s"], starts[k + 1] if k + 1 < len(starts) else sch["zoom_start"])
        prev, nxt = pieces[i - 1], pieces[i]
        if not prev["end"] + cam["dwell_s"] - 1e-9 <= a <= nxt["start"] or b <= a:
            raise Fail(f"camera: move {prev['id']} -> {nxt['id']} {a:.3f}..{b:.3f} s breaks the dwell/hand-off order "
                       f"(previous ends {prev['end']:.3f} s, next starts {nxt['start']:.3f} s)")
        if nxt["start"] < b and not holds(glide(prev, nxt, a, b, nxt["start"]),
                                          revealed(nxt, float(nxt["track"][0][0]) + FIRST_S)):
            raise Fail(f"camera: the first strokes of {nxt['id']} are out of frame at its draw start "
                       f"{nxt['start']:.3f} s (move {a:.3f}..{b:.3f} s)")
        ar = nxt.get("arrow_in")
        if ar is not None:                    # drawn across mid-glide, where the window holds both pieces
            ar["start"] = a + (cam["move_s"] - ar["draw_s"]) / 2
            if ar["start"] + ar["draw_s"] > min(nxt["start"], b) + 1e-6:
                raise Fail(f"camera: the arrow {ar['from']} -> {ar['to']} ({ar['start']:.3f} + {ar['draw_s']:.3f} s) "
                           f"is not finished before {nxt['id']} starts at {nxt['start']:.3f} s / the move ends at "
                           f"{b:.3f} s")
        moves.append({"from": prev["id"], "to": nxt["id"], "start_s": a, "handoff_s": nxt["start"], "end_s": b,
                      "dwell_s": a - prev["end"], "i": i, "shot": k + 1, "union": nxt["union"],
                      "scene_change": prev["scene"] != nxt["scene"], "arrow": ar is not None})
    for sh in shots:
        for i in sh["idx"][1:]:
            ar = pieces[i].get("arrow_in")
            if ar is not None and not (pieces[i - 1]["end"] - 1e-6 <= ar["start"]
                                       <= pieces[i]["start"] - ar["draw_s"] + 1e-6):
                raise Fail(f"camera: the in-shot arrow {ar['from']} -> {ar['to']} ({ar['start']:.3f} s) is not drawn "
                           f"between {pieces[i - 1]['id']} and {pieces[i]['id']}")
    whole = (board["width"] / 2, board["height"] / 2, float(board["width"]))
    mstarts = np.array([m["start_s"] for m in moves])

    def at(t: float) -> tuple[float, float, float]:
        if t >= sch["zoom_start"]:
            x = smooth((t - sch["zoom_start"]) / cam["end_zoom_s"])
            a, b = pieces[-1]["window"], whole
            w = math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * x)
            return (a[0] + (b[0] - a[0]) * x, a[1] + (b[1] - a[1]) * x, w)
        k = int(np.searchsorted(mstarts, t, side="right")) - 1
        if k < 0:
            return pieces[0]["window"]
        m = moves[k]
        nxt = pieces[m["i"]]
        if t >= m["end_s"]:
            return nxt["window"]
        win = glide(pieces[m["i"] - 1], nxt, m["start_s"], m["end_s"], t)
        ar = nxt.get("arrow_in")
        if ar is not None:   # hold the whole arrow while it draws (eased in from the move's start, out by its end)
            a0, e = m["start_s"], ar["start"] + ar["draw_s"]
            hold = (smooth((t - a0) / max(ar["start"] - a0, 1e-9)) if t < ar["start"]
                    else 1.0 if t <= e else 1.0 - smooth((t - e) / max(m["end_s"] - e, 1e-9)))
            if hold > 0:
                x0, y0, x1, y1 = ar["bbox"]
                pad = cam["hold_pad"] * board["min_w"]
                wc = contain(win, [x0 - pad, y0 - pad, x1 + pad, y1 + pad], board["min_w"])
                win = (win[0], win[1], math.exp(math.log(win[2]) + hold * (math.log(wc[2]) - math.log(win[2]))))
        for i in shots[m["shot"]]["idx"]:    # every stroke of the shot drawn so far (a short first piece may hand
            p = pieces[i]                    # over to the next one of its shot before the glide ends)
            if t < p["start"]:
                break
            box = revealed(p, t - p["start"])
            if box is not None:
                win = contain(win, box, board["min_w"])
            ar = p.get("arrow_in")
            if ar is not None and i != m["i"] and t >= ar["start"]:
                win = contain(win, ar["bbox"], board["min_w"])
        return win
    return moves, at


# ------------------------------------------------------------------ frames

class Compositor:
    """Warps each started piece's darkness + reveal-time map for a camera and multiplies the layers onto white."""

    def __init__(self, pieces: list[dict], size: tuple[int, int]):
        self.pieces, self.size = pieces, size
        self.cam_key = None
        self.cache: dict[int, dict | None] = {}
        self.static_key = None
        self.static = None
        self.static_bytes = b""
        self.buf = np.empty((size[1], size[0], 3), np.float32)
        self.k = np.float32(1 / rv.FADE_S)

    def layer(self, i: int, cam: tuple[float, float, float]) -> dict | None:
        if i in self.cache:
            return self.cache[i]
        import cv2
        OW, OH = self.size
        cx, cy, w = cam
        s = OW / w
        S = np.array([[s, 0, -(cx - w / 2) * s], [0, s, -(cy - w * OH / OW / 2) * s], [0, 0, 1]])
        pc = self.pieces[i]
        f_eff = pc["scale"] * s
        L = 0 if f_eff >= 1 else min(LEVELS - 1, int(math.floor(math.log2(1 / f_eff))))
        f = 1 << L
        ox, oy = pc["box"][0], pc["box"][1]
        lev = np.array([[f, 0, ox + (f - 1) / 2], [0, f, oy + (f - 1) / 2], [0, 0, 1]])
        M = (S @ pc["A3"] @ lev)[:2]
        dark = pc["dark"][L]
        h, wd = dark.shape[:2]
        cr = M @ np.array([[-0.5, wd - 0.5, -0.5, wd - 0.5], [-0.5, -0.5, h - 0.5, h - 0.5], [1, 1, 1, 1]])
        x0, y0 = max(0, int(math.floor(cr[0].min()))), max(0, int(math.floor(cr[1].min())))
        x1, y1 = min(OW, int(math.ceil(cr[0].max())) + 1), min(OH, int(math.ceil(cr[1].max())) + 1)
        ly = None
        if x0 < x1 and y0 < y1:
            Mr = M.copy()
            Mr[:, 2] -= (x0, y0)
            d = cv2.warpAffine(dark, Mr, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                               borderValue=0)
            ly = {"i": i, "roi": (x0, y0, x1, y1), "d": d.astype(np.float32) * np.float32(1 / 255)}
            if pc["T"] is not None:
                ly["T"] = cv2.warpAffine(pc["T"][L], Mr, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                                         borderMode=cv2.BORDER_REPLICATE)
        self.cache[i] = ly
        return ly

    @staticmethod
    def to_bytes(a: np.ndarray) -> bytes:
        return (a * np.float32(255) + np.float32(0.5)).astype(np.uint8).tobytes()

    def frame(self, t: float, cam: tuple[float, float, float]) -> bytes:
        key = (round(cam[0], 4), round(cam[1], 4), round(cam[2], 4))
        if key != self.cam_key:
            self.cam_key, self.cache, self.static_key = key, {}, None
        done, active = [], []
        for i, pc in enumerate(self.pieces):
            if pc["T"] is None:                                     # fully drawn (board.png): no start time needed
                done.append(i)
                continue
            tl = t - pc["start"]
            if tl >= pc["tmax"] + rv.FADE_S:
                done.append(i)
            elif tl > pc["tmin"]:
                active.append(i)
        skey = tuple(done)
        if skey != self.static_key:
            st = np.ones_like(self.buf)
            for i in done:
                ly = self.layer(i, cam)
                if ly is not None:
                    x0, y0, x1, y1 = ly["roi"]
                    st[y0:y1, x0:x1] *= 1 - ly["d"]
            self.static_key, self.static, self.static_bytes = skey, st, self.to_bytes(st)
        layers = [ly for ly in (self.layer(i, cam) for i in active) if ly is not None]
        if not layers:
            return self.static_bytes
        fr = self.buf
        np.copyto(fr, self.static)
        for ly in layers:
            x0, y0, x1, y1 = ly["roi"]
            a = np.subtract(np.float32(t - self.pieces[ly["i"]]["start"]), ly["T"])
            np.multiply(a, self.k, out=a)
            np.clip(a, 0, 1, out=a)
            m = ly["d"] * a[..., None]
            np.subtract(np.float32(1), m, out=m)
            fr[y0:y1, x0:x1] *= m
        return self.to_bytes(fr)


def write_board_png(layers: list[dict], board: dict, out: Path) -> list[int]:
    """The whole board fully drawn (every piece and flow arrow), BOARD_PNG_W wide, 16:9."""
    from PIL import Image
    W, H = board["width"], board["height"]
    size = (BOARD_PNG_W, BOARD_PNG_W * 9 // 16)
    maps = [p["T"] for p in layers]
    for p in layers:
        p["T"] = None                                            # fully drawn
    try:
        buf = Compositor(layers, size).frame(0.0, (W / 2, H / 2, float(W)))
    finally:
        for p, m in zip(layers, maps):
            p["T"] = m
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".partial.png")
    Image.frombytes("RGB", size, buf).save(tmp)
    tmp.replace(out)
    return list(size)


# ------------------------------------------------------------------ audio + excerpts

def final_audio(narr: np.ndarray, sch: dict) -> np.ndarray:
    """Silent lead-in, the narration with every inserted pause, silence to the last frame."""
    total = sch["frames"] * SR // FPS
    parts, cur = [np.zeros(round(sch["lead"] * SR), np.float32)], 0
    for p in sch["pauses"]:
        at = round(p["_src_s"] * SR)
        parts += [narr[cur:at], np.zeros(round(p["length_s"] * SR), np.float32)]
        cur = at
    parts.append(narr[cur:])
    track = np.concatenate(parts)
    if track.size > total:
        raise Fail(f"audio {track.size / SR:.3f} s is longer than the video {total / SR:.3f} s")
    return np.concatenate([track, np.zeros(total - track.size, np.float32)])


def scene_cuts(pieces: list[dict], moves: list[dict], frames: int) -> list[dict]:
    """Scene n: from 0 (scene 1, with the title) or min(start of the move into its first segment, that segment's
    speech start), floored to a frame so no excerpt starts or ends inside a sentence, to the next cut."""
    ids = []
    for p in pieces[1:]:
        if p["scene"] not in ids:
            ids.append(p["scene"])
    starts, speech = [], []
    for n in ids:
        first = next(p for p in pieces if p["scene"] == n)
        speech.append(first["speech"][0])
        if n == ids[0]:
            starts.append(0)
            continue
        mv = next(m for m in moves if m["to"] == first["id"])
        starts.append(math.floor(round(min(mv["start_s"], first["speech"][0]) * FPS, 6)))
    out = []
    for i, n in enumerate(ids):
        f0, f1 = starts[i], (starts[i + 1] if i + 1 < len(ids) else frames)
        if f1 <= f0:
            raise Fail(f"scene {n}: empty excerpt (frames {f0}..{f1})")
        last_speech = max(p["speech"][1] for p in pieces if p["scene"] == n)
        # the next scene's camera move starts right after this scene's last word: hold the last frame (silent) so
        # the excerpt does not stop on the word
        hold = max(0, math.ceil(round((last_speech + EXCERPT_TAIL_S - f1 / FPS) * FPS, 6)))
        out.append({"scene": n, "pieces": [p["id"] for p in pieces if p["scene"] == n or (i == 0 and p["scene"] is None)],
                    "frames": [f0, f1], "start_s": r3(f0 / FPS), "end_s": r3(f1 / FPS),
                    "first_speech_s": r3(speech[i]), "last_speech_end_s": r3(last_speech), "hold_frames": hold})
    return out


def cut_excerpt(video: Path, f0: int, f1: int, out: Path, preset: str, hold: int = 0) -> dict:
    """Frames f0..f1-1 of video (and their audio), then the last frame held for `hold` frames over silence (the
    audio fades out over its last EXCERPT_FADE_S first, so the cut cannot click), re-encoded with the final.mp4
    spec, probed, put in place."""
    part = out.with_name(out.stem + ".partial.mp4")
    cut = f1 - f0
    n = cut + hold
    fade = min(EXCERPT_FADE_S, cut / FPS)
    vf = f"tpad=stop_mode=clone:stop={hold}" if hold else "null"
    af = (f"atrim=duration={cut / FPS:.6f},afade=t=out:st={cut / FPS - fade:.6f}:d={fade:.6f},"
          f"apad=whole_dur={n / FPS:.6f}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
           "-ss", f"{f0 / FPS:.6f}", "-t", f"{cut / FPS:.6f}", "-i", str(video),
           "-filter_complex", f"[0:v:0]{vf}[v];[0:a:0]{af}[a]", "-map", "[v]", "-map", "[a]",
           "-frames:v", str(n), "-t", f"{n / FPS:.6f}",
           "-c:v", "libx264", "-preset", preset, "-crf", "18", "-pix_fmt", "yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode != 0:
        part.unlink(missing_ok=True)
        raise Fail(f"ffmpeg could not cut {out.name} (frames {f0}..{f1}): {p.stderr.strip()[-400:]}")
    info = rv.probe(part)
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    au = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if (not v or v[0].get("nb_frames") != str(n) or v[0].get("pix_fmt") != "yuv420p"
            or v[0].get("color_space") != "bt709" or len(au) != 1 or au[0].get("codec_name") != "aac"):
        part.unlink(missing_ok=True)
        raise Fail(f"{out.name}: excerpt check failed (want {n} frames yuv420p bt709 + aac): {v} {au}")
    part.replace(out)
    return {"video": str(out), "frames": n, "duration_s": float(info["format"]["duration"])}


# ------------------------------------------------------------------ timeline doc

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def rel(run: Path, p: Path) -> str:
    return p.relative_to(run).as_posix()


def timeline_json(run: Path, video: Path, board_png: Path, pieces: list[dict], arrows: list[dict], board: dict,
                  sch: dict, shots: list[dict], moves: list[dict], cuts: list[dict], used: dict, lay_doc: dict,
                  size: tuple[int, int]) -> dict:
    def win(w):
        return [round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[2] / ASPECT, 1)]

    seed = lay_doc["seed"]
    doc = {
        "version": 2, "fps": FPS, "size": list(size), "frames": sch["frames"],
        "duration_s": r3(sch["frames"] / FPS), "video": str(video), "board_png": str(board_png),
        "lead_in_s": r3(sch["lead"]),
        "board": {"width": board["width"], "height": board["height"], "seed": seed, "rows": board["rows"],
                  "layout_fingerprint": lay_doc["fingerprint"]},
        "pieces": [{"id": p["id"], "scene": p["scene"], "source": rel(run, p["source"]), "scale": p["scale"],
                    "tilt_deg": p["tilt_deg"], "affine": [[float(v) for v in row] for row in p["A3"][:2]],
                    "ink_bbox": [round(v, 1) for v in p["ink_bbox"]], "window": win(p["window"]),
                    "draw_start_s": r3(p["start"]), "draw_end_s": r3(p["end"]),
                    "speech_start_s": None if p["speech"] is None else r3(p["speech"][0]),
                    "speech_end_s": None if p["speech"] is None else r3(p["speech"][1]),
                    "late_s": r3(p.get("late", 0.0)), "speed": round(p["speed"], 4), "ink_px": p["ink_px"],
                    "draw": p["stats"]} for p in pieces],
        "scenes": [c | {"video": str(video.parent / f"Scene-{c['scene']:02d}.mp4")} for c in cuts],
        "shots": [{"shot": k, "scene": sh["scene"], "segments": [pieces[i]["id"] for i in sh["idx"]],
                   "window": win(sh["window"]), "draw_start_s": r3(pieces[sh["idx"][0]]["start"]),
                   "draw_end_s": r3(pieces[sh["idx"][-1]]["end"])} for k, sh in enumerate(shots)],
        "moves": [{"from": m["from"], "to": m["to"], "to_shot": m["shot"], "start_s": r3(m["start_s"]),
                   "handoff_s": r3(m["handoff_s"]), "end_s": r3(m["end_s"]), "dwell_s": r3(m["dwell_s"]),
                   "scene_change": m["scene_change"], "arrow": m["arrow"], "handoff_window": win(m["union"])}
                  for m in moves],
        "arrows": [{"from": ar["from"], "to": ar["to"], "start_s": r3(ar["start"]),
                    "end_s": r3(ar["start"] + ar["draw_s"]), "colour": ar["rec"]["colour"], "head": ar["rec"]["head"],
                    "width_px": ar["rec"]["width_px"], "length_px": ar["rec"]["length_px"]} for ar in arrows],
        "pauses": [{k: v for k, v in p.items() if not k.startswith("_")} for p in sch["pauses"]],
        "ending": {"zoom_start_s": r3(sch["zoom_start"]), "zoom_end_s": r3(sch["zoom_end"]), "end_s": r3(sch["end"])},
        "camera": dict(used["camera"]) | {"ease": "smoothstep", "min_window_w": round(board["min_w"], 1),
                                          "edge_px": EDGE_PX},
        "layout": dict(used["layout"]) | {"seed": seed},
        "module": used,
    }
    drawing = ({k: used[k] for k in (*rv.MODULE_KEYS, *STITCH_KEYS)}
               | {k: used["camera"][k] for k in ("dwell_s", "max_speedup")})
    doc["fingerprints"] = {
        "schedule": sha({"pieces": [[p["id"], p["draw_start_s"], p["draw_end_s"], p["speech_start_s"], p["speed"]]
                                    for p in doc["pieces"]], "pauses": doc["pauses"], "drawing": drawing}),
        "build": sha({k: v for k, v in doc.items() if k not in ("video", "board_png", "scenes")}
                     | {"cuts": [c["frames"] for c in cuts]}),
    }
    return doc


# ------------------------------------------------------------------ commands

def cmd_board(a) -> dict:
    """Layout step: pack, place the flow arrows, write board.png and board_layout.json (written last)."""
    t0 = time.time()
    run = Path(a.run).resolve()
    mod, al = rv.load_module(rv.run_style(run)), rv.load_alignment(run)
    lay, cam = stitch_config(mod)
    fp, files = layout_fingerprint(run, mod, al)
    pieces = load_pieces(run, mod, al)
    say(f"{len(pieces)} pieces loaded in {time.time() - t0:.1f} s")
    board = layout(pieces, lay, lay["seed"])
    arrows, fill = plan_arrows(pieces, board, lay["arrows"], arrow_colours(run))
    layers = [arrow_layer(x, cam["arrow_draw_s"]) for x in arrows]
    vdir = run / "videos"
    png = vdir / "board.png"
    png_size = write_board_png(pieces + layers, board, png)
    doc = {"version": LAYOUT_VERSION, "fingerprint": fp, "inputs": files,
           "module": {"layout": lay, "ink_threshold": mod["ink_threshold"]}, "seed": lay["seed"],
           "board": {k: board[k] for k in ("width", "height", "rows", "min_w", "frame_factor", "bands")},
           "fill": fill, "board_png": str(png), "board_png_size": png_size,
           "compounds": [list(c) for c in dict.fromkeys(p["compound"] for p in pieces if p.get("compound"))],
           "pieces": [{"id": p["id"], "scene": p["scene"], "source": rel(run, p["source"]), "scale": p["scale"],
                       "tilt_deg": p["tilt_deg"], "affine": [[float(v) for v in row] for row in p["A3"][:2]],
                       "ink_bbox": [round(v, 1) for v in p["ink_bbox"]]} for p in pieces],
           "arrows": arrows}
    lp = vdir / LAYOUT_FILE
    rv.write_json(lp, doc)
    say(f"board {board['width']}x{board['height']} ({board['bands']} bands), {len(arrows)} arrows, fill {fill}; "
        f"{png} + {lp} in {time.time() - t0:.1f} s")
    return {"cmd": "board", "run": str(run), "layout": str(lp), "board_png": str(png), "board_png_size": png_size,
            "board": [board["width"], board["height"]], "rows": [len(r) for r in board["rows"]], "seed": lay["seed"],
            "fingerprint": fp, "fill": fill, "arrows": [f"{x['from']}->{x['to']}" for x in arrows],
            "compounds": ["+".join(c) for c in dict.fromkeys(p["compound"] for p in pieces if p.get("compound"))]}


def setup(a):
    """Pieces placed from the saved layout (never packed again), arrows, schedule, camera, scene cuts."""
    run = Path(a.run).resolve()
    mod, al = rv.load_module(rv.run_style(run)), rv.load_alignment(run)
    lay, cam = stitch_config(mod)
    lay_doc = load_layout(run, mod, al)
    t = time.time()
    pieces = load_pieces(run, mod, al)
    say(f"{len(pieces)} pieces loaded in {time.time() - t:.1f} s")
    t = time.time()
    board = apply_layout(pieces, lay_doc)
    arrows = [arrow_layer(x, cam["arrow_draw_s"]) for x in lay_doc["arrows"]]
    link_arrows(pieces, arrows)
    shots = frame_prep(pieces, cam, board)
    sch = schedule(pieces, mod, cam, al)
    moves, at = camera_plan(pieces, shots, sch, cam, board)
    cuts = scene_cuts(pieces, moves, sch["frames"])
    say(f"layout {board['width']}x{board['height']} rows {[len(r) for r in board['rows']]} seed {lay_doc['seed']}, "
        f"{len(arrows)} arrows, from {LAYOUT_FILE} in {time.time() - t:.1f} s; {sch['frames']} frames "
        f"({sch['frames'] / FPS:.2f} s), lead-in {sch['lead']:.2f} s, {len(sch['pauses'])} pauses")
    cams = np.array([at(i / FPS) for i in range(sch["frames"])])
    step = np.hypot(np.diff(cams[:, 0]), np.diff(cams[:, 1])) / cams[1:, 2]
    zoom = np.abs(np.diff(np.log(cams[:, 2])))
    dw = [m["dwell_s"] for m in moves]
    say(f"camera: dwell before each move min {min(dw):.3f} s, median {float(np.median(dw)):.3f} s; largest frame step "
        f"{step.max():.3f} window widths (t {step.argmax() / FPS:.2f} s), zoom {zoom.max():.3f} log "
        f"(t {zoom.argmax() / FPS:.2f} s)")
    sizes = [len(sh["idx"]) for sh in shots[1:]]
    say(f"shots: {len(shots) - 1} after the title, sizes {dict(sorted(Counter(sizes).items()))}, "
        f"{len(moves)} moves; widest shot window {max(sh['window'][2] for sh in shots[1:]):.0f} px "
        f"(cap {cam['max_shot_window_frac'] * board['width']:.0f})")
    return run, al, mod, lay, cam, lay_doc, pieces, arrows, board, sch, shots, moves, at, cuts


def summary(res: dict, doc: dict) -> dict:
    return res | {"frames": doc["frames"], "duration_s": doc["duration_s"], "lead_in_s": doc["lead_in_s"],
                  "board": [doc["board"]["width"], doc["board"]["height"]], "rows": doc["board"]["rows"],
                  "seed": doc["board"]["seed"], "layout_fingerprint": doc["board"]["layout_fingerprint"],
                  "arrows": len(doc["arrows"]), "shots": [s["segments"] for s in doc["shots"]],
                  "moves": len(doc["moves"]), "pauses": doc["pauses"],
                  "late": {p["id"]: p["late_s"] for p in doc["pieces"] if p["late_s"] > 0},
                  "speed_up": {p["id"]: p["speed"] for p in doc["pieces"] if p["id"] != TITLE_ID and p["speed"] > 1},
                  "scenes": [{k: c[k] for k in ("scene", "start_s", "end_s", "first_speech_s")} for c in doc["scenes"]],
                  "ending": doc["ending"]}


def cmd_plan(a) -> dict:
    run, al, mod, lay, cam, lay_doc, pieces, arrows, board, sch, shots, moves, at, cuts = setup(a)
    tlp = run / "videos" / "preview" / "final_timeline.json"
    tlp.parent.mkdir(parents=True, exist_ok=True)
    doc = timeline_json(run, tlp.parent / "final.mp4", run / "videos" / "board.png", pieces, arrows, board, sch,
                        shots, moves, cuts, module_used(mod, lay, cam), lay_doc, rv.PREVIEW_SIZE)
    rv.write_json(tlp, doc)
    return summary({"cmd": "plan", "run": str(run), "timeline": str(tlp)}, doc)


def cmd_build(a) -> dict:
    t0 = time.time()
    run, al, mod, lay, cam, lay_doc, pieces, arrows, board, sch, shots, moves, at, cuts = setup(a)
    png = run / "videos" / "board.png"
    if not png.is_file():
        raise Fail(f"{png} missing: run `stitch.py board` (it writes the board image Gate 2 approves)")
    narr = rv.decode_narration(run, al)
    audio = final_audio(narr, sch)
    size, preset = (rv.PREVIEW_SIZE, "veryfast") if a.preview else (rv.FULL_SIZE, "medium")
    vdir = run / "videos" / "preview" if a.preview else run / "videos"
    video, tlp = vdir / "final.mp4", vdir / "final_timeline.json"
    comp = Compositor(pieces + arrows, size)
    t1 = time.time()
    frames = (comp.frame(i / FPS, at(i / FPS)) for i in range(sch["frames"]))
    enc = rv.encode(frames, size, sch["frames"], audio, video, preset)
    t2 = time.time()
    if abs(enc["duration_s"] - sch["frames"] / FPS) > 0.1:
        raise Fail(f"{video.name} probes {enc['duration_s']:.3f} s, the timeline says {sch['frames'] / FPS:.3f} s")
    excerpts = []
    if not a.preview:
        for c in cuts:
            excerpts.append(cut_excerpt(video, c["frames"][0], c["frames"][1], vdir / f"Scene-{c['scene']:02d}.mp4",
                                        preset, hold=c["hold_frames"]))
    t3 = time.time()
    doc = timeline_json(run, video, png, pieces, arrows, board, sch, shots, moves, cuts, module_used(mod, lay, cam),
                        lay_doc, size)
    rv.write_json(tlp, doc)
    say(f"{video} {sch['frames']} frames, prep {t1 - t0:.1f} s, frames+encode {t2 - t1:.1f} s, excerpts {t3 - t2:.1f} s")
    return summary({"cmd": "build", "run": str(run), "preview": bool(a.preview), "video": str(video),
                    "timeline": str(tlp), "board_png": str(png), "size": list(size),
                    "render_s": round(t3 - t0, 1), "encode_s": round(t2 - t1, 1), "excerpt_s": round(t3 - t2, 1),
                    "probe": enc, "excerpts": excerpts}, doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("board", help="layout + flow arrows -> videos/board.png + videos/board_layout.json (Gate 2 input)"
                   ).add_argument("--run", required=True)
    b = sub.add_parser("build", help="final video + timeline + scene excerpts from the saved board layout")
    b.add_argument("--run", required=True)
    b.add_argument("--preview", action="store_true", help="960x540 to videos/preview/ for a quick look (no excerpts)")
    sub.add_parser("plan", help="schedule from the saved board layout -> videos/preview/final_timeline.json"
                   ).add_argument("--run", required=True)
    a = ap.parse_args(argv)
    try:
        res = {"board": cmd_board, "build": cmd_build, "plan": cmd_plan}[a.cmd](a)
    except Fail as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        return 1
    print("STITCH_JSON " + json.dumps(res, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
