#!/usr/bin/env python3
"""Build self-contained local HTML questionnaires (optionally chained as gates)
and validate the answer JSON pasted back from them. No MD DOCS needed.
`build --mobile` publishes the page through terminal-share for a phone or tablet."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt

SCHEMA_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parent.parent
QUESTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
MANIFEST_RE = re.compile(r"^\s*window\.BACKLOT\s*=\s*(\{.*\})\s*;?\s*$", re.S)
QUESTION_TYPES = {"single", "multiple", "text"}
MEDIA_TYPES = {"image", "audio", "video"}
GATE_STATES = {"answered", "accepted", "waiting", "skipped"}
# --mobile: terminal-share serves these media extensions from /q/ (keep in step with its
# src/questions.ts TYPES) and only path segments matching SERVED_SEGMENT_RE, at most 4 deep.
MOBILE_MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg",
                    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac",
                    ".mp4", ".m4v", ".webm", ".mov"}
SERVED_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,159}$")
MOBILE_ASSETS = ("page.css", "page.js")

MD = (MarkdownIt("commonmark", {"html": True, "typographer": True})
      .enable(["table", "strikethrough", "replacements", "smartquotes"]))
# Phone pages are served from the terminal-share origin: raw HTML in Markdown is escaped there.
MD_SAFE = (MarkdownIt("commonmark", {"html": False, "typographer": True})
           .enable(["table", "strikethrough", "replacements", "smartquotes"]))


class QuestionnaireError(ValueError):
    pass


# ---------------------------------------------------------------- helpers

def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QuestionnaireError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QuestionnaireError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QuestionnaireError(f"{path} must contain a JSON object")
    return value


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, value: dict) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def one_line(value: object, field: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise QuestionnaireError(f"{field} must not be empty")
    return text


def slug_text(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^A-Za-z0-9_\s-]", "", value)
    value = re.sub(r"\s+", "-", value)
    return value.strip("-") or "questions"


def safe_file_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return value or "item"


def is_remote(path: str) -> bool:
    return urlparse(path).scheme.lower() in {"http", "https", "data"}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def link_between(from_dir: Path, target: Path) -> str:
    """A link from a page/manifest folder to a file: relative when possible
    (same drive), else an absolute file:/// URL."""
    try:
        rel = os.path.relpath(target, from_dir)
    except ValueError:
        return target.as_uri()
    return quote(rel.replace("\\", "/"), safe="/._-~")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def md_block(md: MarkdownIt, text: str) -> str:
    return md.render(text) if text else ""


def md_inline(md: MarkdownIt, text: str) -> str:
    return md.renderInline(text) if text else ""


# ------------------------------------------------------------ validation

def iter_media(spec: dict):
    for question in spec.get("questions", []):
        for media in question.get("media", []):
            yield question["id"], "question", media
        for option in question.get("options", []):
            for media in option.get("media", []):
                yield question["id"], option["value"], media


def validate_media(media: object, field: str) -> dict:
    if not isinstance(media, dict):
        raise QuestionnaireError(f"{field} must be an object")
    if media.get("type") not in MEDIA_TYPES:
        raise QuestionnaireError(f"{field}.type must be image, audio, or video")
    path = str(media.get("path") or "").strip()
    if not path:
        raise QuestionnaireError(f"{field}.path must not be empty")
    media["path"] = path
    for key in ("alt", "caption"):
        if key in media:
            media[key] = one_line(media[key], f"{field}.{key}")
    return media


def validate_gate(raw: object, stage_label: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise QuestionnaireError("gate must be an object")
    manifest = str(raw.get("manifest") or "").strip()
    if not manifest:
        raise QuestionnaireError("gate.manifest must name the shared gates manifest (.js)")
    if not manifest.lower().endswith(".js"):
        raise QuestionnaireError("gate.manifest must be a .js file (file:// pages cannot fetch JSON)")
    n = raw.get("n")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 0):
        raise QuestionnaireError("gate.n must be a non-negative integer")
    return {
        "manifest": manifest,
        "label": one_line(raw.get("label") or stage_label, "gate.label"),
        "n": n,
        "register": bool(raw.get("register", True)),
        "create": bool(raw.get("create", True)),
        "title": str(raw.get("title") or "").strip(),
    }


def validate_spec(raw: dict) -> dict:
    spec = copy.deepcopy(raw)
    spec["title"] = one_line(spec.get("title"), "title")
    spec["slug"] = safe_file_part(spec.get("slug") or slug_text(spec["title"]))
    spec["intro"] = str(spec.get("intro") or "").strip()
    spec["stageLabel"] = one_line(spec.get("stageLabel") or "Questions", "stageLabel")
    spec["gate"] = validate_gate(spec.get("gate"), spec["stageLabel"])
    questions = spec.get("questions")
    if not isinstance(questions, list) or not questions:
        raise QuestionnaireError("questions must be a non-empty array")

    seen_ids: set[str] = set()
    seen_sections: set[str] = set()
    for index, question in enumerate(questions):
        field = f"questions[{index}]"
        if not isinstance(question, dict):
            raise QuestionnaireError(f"{field} must be an object")
        qid = str(question.get("id") or "").strip()
        if not QUESTION_ID_RE.fullmatch(qid):
            raise QuestionnaireError(f"{field}.id must be unique lowercase kebab-case")
        if qid in seen_ids:
            raise QuestionnaireError(f"duplicate question id: {qid}")
        seen_ids.add(qid)
        qtype = question.get("type")
        if qtype not in QUESTION_TYPES:
            raise QuestionnaireError(f"{field}.type must be single, multiple, or text")
        prompt = one_line(question.get("prompt"), f"{field}.prompt")
        question["id"] = qid
        question["prompt"] = prompt
        question["required"] = bool(question.get("required", False))
        question["allowNote"] = bool(question.get("allowNote", True))
        question["help"] = str(question.get("help") or "").strip()
        question["body"] = str(question.get("body") or "").strip()
        placeholder = question.get("placeholder") or question.get("notePlaceholder") or "Add your answer"
        question["placeholder"] = one_line(placeholder, f"{field}.placeholder")
        section = slug_text(f"{prompt} · {qid}")
        if section in seen_sections:
            raise QuestionnaireError(f"question headings produce duplicate section id: {section}")
        seen_sections.add(section)
        question["section"] = section

        media_items = question.get("media", [])
        if not isinstance(media_items, list):
            raise QuestionnaireError(f"{field}.media must be an array")
        question["media"] = [validate_media(m, f"{field}.media[{i}]") for i, m in enumerate(media_items)]

        if qtype == "text":
            if question.get("options"):
                raise QuestionnaireError(f"{field}.options is not valid for a text question")
            default = question.get("default", "")
            if not isinstance(default, str):
                raise QuestionnaireError(f"{field}.default must be a string")
            question["default"] = default.strip()
            question["options"] = []
            question["allowNote"] = False
            continue
        if "default" in question:
            raise QuestionnaireError(f"{field}.default is only valid for a text question")

        options = question.get("options")
        if not isinstance(options, list) or not options:
            raise QuestionnaireError(f"{field}.options must be a non-empty array")
        seen_values: set[str] = set()
        selected = 0
        for option_index, option in enumerate(options):
            option_field = f"{field}.options[{option_index}]"
            if not isinstance(option, dict):
                raise QuestionnaireError(f"{option_field} must be an object")
            value = str(option.get("value") or "").strip()
            if not VALUE_RE.fullmatch(value):
                raise QuestionnaireError(f"{option_field}.value has unsupported characters")
            if value in seen_values:
                raise QuestionnaireError(f"duplicate option value {value} in {qid}")
            seen_values.add(value)
            option["value"] = value
            option["label"] = one_line(option.get("label") or value, f"{option_field}.label")
            option["description"] = str(option.get("description") or "").strip()
            option["recommended"] = bool(option.get("recommended", False))
            option["selected"] = bool(option.get("selected", False))
            selected += int(option["selected"])
            option_media = option.get("media", [])
            if not isinstance(option_media, list):
                raise QuestionnaireError(f"{option_field}.media must be an array")
            option["media"] = [validate_media(m, f"{option_field}.media[{i}]")
                               for i, m in enumerate(option_media)]
        if qtype == "single" and selected > 1:
            raise QuestionnaireError(f"single question {qid} has more than one selected option")
    if "notes" not in seen_ids:
        spec["questions"].append({
            "id": "notes", "type": "text", "prompt": "Notes", "required": False,
            "allowNote": False, "help": "", "body": "", "default": "",
            "placeholder": "Anything else for this gate?", "options": [], "media": [],
            "section": slug_text("Notes · notes"),
        })
    return spec


def resolve_media(spec: dict, spec_dir: Path) -> list[dict]:
    """Point every local media item at its ORIGINAL file (never copied) and
    record a content digest so a changed file changes the spec digest."""
    evidence = []
    for qid, owner, media in iter_media(spec):
        source = media["path"]
        if is_remote(source):
            media["src"] = source
            evidence.append({"question": qid, "owner": owner, "path": source, "digest": None})
            continue
        path = Path(os.path.expandvars(os.path.expanduser(source)))
        if not path.is_absolute():
            path = spec_dir / path
        path = path.resolve()
        if not path.is_file():
            raise QuestionnaireError(f"media file does not exist: {path}")
        media["src"] = path.as_uri()
        media["file"] = str(path)
        evidence.append({"question": qid, "owner": owner, "path": str(path), "digest": file_digest(path)})
    return evidence


# ---------------------------------------------------------- gate manifest

def read_manifest(path: Path) -> dict | None:
    """Parse a `window.BACKLOT = {...};` manifest. None when it carries more
    than the assignment (owned by another tool, e.g. VidWiz)."""
    match = MANIFEST_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        manifest = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("stages", []), list):
        return None
    manifest.setdefault("stages", [])
    return manifest


def write_manifest(path: Path, manifest: dict) -> None:
    body = json.dumps(manifest, indent=2, ensure_ascii=True)
    write_text_atomic(path, f"window.BACKLOT = {body};\n")


def owned_manifest(path: Path) -> dict:
    manifest = read_manifest(path)
    if manifest is None:
        raise QuestionnaireError(
            f"{path} is not a plain `window.BACKLOT = {{...}};` manifest; another tool owns it. "
            "Set \"register\": false to read it without writing.")
    return manifest


def register_gate(gate: dict, manifest_path: Path, page_path: Path, title: str) -> dict:
    """Add or update this page's gate (state waiting) in the shared manifest."""
    if manifest_path.exists():
        manifest = owned_manifest(manifest_path)
    elif gate["create"]:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"title": gate["title"] or title, "stages": []}
    else:
        raise QuestionnaireError(f"gate manifest does not exist: {manifest_path}")
    stages = manifest["stages"]
    href = link_between(manifest_path.parent, page_path)
    if gate["n"] is not None:
        match = next((s for s in stages if s.get("n") == gate["n"]), None)
    else:
        label = gate["label"].lower()
        match = next((s for s in stages if str(s.get("label", "")).lower() == label), None)
    if match is None:
        numbers = [s["n"] for s in stages if isinstance(s.get("n"), int)]
        match = {"n": gate["n"] if gate["n"] is not None else (max(numbers, default=0) + 1)}
        stages.append(match)
    match.update({"label": gate["label"], "href": href, "state": "waiting"})
    stages.sort(key=lambda s: s.get("n") if isinstance(s.get("n"), int) else 10 ** 9)
    write_manifest(manifest_path, manifest)
    return match


def set_gate_state(bundle_path: Path, bundle: dict, state: str, only_from: set | None = None) -> str | None:
    """Set this page's gate state in the shared manifest. None when the page is not a registered gate, or a newer
    page of the same gate has taken its place. With only_from, a state outside that set is left alone."""
    if state not in GATE_STATES:
        raise QuestionnaireError(f"state must be one of {sorted(GATE_STATES)}")
    gate = bundle.get("gate")
    if not gate or not gate.get("register"):
        return None
    manifest_path = Path(gate["manifestPath"])
    if not manifest_path.is_file():
        return None
    manifest = owned_manifest(manifest_path)
    href = link_between(manifest_path.parent, bundle_path.parent / bundle["html"])
    for stage in manifest["stages"]:
        if stage.get("href") == href:
            if only_from is None or stage.get("state") in only_from:
                stage["state"] = state
                write_manifest(manifest_path, manifest)
            return str(stage.get("state"))
    return None


def mark_gate_answered(bundle_path: Path, bundle: dict) -> str | None:
    return set_gate_state(bundle_path, bundle, "answered", only_from={None, "waiting"})


# ------------------------------------------------------------- rendering

def media_html(media: dict, md: MarkdownIt) -> str:
    src = esc(media["src"])
    alt = esc(media.get("alt") or media["type"])
    if media["type"] == "image":
        out = f'<img src="{src}" alt="{alt}" loading="lazy">'
    elif media["type"] == "audio":
        out = f'<audio controls preload="metadata" src="{src}"></audio>'
    else:
        out = f'<video controls preload="metadata" src="{src}"></video>'
    if media.get("caption"):
        out += f'<p class="hq-caption"><em>{md_inline(md, media["caption"])}</em></p>'
    return out


def option_html(question: dict, option: dict, md: MarkdownIt) -> str:
    kind, cls = ("radio", "bl-radio") if question["type"] == "single" else ("checkbox", "bl-check")
    checked = " checked" if option["selected"] else ""
    label = f"<strong>{md_inline(md, option['label'])}</strong>"
    suffix = []
    if option["description"]:
        suffix.append(md_inline(md, option["description"]))
    if option["recommended"]:
        suffix.append("<strong>Recommended</strong>")
    text = f"<code>{esc(option['value'])}</code> — {label}"
    if suffix:
        text += " — " + " · ".join(suffix)
    media = "".join(media_html(m, md) for m in option["media"])
    return (f'<li class="bl-item {cls}"><input type="{kind}" class="bl-input" '
            f'name="hq-{esc(question["section"])}" value="{esc(option["value"])}"{checked}> '
            f"{text}{media}</li>")


def note_html(label: str, placeholder: str, value: str = "") -> str:
    rows = 3 if not value else min(24, max(3, sum(1 + len(line) // 90 for line in value.split("\n"))))
    return (f'<label class="bl-note"><span>{esc(label)}</span>'
            f'<textarea class="bl-input" rows="{rows}" placeholder="{esc(placeholder)}">{esc(value)}</textarea></label>')


def question_html(question: dict, md: MarkdownIt) -> str:
    out = [f'<section class="hq-question" data-section="{esc(question["section"])}" '
           f'data-required="{1 if question["required"] else 0}">',
           f'<h2 id="{esc(question["section"])}">{md_inline(md, question["prompt"])} · '
           f'<code>{esc(question["id"])}</code></h2>']
    if question["required"]:
        out.append("<p><strong>Required.</strong></p>")
    out.append(md_block(md, question["help"]))
    out.append(md_block(md, question["body"]))
    out.extend(media_html(m, md) for m in question["media"])
    if question["type"] == "text":
        out.append(note_html("Answer", question["placeholder"], question["default"]))
    else:
        out.append("<ul>" + "".join(option_html(question, o, md) for o in question["options"]) + "</ul>")
        if question["allowNote"]:
            out.append(note_html("Note", question["placeholder"]))
    out.append("</section>")
    return "\n".join(part for part in out if part)


def toc_html(spec: dict) -> str:
    items = "".join(
        f'<li><a href="#{esc(q["section"])}">{esc(q["prompt"])}</a>'
        + ('<span class="toc-required" title="Required">*</span>' if q["required"] else "")
        + "</li>"
        for q in spec["questions"])
    return (f'<li class="toc-section toc-h1"><div class="toc-section-header">'
            f'<span class="toc-toggle">&#9660;</span><a href="#top">{esc(spec["title"])}</a></div>'
            f'<ul class="toc-children">{items}</ul></li>')


def script_json(value: object) -> str:
    """JSON safe to embed in a <script> element or a script file (ASCII, so encoding-proof)."""
    return (json.dumps(value, ensure_ascii=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def build_html(spec: dict, config: dict, config_script: str | None = None) -> str:
    """The page. With config_script (--mobile) the style, script and config are linked files,
    because terminal-share's page CSP allows no inline script or style; otherwise all inline."""
    md = MD_SAFE if config_script else MD
    if config_script:
        style = '<link rel="stylesheet" href="page.css">'
        scripts = f'<script src="{esc(config_script)}"></script>\n  <script src="page.js"></script>'
        instructions = ("<blockquote><p>Complete every item marked <strong>Required</strong>, then tap "
                        "<strong>Copy answers</strong> at the top. Go back to terminal-share, paste the JSON "
                        "into the message box and send it.</p></blockquote>")
    else:
        css = (SKILL_DIR / "assets" / "page.css").read_text(encoding="utf-8")
        js = (SKILL_DIR / "assets" / "page.js").read_text(encoding="utf-8")
        style = f"<style>\n{css}\n  </style>"
        scripts = f"<script>window.HQ = {script_json(config)};</script>\n  <script>\n{js}\n  </script>"
        instructions = ("<blockquote><p>Complete every item marked <strong>Required</strong>, then click "
                        "<strong>Copy answers</strong> in the header. Paste the JSON into chat. "
                        "Do not save a file.</p></blockquote>")
    intro = md_block(md, spec["intro"])
    body = "\n".join(question_html(q, md) for q in spec["questions"])
    title = esc(spec["title"])
    rail_row = ""
    if config["gate"]:
        rail_row = ('\n    <div class="header-inner bl-rail-row" id="blRailRow" hidden>'
                    '<nav class="bl-rail" id="blRail" aria-label="Gates"></nav></div>')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  {style}
</head>
<body data-theme="dark">
  <header class="site-header">
    <div class="header-inner">
      <div class="brand">{title}</div>
      <div class="spacer"></div>
      <span class="bl-status" id="hqStatus"></span>
      <button class="btn bl-save" id="hqCopy" type="button" title="Copy answers JSON to the clipboard">Copy answers</button>
      <button class="btn" id="themeToggle" type="button">Toggle Theme</button>
    </div>{rail_row}
  </header>
  <main class="layout">
    <nav class="toc">
      <div class="toc-header">
        <h3>Contents</h3>
        <div class="toc-controls">
          <button id="tocExpandAll" type="button" title="Expand all">+</button>
          <button id="tocCollapseAll" type="button" title="Collapse all">&minus;</button>
        </div>
      </div>
      <ul id="tocList">{toc_html(spec)}</ul>
    </nav>
    <section class="card">
      <article class="doc">
        <h1 id="top">{title}</h1>
        {intro}
        {instructions}
        {body}
      </article>
    </section>
  </main>
  {scripts}
</body>
</html>
"""


# ------------------------------------------------------------------ build

def phone_target() -> tuple[Path, str]:
    """terminal-share's published-questions folder (served at /q/) and the https origin the phone uses."""
    home = os.environ.get("TERMINAL_SHARE_HOME")
    runtime = Path(home) if home else Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "terminal-share"
    config_path = runtime / "config.json"
    if not config_path.is_file():
        raise QuestionnaireError(f"--mobile publishes through terminal-share, but {config_path} does not exist")
    origin = str(read_json(config_path).get("publicOrigin") or "").rstrip("/")
    if not origin.startswith("https://"):
        raise QuestionnaireError(f"terminal-share has no https publicOrigin in {config_path}; run its scripts\\tailscale-setup.ps1")
    return (runtime / "questions").resolve(), origin


def served_parts(root: Path, path: Path) -> list[str]:
    """URL path segments of a published file, checked against what terminal-share serves from /q/."""
    try:
        parts = list(path.relative_to(root).parts)
    except ValueError:
        raise QuestionnaireError(f"{path} is outside terminal-share's questions folder {root}") from None
    if not 1 <= len(parts) <= 4 or not all(SERVED_SEGMENT_RE.fullmatch(p) for p in parts):
        raise QuestionnaireError(f"terminal-share cannot serve {path}: at most 4 levels below {root}, "
                                 "each a plain name (letters, digits, . _ -)")
    return parts


def publish_media(spec: dict, out_dir: Path, stem: str, root: Path) -> None:
    """--mobile: copy local media into <stem>_media/ so terminal-share can serve it, and link the copies."""
    media_dir = out_dir / f"{stem}_media"
    for index, (_, _, media) in enumerate(iter_media(spec), 1):
        if "file" not in media:
            if not media["src"].lower().startswith(("https:", "data:")):
                raise QuestionnaireError(f"the phone page is HTTPS, so it cannot load {media['src']}; "
                                         "use a local file or an https URL")
            continue
        source = Path(media["file"])
        if source.suffix.lower() not in MOBILE_MEDIA_EXT:
            raise QuestionnaireError(f"terminal-share does not serve {source.suffix or 'extensionless'} files: {source}")
        target = media_dir / f"{index:02d}-{safe_file_part(source.stem)[:60]}{source.suffix.lower()}"
        served_parts(root, target)
        media_dir.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        media["src"] = link_between(out_dir, target)


def build_bundle(spec_path: Path, out_dir: Path | None, mobile: bool = False) -> dict:
    raw = read_json(spec_path)
    spec = validate_spec(raw)
    evidence = resolve_media(spec, spec_path.parent.resolve())
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    evidence_json = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256((canonical + "\n" + evidence_json).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    stem = f"{spec['slug']}-q-{digest[:8]}-{run_id}"
    root, origin = phone_target() if mobile else (None, None)
    if root:
        # A new folder per questionnaire unless --out names one (a gate chain reuses its folder).
        if out_dir is None:
            out_dir = root / f"{spec['slug'][:60]}-{secrets.token_hex(4)}"
        elif not out_dir.is_absolute():
            out_dir = root / out_dir
        out_dir = out_dir.resolve()
    elif out_dir is None:
        raise QuestionnaireError("--out is required (only --mobile chooses its own folder)")
    page_parts = served_parts(root, out_dir / f"{stem}.html") if root else None
    out_dir.mkdir(parents=True, exist_ok=True)

    names = {
        "html": f"{stem}.html",
        "answers": f"{stem}.answers.json",
        "submitted": f"{stem}.answers.js",
        "responses": f"{stem}.responses.json",
        "bundle": f"{stem}.bundle.json",
    }
    page_path = out_dir / names["html"]
    if root:
        names["config"] = f"{stem}.config.js"
        publish_media(spec, out_dir, stem, root)

    gate = spec["gate"]
    gate_info = None
    page_gate = None
    if gate:
        manifest_path = Path(os.path.expandvars(os.path.expanduser(gate["manifest"])))
        if not manifest_path.is_absolute():
            manifest_path = out_dir / manifest_path
        manifest_path = manifest_path.resolve()
        if root:
            served_parts(root, manifest_path)
        n = gate["n"]
        if gate["register"]:
            n = register_gate(gate, manifest_path, page_path, spec["title"])["n"]
        elif not manifest_path.is_file():
            raise QuestionnaireError(f"gate manifest does not exist: {manifest_path}")
        page_gate = {"manifest": link_between(out_dir, manifest_path), "n": n, "label": gate["label"]}
        gate_info = {"manifestPath": str(manifest_path), "n": n, "label": gate["label"],
                     "register": gate["register"]}

    config = {"page": names["html"], "gate": page_gate, "submitted": names["submitted"]}
    if root:
        for name in MOBILE_ASSETS:
            shutil.copyfile(SKILL_DIR / "assets" / name, out_dir / name)
        write_text_atomic(out_dir / names["config"], f"window.HQ = {script_json(config)};\n")
        write_text_atomic(page_path, build_html(spec, config, names["config"]))
        url = origin + "/q/" + "/".join(quote(part) for part in page_parts)
    else:
        write_text_atomic(page_path, build_html(spec, config))
        url = page_path.resolve().as_uri()

    bundle = {
        "schemaVersion": SCHEMA_VERSION,
        "reviewId": run_id,
        "specDigest": digest,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "title": spec["title"],
        "stageLabel": spec["stageLabel"],
        "sourceSpec": str(spec_path.resolve()),
        "directory": str(out_dir.resolve()),
        "mobile": mobile,
        "url": url,
        "html": names["html"],
        "answers": names["answers"],
        "submitted": names["submitted"],
        "responses": names["responses"],
        "gate": gate_info,
        "questions": [{
            "id": q["id"], "type": q["type"], "prompt": q["prompt"], "section": q["section"],
            "required": q["required"], "allowNote": q["allowNote"],
            "options": [{"value": o["value"], "label": o["label"]} for o in q["options"]],
        } for q in spec["questions"]],
    }
    write_json(out_dir / names["bundle"], bundle)
    return {
        "bundle": str((out_dir / names["bundle"]).resolve()),
        "html": str(page_path.resolve()),
        "url": url,
        "directory": str(out_dir.resolve()),
        "expectedAnswers": str((out_dir / names["answers"]).resolve()),
        "gateManifest": gate_info["manifestPath"] if gate_info else None,
        "reviewId": run_id,
        "specDigest": digest,
    }


# ------------------------------------------------------------------- read

def parse_timestamp(value: object, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise QuestionnaireError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuestionnaireError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def locate_answers(bundle_path: Path, bundle: dict, explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit.resolve() if explicit.is_file() else None
    name = bundle["answers"]
    beside = bundle_path.parent / name
    if beside.is_file():
        return beside.resolve()
    downloaded = Path.home() / "Downloads" / name
    return downloaded.resolve() if downloaded.is_file() else None


def normalize_answers(bundle_path: Path, answers_path: Path, output_path: Path | None = None) -> dict:
    bundle = read_json(bundle_path)
    if bundle.get("schemaVersion") != SCHEMA_VERSION:
        raise QuestionnaireError("unsupported bundle schema version")
    payload = read_json(answers_path)
    if payload.get("page") != bundle.get("html"):
        raise QuestionnaireError(f"answer page {payload.get('page')!r} does not match {bundle.get('html')!r}")
    generated_at = parse_timestamp(bundle.get("generatedAt"), "bundle.generatedAt")
    saved_at = parse_timestamp(payload.get("savedAt"), "answers.savedAt")
    if saved_at < generated_at:
        raise QuestionnaireError("answers were saved before this questionnaire was generated")
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise QuestionnaireError("answers.answers must be an array")

    questions = {q["section"]: q for q in bundle.get("questions", [])}
    by_section: dict[str, dict[str, dict]] = {}
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            raise QuestionnaireError(f"answers[{index}] must be an object")
        section, kind = answer.get("section"), answer.get("kind")
        if section not in questions:
            raise QuestionnaireError(f"unknown answer section: {section!r}")
        qtype = questions[section]["type"]
        allowed_kinds = {"note"} if qtype == "text" else {"pick" if qtype == "single" else "check"}
        if questions[section].get("allowNote"):
            allowed_kinds.add("note")
        if kind not in allowed_kinds:
            raise QuestionnaireError(f"answer kind {kind!r} is invalid for {section}")
        section_answers = by_section.setdefault(section, {})
        if kind in section_answers:
            raise QuestionnaireError(f"duplicate {kind} answer for {section}")
        section_answers[kind] = answer

    responses = {}
    for question in bundle.get("questions", []):
        section_answers = by_section.get(question["section"], {})
        allowed = {o["value"]: o["label"] for o in question.get("options", [])}
        qtype = question["type"]
        note = str(section_answers["note"].get("value") or "").strip() if "note" in section_answers else ""
        if qtype == "text":
            if question["required"] and not note:
                raise QuestionnaireError(f"required question is unanswered: {question['id']}")
            responses[question["id"]] = {"type": qtype, "value": note}
            continue
        if qtype == "single":
            answer = section_answers.get("pick")
            value = str(answer.get("value") or "").strip() if answer else ""
            if value and value not in allowed:
                raise QuestionnaireError(f"invalid value {value!r} for {question['id']}")
            if question["required"] and not value:
                raise QuestionnaireError(f"required question is unanswered: {question['id']}")
            response = {"type": qtype, "value": value or None, "label": allowed.get(value)}
        else:
            answer = section_answers.get("check")
            raw_values = answer.get("value", []) if answer else []
            if not isinstance(raw_values, list):
                raise QuestionnaireError(f"multiple answer for {question['id']} must be an array")
            values = [str(v) for v in raw_values]
            invalid = [v for v in values if v not in allowed]
            if invalid:
                raise QuestionnaireError(f"invalid values for {question['id']}: {invalid}")
            if len(values) != len(set(values)):
                raise QuestionnaireError(f"duplicate values for {question['id']}")
            if question["required"] and not values:
                raise QuestionnaireError(f"required question is unanswered: {question['id']}")
            response = {"type": qtype, "value": values, "labels": [allowed[v] for v in values]}
        if question.get("allowNote"):
            response["note"] = note
        responses[question["id"]] = response

    normalized = {
        "schemaVersion": SCHEMA_VERSION,
        "reviewId": bundle["reviewId"],
        "specDigest": bundle["specDigest"],
        "savedAt": payload["savedAt"],
        "sourceAnswers": str(answers_path.resolve()),
        "responses": responses,
    }
    destination = output_path or (bundle_path.parent / bundle["responses"])
    write_json(destination, normalized)

    # The page reloads these as the submitted answers when reopened.
    submitted = {"savedAt": payload["savedAt"], "answers": answers}
    write_text_atomic(bundle_path.parent / bundle["submitted"],
                      f"window.HQ_SUBMITTED = {script_json(submitted)};\n")
    gate_state = mark_gate_answered(bundle_path, bundle)
    return {"responsesFile": str(destination.resolve()), "gateState": gate_state, **normalized}


# ------------------------------------------------------------------ clean

def clean(root: Path, older_than: float | None, apply: bool) -> dict:
    """Delete generated questionnaires (html-questions or md-questions) under
    root: every file named <stem>.*, <stem>_images/ or <stem>_media/, the spec
    when it sits in the same folder and no kept questionnaire uses it, the
    shared page.css/page.js of a --mobile folder once no questionnaire is left
    in it, then folders this left empty. Gate manifests are never touched."""
    if not root.is_dir():
        raise QuestionnaireError(f"not a folder: {root}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than) if older_than is not None else None
    doomed, kept_specs, kept = [], set(), 0
    for bundle_path in sorted(root.rglob("*.bundle.json")):
        try:
            bundle = read_json(bundle_path)
            generated = parse_timestamp(bundle.get("generatedAt"), "generatedAt")
        except QuestionnaireError:
            continue
        if "html" not in bundle or "answers" not in bundle:
            continue
        spec = Path(bundle["sourceSpec"]).resolve() if bundle.get("sourceSpec") else None
        if cutoff and generated > cutoff:
            kept += 1
            if spec:
                kept_specs.add(spec)
            continue
        doomed.append((bundle_path, spec, bool(bundle.get("mobile"))))

    removed: list[str] = []
    touched: set[Path] = set()
    doomed_bundles = {bundle_path for bundle_path, _, _ in doomed}
    mobile_folders: set[Path] = set()
    for bundle_path, spec, mobile in doomed:
        stem = bundle_path.name[: -len(".bundle.json")]
        if mobile:
            mobile_folders.add(bundle_path.parent)
        items = [p for p in bundle_path.parent.iterdir()
                 if p.name.startswith(stem + ".") or p.name in (f"{stem}_images", f"{stem}_media")]
        if spec and spec.parent == bundle_path.parent.resolve() and spec not in kept_specs and spec.is_file():
            items.append(spec)
        for item in items:
            if str(item) in removed:
                continue
            removed.append(str(item))
            touched.add(item.parent)
            if apply:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
    for folder in sorted(mobile_folders):
        if any(b not in doomed_bundles for b in folder.glob("*.bundle.json")):
            continue
        for asset in (folder / name for name in MOBILE_ASSETS):
            if asset.is_file():
                removed.append(str(asset))
                if apply:
                    asset.unlink()
    if apply:
        root = root.resolve()
        for folder in sorted(touched, key=lambda p: len(p.parts), reverse=True):
            folder = folder.resolve()
            while folder != root and root in folder.parents and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
    return {"root": str(root), "applied": apply, "removed": removed, "keptNewer": kept}


# -------------------------------------------------------------------- cli

def command_build(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else None
    if out is not None and not args.mobile:
        out = out.resolve()  # --mobile resolves a relative --out against terminal-share's questions folder
    result = build_bundle(Path(args.spec).resolve(), out, args.mobile)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_read(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).resolve()
    bundle = read_json(bundle_path)
    explicit = Path(args.answers).resolve() if args.answers else None
    answers_path = locate_answers(bundle_path, bundle, explicit)
    if answers_path is None:
        raise QuestionnaireError(f"answers not found: {bundle.get('answers')}")
    output = Path(args.out).resolve() if args.out else None
    print(json.dumps(normalize_answers(bundle_path, answers_path, output), indent=2, ensure_ascii=False))
    return 0


def command_clean(args: argparse.Namespace) -> int:
    if args.mobile:
        root = phone_target()[0]
    elif args.root:
        root = Path(args.root).resolve()
    else:
        raise QuestionnaireError("clean needs --root or --mobile")
    print(json.dumps(clean(root, args.older_than, args.yes), indent=2))
    return 0


def command_mark(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).resolve()
    bundle = read_json(bundle_path)
    if not (bundle.get("gate") or {}).get("register"):
        raise QuestionnaireError("this page is not a registered gate")
    state = set_gate_state(bundle_path, bundle, args.state)
    if state is None:
        raise QuestionnaireError("the manifest no longer lists this page (a newer page of the gate replaced it)")
    print(json.dumps({"gateManifest": bundle["gate"]["manifestPath"], "state": state}))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a self-contained HTML questionnaire from a JSON spec")
    build.add_argument("--spec", required=True)
    build.add_argument("--out", help="output folder (required unless --mobile)")
    build.add_argument("--mobile", action="store_true",
                       help="publish through terminal-share for a phone or tablet; prints an https url")
    build.set_defaults(func=command_build)

    read = sub.add_parser("read", help="validate and normalize a pasted answer file")
    read.add_argument("--bundle", required=True)
    read.add_argument("--answers")
    read.add_argument("--out")
    read.set_defaults(func=command_read)

    cl = sub.add_parser("clean", help="delete generated questionnaires under a folder (dry run without --yes)")
    cl.add_argument("--root")
    cl.add_argument("--mobile", action="store_true", help="clean terminal-share's published questions folder")
    cl.add_argument("--older-than", type=float, metavar="DAYS")
    cl.add_argument("--yes", action="store_true", help="actually delete")
    cl.set_defaults(func=command_clean)

    mk = sub.add_parser("mark", help="set this page's gate state in the shared manifest")
    mk.add_argument("--bundle", required=True)
    mk.add_argument("--state", required=True, choices=sorted(GATE_STATES))
    mk.set_defaults(func=command_mark)
    return ap


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except QuestionnaireError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
