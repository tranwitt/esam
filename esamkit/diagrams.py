"""
Picture diagrams: from a picture to text that can be searched.

A block diagram, a state machine or a wiring sketch carries facts that are
nowhere in the text around it ("the brake ECU sends EBC1 to the cluster").
Search works on text, so every picture in an ingested document is turned
into a figure block:

    > **Picture 2** (`image2.png`). Caption: Figure 3 — CAN topology.
    > Diagram description (gpt-5.1): A block diagram with four ECUs on one bus ...

Where the words come from, in order of preference (ESAM_DIAGRAMS, or
`documents.diagrams` in a profile):

  auto   a vision model describes the picture, if one is configured
         (ESAM_VISION_MODEL, or the profile's llm.roles.vision). Without
         one, the local sources below are used.
  local  nothing leaves the machine: the text labels in an SVG, OCR of a
         raster picture when the `tesseract` program is installed, and the
         text typed inside Word shapes, SmartArt and charts.
  off    only the picture's name, caption and alt text.

The author's caption and alt text are always kept. A description is the
model's reading of the picture, so the block says which model wrote it and
the intake review lists every picture for the SE to check.

Descriptions are cached by picture content and model (work/cache/diagrams/),
so the same diagram is described once: re-reading a document costs nothing
and gives the same text again.
"""

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from . import llm, office
from .config import Settings, settings as default_settings
from .docmodel import Picture

PROMPT_VERSION = "1"
SYSTEM = ("You read engineering diagrams for a systems-engineering document archive. Your description is stored "
          "as text and searched later, so it must state what the picture shows and nothing else.")
PROMPT = (
    "Describe this picture from a design document so that an engineer can find it by searching.\n"
    "- Say what kind of picture it is (block diagram, state machine, sequence diagram, wiring or bus topology, "
    "flowchart, timing diagram, table, plot, photo, ...).\n"
    "- Name every labelled element exactly as written (component names, identifiers such as SUB-BRK-ECU or "
    "PGN 61441, signal and message names).\n"
    "- State every connection: from which element to which, in which direction, and its label "
    "(message, rate, voltage, protocol).\n"
    "- Copy numbers, units and identifiers verbatim. If something is unreadable, say so; do not guess.\n"
    "- Do not interpret, judge or add anything that is not in the picture.\n"
    "Answer in plain sentences in one paragraph: no headings, no lists, no markdown.")
SVG_PROMPT = PROMPT + "\n\nThe picture is given as SVG source:\n\n"

RASTER = {"image/png", "image/jpeg", "image/gif", "image/webp"}       # what vision endpoints accept as they are
MAX_SIDE, MAX_BYTES, MAX_SVG_CHARS = 2048, 15 * 1024 * 1024, 120_000
_SNIFF = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
          (b"BM", "image/bmp"), (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff"))


def sniff(data: bytes, fallback: str = "") -> str:
    """The picture's type from its first bytes (a file extension can lie)."""
    for magic, mime in _SNIFF:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2000].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head) or b"<svg" in head[:600]:
        return "image/svg+xml"
    if data[:4] == b"\x01\x00\x00\x00" and data[40:44] == b" EMF":
        return "image/x-emf"
    if data[:4] == b"\xd7\xcd\xc6\x9a":
        return "image/x-wmf"
    return fallback


def svg_text(data: bytes) -> list[str]:
    """The text an SVG shows: <text>/<tspan> labels, and its <title> and <desc>."""
    if b"<!ENTITY" in data[:8192]:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out: list[str] = []
    for el in root.iter():
        name = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
        if name in ("text", "title", "desc"):
            text = " ".join("".join(el.itertext()).split())
            if text and text not in out:
                out.append(text)
    return out


def ocr(data: bytes, mime: str) -> str | None:
    """Text read off a raster picture by tesseract; None if it is not
    installed or finds nothing."""
    exe = shutil.which("tesseract")
    if exe is None or mime not in (RASTER | {"image/bmp", "image/tiff"}):
        return None
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
           "image/bmp": ".bmp", "image/tiff": ".tif"}[mime]
    with tempfile.TemporaryDirectory(prefix="esam-ocr-") as tmp:
        src = Path(tmp) / f"picture{ext}"
        src.write_bytes(data)
        try:
            run = subprocess.run([exe, str(src), "stdout", "--psm", "11"], capture_output=True, timeout=90, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
    words = " ".join(run.stdout.decode("utf-8", "replace").split())
    return words if len(re.sub(r"[^A-Za-z0-9]", "", words)) >= 3 else None


def _for_vision(data: bytes, mime: str, s: Settings) -> tuple[bytes, str] | None:
    """The picture in a form a vision endpoint accepts (PNG/JPEG/GIF/WebP, not
    huge), or None if it cannot be made so on this machine."""
    if mime in ("image/x-emf", "image/x-wmf"):
        try:
            data, mime = office.convert(data, ".emf" if mime.endswith("emf") else ".wmf", "png", s), "image/png"
        except RuntimeError:
            return None
    try:
        from PIL import Image
    except ImportError:
        return (data, mime) if mime in RASTER and len(data) <= MAX_BYTES else None
    try:
        with Image.open(io.BytesIO(data)) as img:
            if mime in RASTER and max(img.size) <= MAX_SIDE and len(data) <= MAX_BYTES:
                return data, mime
            img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue(), "image/png"
    except Exception:
        return (data, mime) if mime in RASTER and len(data) <= MAX_BYTES else None


def _one_line(text: str) -> str:
    """Text as one plain paragraph: inside a document it may not start a
    heading, a list item or a table. Identifiers (wheel_speed, SUB-BRK-ECU)
    are left exactly as they are."""
    lines = [re.sub(r"^\s*(?:[#>]+|[-*+]|\d+[.)])\s+", "", ln) for ln in text.splitlines()]
    text = " ".join(lines).replace("**", "").replace("`", "").replace("|", "/")
    return " ".join(text.split())


def _cache_file(data: bytes, model: str, s: Settings) -> Path:
    key = hashlib.sha256(data + b"\0" + model.encode() + b"\0" + PROMPT_VERSION.encode()).hexdigest()[:40]
    return s.diagram_cache_dir / f"{key}.json"


def describe(data: bytes, mime: str, s: Settings = default_settings) -> dict:
    """Ask the vision model. Returns {description, model, cached} or
    {error}. Never raises: a picture that cannot be described is still
    ingested with whatever else is known about it."""
    routes = llm.vision_routes(s)
    if not routes:
        return {"error": "no vision model is configured"}
    model = routes[0].model
    cache = _cache_file(data, model, s)
    if cache.exists():
        try:
            return {**json.loads(cache.read_text()), "cached": True}
        except (ValueError, OSError):
            pass
    try:
        if mime == "image/svg+xml":
            source = data.decode("utf-8", "replace")
            if len(source) > MAX_SVG_CHARS:
                return {"error": f"the SVG is larger than {MAX_SVG_CHARS} characters"}
            text = llm.complete(SYSTEM, SVG_PROMPT + source, routes=routes, s=s)
        else:
            ready = _for_vision(data, mime, s)
            if ready is None:
                return {"error": f"a {mime or 'picture of unknown type'} cannot be sent to the model from this machine"}
            text = llm.complete(SYSTEM, PROMPT, images=[(ready[1], ready[0])], routes=routes, s=s)
    except Exception as e:
        return {"error": f"the vision model did not answer ({type(e).__name__}: {str(e)[:200]})"}
    out = {"description": _one_line(text), "model": model,
           "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, indent=1))
    except OSError:
        pass
    return {**out, "cached": False}


def figure_block(pic: Picture, n: int, s: Settings = default_settings, mode: str | None = None) -> tuple[str, dict]:
    """(the markdown block that stands for the picture, a record of how it was read)."""
    mode = (mode or s.diagrams or "auto").lower()
    mime = sniff(pic.data, pic.mime) if pic.data else ""
    head = f"> **Picture {n}** (`{pic.name}`)."
    if pic.caption:
        head += f" Caption: {_one_line(pic.caption)}."
    if pic.alt and pic.alt.strip() != pic.caption.strip():
        head += f" Alt text: {_one_line(pic.alt)}."
    lines = [head]
    info = {"n": n, "name": pic.name, "type": mime or ("word drawing" if pic.labels else ""), "caption": pic.caption,
            "bytes": len(pic.data), "read_by": [], "note": ""}
    labels = list(pic.labels)
    if labels:
        info["read_by"].append("text in the drawing")
    if pic.data and mode != "off":
        got = describe(pic.data, mime, s) if mode == "auto" and llm.vision_routes(s) else None
        if got and got.get("description"):
            lines.append(f"> Diagram description ({got['model']}): {got['description']}")
            info["read_by"].append(f"vision model {got['model']}" + (" (cached)" if got.get("cached") else ""))
            info["description"] = got["description"]
        else:
            if got:
                info["note"] = got["error"]
            if mime == "image/svg+xml":
                found = svg_text(pic.data)
                if found:
                    labels += [x for x in found if x not in labels]
                    info["read_by"].append("SVG text")
            else:
                words = ocr(pic.data, mime)
                if words:
                    lines.append(f"> Text in the picture (OCR): {_one_line(words)}")
                    info["read_by"].append("OCR (tesseract)")
    if labels and mode != "off" or pic.labels:
        lines.append("> Text in the picture: " + "; ".join(_one_line(x) for x in labels[:150]) + ".")
    if pic.data and mode != "off" and not info["read_by"]:
        why = info["note"] or ("no vision model is configured and no text could be read locally"
                               if mode == "auto" else "no text could be read locally")
        lines.append(f"> Not described: {why}.")
        info["note"] = why
    info["chars"] = sum(len(x) for x in lines[1:])
    return "\n".join(lines), info
