#!/usr/bin/env python
r"""prompt_meta.py - every rendered image and video carries the prompt that made it.

Chipp, 2026-09-03: "All images and videos need to have their prompts stored.
You should be able to store the prompt in the metadata of the object if
possible assuming it is easy to retrieve." And: "whenever I ask you to
re-render an image or video, display the prompt in .txt format and always
launch it. Do not ask for confirmation."

Why: on 2026-09-03 the prompt behind an approved clip could not be recovered
- the prompts folder had been regenerated - and a walking-backwards defect
that had been fixed once came back because nobody could see what the
second render had been asked for. A result without its cause cannot be
reasoned about.

What this module does, for every driver in the /render roster and the
vid-wiz pipeline:

  stamp(path, prompt, meta)   embed the prompt IN the file, losslessly, and
                              write the sidecar <stem>.prompt.txt beside it.
                              sidecar=False embeds only and writes no
                              sidecar (a WebP twin of a stamped PNG shares
                              its stem; the PNG's sidecar must survive).
  read(path)                  get the prompt back out (file first, sidecar
                              second). `prompt_meta.py read <file>` at the
                              shell.
  preview(path, prompt, meta) BEFORE a re-render: write the sidecar for the
                              file that is about to exist and open it in the
                              default .txt viewer, so the prompt is on screen
                              while the render runs. Fires when the caller
                              passes --show-prompt or the environment has
                              RENDER_SHOW_PROMPT=1 (a pipeline's --rework
                              sets that so every driver underneath obeys).
  show(path)                  open an existing file's prompt sidecar,
                              rebuilding it from the metadata if missing.

Where the prompt lives inside each file type - all readable with common tools:

  .png        iTXt chunks `prompt` and `render` (UTF-8), inserted after IHDR.
              Pillow: Image.open(p).text["prompt"]. exiftool: -Prompt.
              Pixel data untouched - chunks are spliced, nothing re-encoded.
  .jpg/.jpeg  COM segments `prompt:...` and `render:...` after the APPn
              headers. Pillow: Image.open(p).info["comment"]. exiftool:
              -Comment. Bytes of the picture untouched.
  .mp4/.mov   `comment` = prompt, `description` = render JSON, written by
  .mkv/.webm  ffmpeg -c copy (a remux, no re-encode). ffprobe shows them
              under format.tags; Windows Explorer shows `comment` as
              Comments on an .mp4.
  .webp       an `XMP ` chunk at the end of the RIFF container. In it,
              dc:description = prompt, render:record = render JSON. The
              file becomes extended layout (VP8X with the XMP flag) if it
              was not already. Pillow: Image.open(p).info["xmp"] (bytes).
              exiftool: -XMP-dc:Description, -XMP:Record or
              -XMP-render:Record. Image chunks untouched, nothing re-encoded.

`render` is a small JSON record: engine, model, source image, reference
images, seed/steps where the engine has them, and when it was stamped. The
sidecar is the prompt first, then a `----- render -----` block with the
same facts, so a human opening it reads the prompt at the top.

Stamping keeps the file's mtime by default: it is the same picture with its
cause attached, and the pipeline's "newer than" checks mean "new picture".

Stdlib only, plus ffmpeg/ffprobe on PATH for video.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

PROMPT_KEY = "prompt"
META_KEY = "render"
SHOW_ENV = "RENDER_SHOW_PROMPT"
DIVIDER = "----- render -----"

IMAGE_PNG = {".png"}
IMAGE_JPG = {".jpg", ".jpeg"}
IMAGE_WEBP = {".webp"}
# Anything ffmpeg can remux with a `comment` tag: video containers and the
# audio formats the voice cell writes (WAV -> INFO/ICMT, MP3 -> ID3 COMM,
# FLAC/OGG -> Vorbis comment, M4A -> the same atoms as MP4).
VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".webm",
         ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

FFMPEG = shutil.which("ffmpeg") or r"C:\Program Files\FFmpeg\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or r"C:\Program Files\FFmpeg\bin\ffprobe.exe"


def log(msg):
    print(msg, flush=True)


def show_wanted():
    return os.environ.get(SHOW_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def sidecar_path(path):
    p = Path(path)
    return p.with_name(p.stem + ".prompt.txt")


# --------------------------------------------------------------------- sidecar
def _meta_lines(meta):
    L = []
    for k, v in (meta or {}).items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (list, tuple)):
            L.append(f"{k}:")
            L.extend(f"  - {x}" for x in v)
        elif isinstance(v, dict):
            L.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            L.append(f"{k}: {v}")
    return L


def write_sidecar(path, prompt, meta=None):
    p = Path(path)
    sc = sidecar_path(p)
    meta = dict(meta or {})
    meta.setdefault("file", str(p.resolve() if p.exists() else p))
    meta.setdefault("stamped", time.strftime("%Y-%m-%d %H:%M:%S"))
    body = (prompt or "").rstrip() + "\n\n" + DIVIDER + "\n" + "\n".join(_meta_lines(meta)) + "\n"
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(body, encoding="utf-8")
    return sc


def read_sidecar(path):
    sc = sidecar_path(path)
    if not sc.exists():
        return None, None
    text = sc.read_text(encoding="utf-8", errors="replace")
    if "\n" + DIVIDER in text:
        prompt, _, tail = text.partition("\n" + DIVIDER)
        return prompt.strip(), tail.strip()
    return text.strip(), None


# ------------------------------------------------------------------------- PNG
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunks(data):
    if data[:8] != PNG_SIG:
        raise ValueError("not a PNG")
    i, out = 8, []
    while i + 8 <= len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + n]
        out.append((typ, body))
        i += 12 + n
        if typ == b"IEND":
            break
    return out


def _png_chunk(typ, body):
    return struct.pack(">I", len(body)) + typ + body + \
        struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def _png_text_keyword(typ, body):
    if typ in (b"tEXt", b"zTXt", b"iTXt"):
        return body.split(b"\0", 1)[0].decode("latin-1", "replace")
    return None


def _png_text_value(typ, body):
    if typ == b"tEXt":
        return body.split(b"\0", 1)[1].decode("latin-1", "replace")
    if typ == b"zTXt":
        rest = body.split(b"\0", 1)[1]
        return zlib.decompress(rest[1:]).decode("latin-1", "replace")
    if typ == b"iTXt":
        _kw, rest = body.split(b"\0", 1)
        flag, method = rest[0], rest[1]
        rest = rest[2:]
        _lang, rest = rest.split(b"\0", 1)
        _tr, rest = rest.split(b"\0", 1)
        if flag == 1:
            rest = zlib.decompress(rest)
        return rest.decode("utf-8", "replace")
    return None


def _itxt(keyword, text):
    return _png_chunk(b"iTXt", keyword.encode("latin-1") + b"\0" + b"\0\0"
                      + b"\0" + b"\0" + text.encode("utf-8"))


def _stamp_png(p, prompt, meta_json):
    data = p.read_bytes()
    chunks = _png_chunks(data)
    kept = [(t, b) for t, b in chunks
            if _png_text_keyword(t, b) not in (PROMPT_KEY, META_KEY)]
    out = bytearray(PNG_SIG)
    for i, (t, b) in enumerate(kept):
        out += _png_chunk(t, b)
        if i == 0:                                   # right after IHDR
            out += _itxt(PROMPT_KEY, prompt)
            out += _itxt(META_KEY, meta_json)
    _atomic_write(p, bytes(out))
    return "png-iTXt"


def _read_png(p):
    data = p.read_bytes()
    found = {}
    for t, b in _png_chunks(data):
        kw = _png_text_keyword(t, b)
        if kw in (PROMPT_KEY, META_KEY):
            found[kw] = _png_text_value(t, b)
    return found.get(PROMPT_KEY), found.get(META_KEY)


# ------------------------------------------------------------------------ JPEG
COM_MAX = 65533


def _jpeg_segments(data):
    """Yield (marker, start, end) for the leading marker segments after SOI,
    stopping at the first SOS (scan data follows and is not segmented)."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m = data[i + 1]
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
            i += 2
            continue
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        yield m, i, i + 2 + n
        if m == 0xDA:                                # SOS
            break
        i += 2 + n


def _com(payload):
    if len(payload) > COM_MAX:
        payload = payload[:COM_MAX - 16] + b"...[truncated]"
    return b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload


def _stamp_jpeg(p, prompt, meta_json):
    data = p.read_bytes()
    segs = list(_jpeg_segments(data))
    # drop our own earlier COM segments
    drop = [(s, e) for m, s, e in segs if m == 0xFE
            and data[s + 4:e].startswith((b"prompt:", b"render:"))]
    for s, e in reversed(drop):
        data = data[:s] + data[e:]
    segs = list(_jpeg_segments(data))
    insert_at = 2
    for m, s, e in segs:
        if 0xE0 <= m <= 0xEF:                        # after the APPn headers
            insert_at = e
        else:
            break
    # render first, prompt LAST: Pillow's info["comment"] is the last COM
    # segment, so a plain Image.open(p).info["comment"] shows the prompt.
    new = data[:insert_at] + _com(b"render:" + meta_json.encode("utf-8")) \
        + _com(b"prompt:" + prompt.encode("utf-8")) + data[insert_at:]
    _atomic_write(p, new)
    return "jpeg-COM"


def _read_jpeg(p):
    data = p.read_bytes()
    prompt = meta = None
    for m, s, e in _jpeg_segments(data):
        if m != 0xFE:
            continue
        body = data[s + 4:e]
        if body.startswith(b"prompt:"):
            prompt = body[7:].decode("utf-8", "replace")
        elif body.startswith(b"render:"):
            meta = body[7:].decode("utf-8", "replace")
    return prompt, meta


# ------------------------------------------------------------------------ WEBP
# RIFF container: `RIFF <size> WEBP` then chunks <fourcc><size LE><payload>
# padded to even length. The `XMP ` chunk is legal only in the extended
# layout: first chunk VP8X, flags byte with 0x04 set. A simple-layout file
# (first chunk `VP8 ` or `VP8L`) gets a VP8X built from the bitstream's own
# canvas size. Our XMP chunk goes last; any earlier `XMP ` is dropped.
XMP_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS_DC = "http://purl.org/dc/elements/1.1/"
XMP_NS_RENDER = "http://chippwalters.com/ns/render/1.0/"
VP8X_XMP_FLAG = 0x04


def _webp_chunks(data):
    """[(fourcc, payload)] in file order."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP")
    i, out = 12, []
    while i + 8 <= len(data):
        cc = data[i:i + 4]
        n = struct.unpack("<I", data[i + 4:i + 8])[0]
        out.append((cc, data[i + 8:i + 8 + n]))
        i += 8 + n + (n & 1)
    return out


def _webp_chunk(cc, payload):
    return cc + struct.pack("<I", len(payload)) + payload + \
        (b"\0" if len(payload) & 1 else b"")


def _webp_canvas(chunks):
    """(width, height) read from the bitstream of a simple-layout file."""
    for cc, body in chunks:
        if cc == b"VP8 ":                            # lossy key frame
            if body[3:6] != b"\x9d\x01\x2a":
                raise ValueError("VP8 start code missing")
            w = struct.unpack("<H", body[6:8])[0] & 0x3FFF
            h = struct.unpack("<H", body[8:10])[0] & 0x3FFF
            return w, h
        if cc == b"VP8L":                            # lossless
            if body[0] != 0x2F:
                raise ValueError("VP8L signature missing")
            bits = struct.unpack("<I", body[1:5])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("no VP8/VP8L chunk")


def _xmp_packet(prompt, meta_json):
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        f'<rdf:RDF xmlns:rdf="{XMP_NS_RDF}">\n'
        f'<rdf:Description rdf:about="" xmlns:dc="{XMP_NS_DC}" '
        f'xmlns:render="{XMP_NS_RENDER}">\n'
        '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        f'{xml_escape(prompt)}</rdf:li></rdf:Alt></dc:description>\n'
        f'<render:record>{xml_escape(meta_json)}</render:record>\n'
        '</rdf:Description></rdf:RDF></x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )


def _xmp_fields(xmp):
    """(prompt, meta_json) from an XMP packet; None for what is not there,
    so a packet another tool wrote still parses."""
    root = ET.fromstring(xmp.decode("utf-8", "replace").strip("\0 \t\r\n"))
    ns = {"rdf": XMP_NS_RDF, "dc": XMP_NS_DC, "render": XMP_NS_RENDER}
    prompt = meta = None
    desc = root.find(".//dc:description", ns)
    if desc is not None:
        li = desc.find("rdf:Alt/rdf:li", ns)
        text = li.text if li is not None else desc.text
        if text is not None and text.strip():
            prompt = text
    rec = root.find(".//render:record", ns)
    if rec is not None and rec.text:
        meta = rec.text
    return prompt, meta


def _stamp_webp(p, prompt, meta_json):
    data = p.read_bytes()
    chunks = [(cc, b) for cc, b in _webp_chunks(data) if cc != b"XMP "]
    if chunks and chunks[0][0] == b"VP8X":
        flags = bytearray(chunks[0][1])
        flags[0] |= VP8X_XMP_FLAG
        chunks[0] = (b"VP8X", bytes(flags))
    else:
        w, h = _webp_canvas(chunks)
        vp8x = bytes([VP8X_XMP_FLAG, 0, 0, 0]) \
            + struct.pack("<I", w - 1)[:3] + struct.pack("<I", h - 1)[:3]
        chunks.insert(0, (b"VP8X", vp8x))
    chunks.append((b"XMP ", _xmp_packet(prompt, meta_json).encode("utf-8")))
    body = b"".join(_webp_chunk(cc, b) for cc, b in chunks)
    _atomic_write(p, b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body)
    return "webp-XMP"


def _read_webp(p):
    xmp = None
    for cc, body in _webp_chunks(p.read_bytes()):
        if cc == b"XMP ":
            xmp = body                               # the last one wins
    if xmp is None:
        return None, None
    return _xmp_fields(xmp)


# ----------------------------------------------------------------------- video
def _stamp_video(p, prompt, meta_json):
    tmp = p.with_name(p.stem + ".stamping" + p.suffix)
    tmp.unlink(missing_ok=True)
    ext = p.suffix.lower()
    cmd = [FFMPEG, "-v", "error", "-y", "-i", str(p), "-map", "0", "-c", "copy",
           "-metadata", f"comment={prompt}"]
    # The render record rides as `description` where the container keeps
    # one. WAV's INFO list has no such field, and FLAC/OGG map DESCRIPTION
    # onto comment (it would overwrite the prompt) - measured 2026-09-03.
    # For those the record lives in the sidecar only, and `how` says so.
    keeps_description = ext not in (".wav", ".flac", ".ogg")
    if keeps_description:
        cmd += ["-metadata", f"description={meta_json}"]
    if ext == ".mp3":
        cmd += ["-id3v2_version", "3"]      # ID3v2.3 - what Explorer reads
    r = subprocess.run(cmd + [str(tmp)], capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 100:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg remux failed: {(r.stderr or '')[-300:]}")
    os.replace(tmp, p)
    return "media-comment" if keeps_description else \
        "media-comment (render record in the sidecar only)"


def _read_video(p):
    r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", str(p)], capture_output=True, text=True)
    try:
        fmt = json.loads(r.stdout or "{}").get("format", {})
    except ValueError:
        return None, None
    tags = fmt.get("tags", {}) or {}
    if not tags:
        # WAV keeps INFO tags on the format for some builds and on the
        # stream for others; look in both places.
        r2 = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                             "-show_streams", str(p)], capture_output=True, text=True)
        try:
            for st in json.loads(r2.stdout or "{}").get("streams", []):
                tags.update(st.get("tags", {}) or {})
        except ValueError:
            pass
    low = {k.lower(): v for k, v in tags.items()}
    return low.get("comment"), low.get("description")


# ----------------------------------------------------------------------- shared
def _atomic_write(p, data):
    tmp = p.with_name(p.stem + ".stamping" + p.suffix)
    tmp.write_bytes(data)
    os.replace(tmp, p)


def stamp(path, prompt, meta=None, keep_mtime=True, quiet=False, sidecar=True):
    """Embed the prompt in the file and write the sidecar. Returns a dict:
    {"embedded": bool, "how": str, "sidecar": str}. Never raises for a
    format it cannot embed; says so and falls back to the sidecar.
    sidecar=False: embed only, write no sidecar, "sidecar" is None. If the
    embed then fails, "embedded" is False and "how" carries the error, so
    the caller can fail loud - nothing on disk says what happened."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    prompt = (prompt or "").strip()
    meta = dict(meta or {})
    meta.setdefault("stamped", time.strftime("%Y-%m-%d %H:%M:%S"))
    meta_json = json.dumps(meta, ensure_ascii=False, default=str)
    st = p.stat()
    ext = p.suffix.lower()
    how, err = "sidecar-only", None
    try:
        if ext in IMAGE_PNG:
            how = _stamp_png(p, prompt, meta_json)
        elif ext in IMAGE_JPG:
            how = _stamp_jpeg(p, prompt, meta_json)
        elif ext in IMAGE_WEBP:
            how = _stamp_webp(p, prompt, meta_json)
        elif ext in VIDEO:
            how = _stamp_video(p, prompt, meta_json)
        else:
            err = f"no embed for {ext or 'this type'}"
    except Exception as e:                                       # noqa: BLE001
        err = str(e)
    if err:
        meta["embed"] = f"NOT embedded ({err}); this sidecar is the only copy"
    if keep_mtime:
        try:
            os.utime(p, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    if not sidecar:
        if err:
            how = f"NOT embedded ({err}); no sidecar written"
        if not quiet:
            log(f"  prompt kept: {how} (no sidecar)")
        return {"embedded": err is None, "how": how, "sidecar": None}
    sc = write_sidecar(p, prompt, meta)
    if not quiet:
        log(f"  prompt kept: {how}" + (f" ({err})" if err else "") + f" + {sc}")
    return {"embedded": err is None, "how": how, "sidecar": str(sc)}


def read(path):
    """(prompt, meta_dict_or_None). File metadata first, sidecar second."""
    p = Path(path)
    ext = p.suffix.lower()
    prompt = meta = None
    try:
        if ext in IMAGE_PNG:
            prompt, meta = _read_png(p)
        elif ext in IMAGE_JPG:
            prompt, meta = _read_jpeg(p)
        elif ext in IMAGE_WEBP:
            prompt, meta = _read_webp(p)
        elif ext in VIDEO:
            prompt, meta = _read_video(p)
    except Exception:                                            # noqa: BLE001
        prompt = meta = None
    sc_prompt, sc_tail = read_sidecar(p)
    if prompt is None:
        prompt = sc_prompt
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {"raw": meta}
    if meta is None and sc_tail:
        # WAV/FLAC/OGG (and anything sidecar-only) keep the record in the
        # sidecar; hand back its `key: value` lines as a dict.
        meta, cur = {}, None
        for ln in sc_tail.splitlines():
            if ln.startswith("  - ") and cur:
                meta.setdefault(cur, []).append(ln[4:].strip())
            elif ":" in ln:
                k, _, v = ln.partition(":")
                cur = k.strip()
                if v.strip():
                    meta[cur] = v.strip()
        meta["from"] = "sidecar"
    return prompt, meta


def open_file(path):
    """Launch in the default viewer. Chipp reads the screen, not the chat.
    PROMPT_META_NO_OPEN=1 suppresses the launch (tests, unattended runs)."""
    p = str(path)
    if os.environ.get("PROMPT_META_NO_OPEN", "").strip().lower() in ("1", "true", "yes"):
        log(f"  prompt written (not opened, PROMPT_META_NO_OPEN): {p}")
        return True
    try:
        if sys.platform == "win32":
            os.startfile(p)                                      # noqa: S606
        else:
            subprocess.Popen(["xdg-open", p])
        log(f"  prompt opened: {p}")
        return True
    except OSError as e:
        log(f"  could not open {p}: {e}")
        return False


def show(path):
    """Open the sidecar for an existing file, rebuilding it from the metadata."""
    p = Path(path)
    sc = sidecar_path(p)
    if not sc.exists():
        prompt, meta = read(p)
        if prompt is None:
            log(f"  no prompt stored for {p} (not in the file, no sidecar)")
            return False
        write_sidecar(p, prompt, meta or {})
    return open_file(sc)


def preview(path, prompt, meta=None, show_it=None):
    """Before a (re-)render: put the prompt on screen for the file about to
    exist. Fires when show_it is True, or when RENDER_SHOW_PROMPT is set and
    show_it is None. Returns the sidecar path, or None when it did not fire."""
    if show_it is None:
        show_it = show_wanted()
    if not show_it:
        return None
    meta = dict(meta or {})
    meta.setdefault("status", "RENDERING - this file is being made now")
    sc = write_sidecar(path, prompt, meta)
    open_file(sc)
    return sc


PROPOSED = "PROPOSED - awaiting approval; edit this text if you want, then approve"
EXIT_PROPOSED = 3


def propose(path, prompt, meta=None):
    """AN INTERACTIVE RE-RENDER STOPS HERE. Chipp, 2026-09-03: "display the
    prompt first and then only proceed after I approve the prompt." Writes
    the sidecar for the file about to exist, opens it, and the caller exits
    with EXIT_PROPOSED without rendering. The human may edit the text in
    the viewer; approved_prompt() picks the edit up on the next run."""
    meta = dict(meta or {})
    meta["status"] = PROPOSED
    sc = write_sidecar(path, prompt, meta)
    open_file(sc)
    log(f"PROMPT SHOWN - awaiting approval, nothing rendered: {sc}")
    return sc


def approved_prompt(path, fallback):
    """The prompt to render with once the human has approved: the sidecar's
    text (which they may have edited) when one exists, else `fallback`.
    Returns (prompt, changed)."""
    sc_prompt, _ = read_sidecar(path)
    if sc_prompt is None:
        return fallback, False
    changed = sc_prompt.strip() != (fallback or "").strip()
    if changed:
        log(f"  using the prompt as EDITED in {sidecar_path(path)} "
            f"(differs from the composed text)")
    return sc_prompt, changed


def add_approval_flags(parser):
    """The three flags every driver carries. --propose: show and stop.
    --approved: render, taking the sidecar's (possibly edited) text.
    --show-prompt: show and carry on (batch visibility, not the rework rule)."""
    parser.add_argument("--propose", action="store_true",
                        help="INTERACTIVE RE-RENDER, step 1: write the prompt "
                             "sidecar for the output, open it in the .txt "
                             f"viewer, exit {EXIT_PROPOSED}. Renders nothing.")
    parser.add_argument("--approved", action="store_true",
                        help="INTERACTIVE RE-RENDER, step 2: the human approved "
                             "the shown prompt. Render with the sidecar's text "
                             "(their edits included).")
    parser.add_argument("--show-prompt", dest="show_prompt", action="store_true",
                        help="open the prompt as a .txt and carry on rendering "
                             "(RENDER_SHOW_PROMPT=1 does the same).")
    return parser


def gate(args, out, prompt, meta):
    """One call for a driver: returns (prompt_to_use, stop). stop=True means
    the caller must exit EXIT_PROPOSED now."""
    if getattr(args, "propose", False):
        propose(out, prompt, meta)
        return prompt, True
    if getattr(args, "approved", False):
        prompt, _ = approved_prompt(out, prompt)
    preview(out, prompt, meta,
            show_it=True if getattr(args, "show_prompt", False) else None)
    return prompt, False


def finish_preview(preview_sc, final_path):
    """A driver that could only guess the output name at preview time calls
    this after stamp(): the guessed sidecar goes away if the real one differs."""
    if preview_sc is None:
        return
    try:
        if Path(preview_sc).resolve() != sidecar_path(final_path).resolve():
            Path(preview_sc).unlink(missing_ok=True)
    except OSError:
        pass


# -------------------------------------------------------------------------- cli
def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stamp", help="embed a prompt in a file + write its sidecar")
    s.add_argument("file")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt")
    g.add_argument("--prompt-file", dest="prompt_file")
    g.add_argument("--from-sidecar", action="store_true",
                   help="use the existing <stem>.prompt.txt as the prompt")
    s.add_argument("--meta", default=None, help="JSON object of render facts")
    s.add_argument("--show", action="store_true", help="open the sidecar after")
    r = sub.add_parser("read", help="print the prompt stored in a file")
    r.add_argument("file")
    r.add_argument("--meta", action="store_true", help="also print the render record")
    o = sub.add_parser("show", help="open the file's prompt in the default .txt viewer")
    o.add_argument("file")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if a.cmd == "stamp":
        if a.from_sidecar:
            prompt, _ = read_sidecar(a.file)
            if prompt is None:
                log(f"no sidecar beside {a.file}")
                return 2
        elif a.prompt_file:
            prompt = Path(a.prompt_file).read_text(encoding="utf-8")
        else:
            prompt = a.prompt
        meta = json.loads(a.meta) if a.meta else {}
        res = stamp(a.file, prompt, meta)
        if a.show:
            open_file(res["sidecar"])
        return 0 if res["embedded"] else 1
    if a.cmd == "read":
        prompt, meta = read(a.file)
        if prompt is None:
            log(f"no prompt stored for {a.file}")
            return 1
        print(prompt)
        if a.meta and meta:
            print()
            print(json.dumps(meta, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "show":
        return 0 if show(a.file) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
