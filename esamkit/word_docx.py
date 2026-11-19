"""
Read a Word .docx (Office Open XML) into blocks, with the standard library
only: a .docx is a zip of XML parts, and carrying one less wheel across the
air gap is worth the hundred lines.

What is read: paragraphs in document order with their role (Title, Heading
1-9 by style, by a style based on one, or by outline level; list items;
captions), automatic numbers as Word displays them (numbered headings such
as "1.2", "a)" list items), bold runs, line breaks, tables (also nested, as
text), tracked insertions (deletions are left out), content controls,
hyperlinks, and pictures: embedded images with their alt text, and the text
inside drawings made of Word shapes, SmartArt and charts.

What is not: headers and footers, footnotes, comments, the table of
contents (it repeats the headings), text effects.
"""

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .docmodel import Para, Picture, Table, merge_drawings, reading_order

MAX_PART = 200 * 1024 * 1024         # refuse absurd (zip-bomb) parts
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
        ".svg": "image/svg+xml", ".emf": "image/x-emf", ".wmf": "image/x-wmf"}
_HEADING = re.compile(r"^heading\s*([1-9])$", re.I)
_TOC = re.compile(r"^(toc\s*\d|toc heading|table of figures|index\s*\d)", re.I)
_LIST_STYLE = re.compile(r"^list\s*(bullet|number|paragraph|continue)?", re.I)


class DocxError(ValueError):
    pass


def _ln(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(el, name: str, default: str = "") -> str:
    """An attribute by local name: w:val and a strict-OOXML val are the same to us."""
    for key, value in el.attrib.items():
        if _ln(key) == name:
            return value
    return default


def _child(el, name: str):
    if el is None:
        return None
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _on(el) -> bool:
    """A toggle element such as <w:b/> or <w:b w:val="0"/>."""
    return el is not None and _attr(el, "val", "1").lower() not in ("0", "false", "off")


def _xml(z: zipfile.ZipFile, name: str):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise DocxError(f"{name} is {info.file_size} bytes uncompressed: refusing to read it")
    data = z.read(name)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise DocxError(f"{name} declares a DTD, which a Word document never does: refusing to read it")
    return ET.fromstring(data)


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """{relationship id: part name} for a part (internal targets only)."""
    folder, name = posixpath.split(part)
    root = _xml(z, posixpath.join(folder, "_rels", name + ".rels"))
    out = {}
    for rel in root if root is not None else []:
        if rel.attrib.get("TargetMode") == "External":
            continue
        target = rel.attrib.get("Target", "")
        out[rel.attrib.get("Id", "")] = (target.lstrip("/") if target.startswith("/")
                                         else posixpath.normpath(posixpath.join(folder, target)))
    return out


# ── Automatic numbering ──────────────────────────────────────────────────────

def _roman(n: int) -> str:
    out = ""
    for value, sym in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
                       (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out, n = out + sym, n - value
    return out


def _letters(n: int) -> str:
    """1 → a, 26 → z, 27 → aa (as Word does: the letter repeated)."""
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1) if n > 0 else ""


def _format(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter",):
        return _letters(n)
    if fmt in ("upperLetter",):
        return _letters(n).upper()
    if fmt == "lowerRoman":
        return _roman(n).lower()
    if fmt == "upperRoman":
        return _roman(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt in ("none", "bullet"):
        return ""
    return str(n)


class Numbering:
    """word/numbering.xml, and the counters as the document is read top to bottom."""

    def __init__(self, root):
        self.abstract: dict[str, dict[int, dict]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.counters: dict[str, list[int | None]] = {}
        self.started: set[str] = set()
        for el in root if root is not None else []:
            if _ln(el.tag) == "abstractNum":
                levels = {}
                for lvl in el:
                    if _ln(lvl.tag) != "lvl":
                        continue
                    get = lambda name, default="": _attr(_child(lvl, name), "val", default) if _child(lvl, name) is not None else default  # noqa: E731
                    levels[int(_attr(lvl, "ilvl", "0"))] = {"fmt": get("numFmt", "decimal"), "text": get("lvlText"),
                                                            "start": int(get("start", "1") or 1)}
                self.abstract[_attr(el, "abstractNumId")] = levels
            elif _ln(el.tag) == "num":
                ref = _child(el, "abstractNumId")
                starts = {}
                for ov in el:
                    so = _child(ov, "startOverride") if _ln(ov.tag) == "lvlOverride" else None
                    if so is not None:
                        starts[int(_attr(ov, "ilvl", "0"))] = int(_attr(so, "val", "1") or 1)
                if ref is not None:
                    self.nums[_attr(el, "numId")] = (_attr(ref, "val"), starts)

    def next(self, num_id: str, ilvl: int) -> tuple[str, bool] | None:
        """(the number as displayed, is it a bullet) for the next paragraph at
        this list level; None if the list is not defined."""
        if num_id not in self.nums:
            return None
        abs_id, starts = self.nums[num_id]
        levels = self.abstract.get(abs_id) or {}
        lvl = levels.get(ilvl)
        if lvl is None:
            return None
        c = self.counters.setdefault(abs_id, [None] * 9)
        if num_id not in self.started:
            self.started.add(num_id)
            for i, start in starts.items():
                if 0 <= i < 9:
                    c[i] = start - 1
        ilvl = max(0, min(ilvl, 8))
        c[ilvl] = (c[ilvl] if c[ilvl] is not None else lvl["start"] - 1) + 1
        for deeper in range(ilvl + 1, 9):
            c[deeper] = None
        for above in range(ilvl):
            if c[above] is None:
                c[above] = (levels.get(above) or {}).get("start", 1)
        if lvl["fmt"] == "bullet":
            return "", True
        text = re.sub(r"%([1-9])", lambda m: _format(c[int(m[1]) - 1] or 0, (levels.get(int(m[1]) - 1) or lvl)["fmt"]),
                      lvl["text"])
        return text.strip(), False


# ── Styles ───────────────────────────────────────────────────────────────────

class Styles:
    def __init__(self, root):
        self.by_id: dict[str, dict] = {}
        for st in root if root is not None else []:
            if _ln(st.tag) != "style":
                continue
            ppr = _child(st, "pPr")
            numpr = _child(ppr, "numPr")
            outline = _child(ppr, "outlineLvl")
            self.by_id[_attr(st, "styleId")] = {
                "name": _attr(_child(st, "name"), "val") if _child(st, "name") is not None else "",
                "based": _attr(_child(st, "basedOn"), "val") if _child(st, "basedOn") is not None else "",
                "outline": int(_attr(outline, "val", "9")) if outline is not None else None,
                "num": (_attr(_child(numpr, "numId"), "val") if _child(numpr, "numId") is not None else "",
                        int(_attr(_child(numpr, "ilvl"), "val", "0")) if _child(numpr, "ilvl") is not None else None)
                if numpr is not None else None,
            }

    def chain(self, style_id: str) -> list[dict]:
        out, seen = [], set()
        while style_id and style_id in self.by_id and style_id not in seen:
            seen.add(style_id)
            out.append(self.by_id[style_id])
            style_id = self.by_id[style_id]["based"]
        return out


# ── The document ─────────────────────────────────────────────────────────────

class _Reader:
    def __init__(self, z: zipfile.ZipFile, part: str):
        self.z, self.part = z, part
        self.rels = _rels(z, part)
        folder = posixpath.dirname(part)
        self.styles = Styles(_xml(z, posixpath.join(folder, "styles.xml")))
        self.numbering = Numbering(_xml(z, posixpath.join(folder, "numbering.xml")))
        self.notes: list[str] = []
        self._seen: dict[str, int] = {}

    # pictures -----------------------------------------------------------------
    def _labels_from_part(self, rid: str) -> list[str]:
        target = self.rels.get(rid)
        root = _xml(self.z, target) if target else None
        out: list[str] = []
        for el in root.iter() if root is not None else []:
            if _ln(el.tag) in ("t", "v") and (el.text or "").strip() and el.text.strip() not in out:
                out.append(el.text.strip())
        return out[:120]

    def pictures(self, el) -> list[Picture]:
        """Pictures in a drawing / legacy picture / embedded object element."""
        alt = ""
        for d in el.iter():
            if _ln(d.tag) == "docPr":
                alt = _attr(d, "descr") or _attr(d, "title")
                break
        out: list[Picture] = []
        labels: list[str] = []
        shapes: list[tuple[tuple | None, str]] = []    # (where the shape sits, its text)
        where: dict[int, tuple] = {}                   # text box element -> the box of the shape that holds it
        for d in el.iter():
            if _ln(d.tag) == "wsp":
                box = self._box(d)
                for x in d.iter():
                    if _ln(x.tag) == "txbxContent":
                        where[id(x)] = box
        for d in el.iter():
            name = _ln(d.tag)
            rid = _attr(d, "embed") if name == "blip" else _attr(d, "id") if name == "imagedata" else ""
            if rid and rid in self.rels:
                target = self.rels[rid]
                try:
                    info = self.z.getinfo(target)
                except KeyError:
                    continue
                if info.file_size > MAX_PART:
                    self.notes.append(f"{posixpath.basename(target)}: too large, skipped")
                    continue
                ext = posixpath.splitext(target)[1].lower()
                out.append(Picture(posixpath.basename(target), self.z.read(target), MIME.get(ext, ""), alt=alt))
            elif name == "relIds" and _attr(d, "dm"):          # SmartArt: its text is in the data part
                labels += self._labels_from_part(_attr(d, "dm"))
            elif name == "chart" and _attr(d, "id"):           # a chart: titles, series and category names
                labels += self._labels_from_part(_attr(d, "id"))
            elif name == "txbxContent":                        # text typed inside a shape
                text = " ".join("".join(t.text or "" for t in p.iter() if _ln(t.tag) == "t").strip()
                                for p in d.iter() if _ln(p.tag) == "p").strip()
                if text:
                    shapes.append((where.get(id(d)), text))
        # the shapes of one drawing, top row first and left to right
        labels += [x for x in dict.fromkeys(reading_order(shapes)) if x not in labels]
        if out:
            out[-1].labels = labels
        elif labels:
            self._seen["drawing"] = self._seen.get("drawing", 0) + 1
            out.append(Picture(f"drawing{self._seen['drawing']}", alt=alt, labels=labels, box=self._anchor_box(el)))
        return out

    @staticmethod
    def _int(text) -> int | None:
        try:
            return int((text or "").strip())
        except ValueError:
            return None

    def _box(self, shape) -> tuple | None:
        """Where a shape sits inside its drawing: (left, top, right, bottom) from its a:xfrm."""
        for x in shape.iter():
            if _ln(x.tag) == "xfrm":
                off, ext = _child(x, "off"), _child(x, "ext")
                if off is not None:
                    left, top = self._int(off.get("x")), self._int(off.get("y"))
                    w = self._int(ext.get("cx")) if ext is not None else 0
                    h = self._int(ext.get("cy")) if ext is not None else 0
                    if left is not None and top is not None:
                        return left, top, left + (w or 0), top + (h or 0)
                return None
        return None

    def _anchor_box(self, el) -> tuple | None:
        """Where a floating drawing sits relative to its paragraph, from wp:anchor."""
        pos: dict[str, int] = {}
        for x in el.iter():
            name = _ln(x.tag)
            if name in ("positionH", "positionV"):
                off = _child(x, "posOffset")
                value = self._int(off.text) if off is not None else None
                if value is not None:
                    pos[name] = value
            elif name == "extent" and "cx" not in pos:
                pos["cx"], pos["cy"] = self._int(x.get("cx")) or 0, self._int(x.get("cy")) or 0
        if "positionH" not in pos or "positionV" not in pos:
            return None
        return (pos["positionH"], pos["positionV"], pos["positionH"] + pos.get("cx", 0),
                pos["positionV"] + pos.get("cy", 0))

    @staticmethod
    def _alternate(el):
        """mc:AlternateContent holds the same content twice (new and legacy): take one."""
        for name in ("Choice", "Fallback"):
            if _child(el, name) is not None:
                return _child(el, name)
        return []

    # paragraphs ---------------------------------------------------------------
    def _walk(self, el, bold: bool, runs: list, pics: list) -> None:
        for c in el:
            name = _ln(c.tag)
            if name == "r":
                rpr = _child(c, "rPr")
                b = bold
                if rpr is not None:
                    if _child(rpr, "b") is not None:
                        b = _on(_child(rpr, "b"))
                    style = _child(rpr, "rStyle")
                    if style is not None and _attr(style, "val").lower() == "strong":
                        b = True
                self._walk(c, b, runs, pics)
            elif name == "t":
                runs.append((c.text or "", bold))
            elif name == "tab":
                runs.append((" ", bold))
            elif name in ("br", "cr"):
                if _attr(c, "type") not in ("page", "column"):
                    runs.append(("\n", bold))
            elif name == "noBreakHyphen":
                runs.append(("-", bold))
            elif name in ("drawing", "pict", "object"):
                pics += self.pictures(c)
            elif name == "AlternateContent":                    # the same content twice: take one
                self._walk(self._alternate(c), bold, runs, pics)
            elif name in ("hyperlink", "smartTag", "sdt", "sdtContent", "ins", "moveTo", "fldSimple", "customXml",
                          "bdo", "dir"):
                self._walk(c, bold, runs, pics)
            # del, moveFrom, rPr, pPr, instrText, footnoteReference, ...: not document text

    def paragraph(self, p) -> Para | None:
        ppr = _child(p, "pPr")
        style_id = _attr(_child(ppr, "pStyle"), "val") if _child(ppr, "pStyle") is not None else ""
        chain = self.styles.chain(style_id)
        names = [s["name"] for s in chain]
        if any(_TOC.match(n) for n in names):
            return None
        runs: list = []
        pics: list = []
        drawn = self._seen.get("drawing", 0)
        self._walk(p, False, runs, pics)
        para = Para(runs=runs, pictures=merge_drawings(pics))
        if len(para.pictures) != len(pics):                # shapes were joined: number the drawings without gaps
            for pic in para.pictures:
                if not pic.data and pic.name.startswith("drawing"):
                    drawn += 1
                    pic.name = f"drawing{drawn}"
            self._seen["drawing"] = drawn

        numpr = _child(ppr, "numPr")
        num_id, ilvl = "", None
        if numpr is not None:
            num_id = _attr(_child(numpr, "numId"), "val") if _child(numpr, "numId") is not None else ""
            ilvl = int(_attr(_child(numpr, "ilvl"), "val", "0")) if _child(numpr, "ilvl") is not None else None
        for s in chain:                                        # the style's list, unless the paragraph has its own
            if s["num"]:
                num_id = num_id or s["num"][0]
                ilvl = ilvl if ilvl is not None else s["num"][1]
        ilvl = ilvl or 0

        level = next((int(m[1]) for n in names if (m := _HEADING.match(n))), 0)
        outline = _child(ppr, "outlineLvl")
        if not level:
            direct = int(_attr(outline, "val", "9")) if outline is not None else None
            inherited = next((s["outline"] for s in chain if s["outline"] is not None), None)
            lvl = direct if direct is not None else inherited
            level = lvl + 1 if lvl is not None and 0 <= lvl < 9 else 0

        numbered = self.numbering.next(num_id, ilvl) if num_id and num_id != "0" and para.text else None
        if any(n.lower() == "title" for n in names):
            para.kind = "title"
        elif level:
            para.kind, para.level = "heading", level
            para.number = numbered[0] if numbered else ""
        elif any(n.lower() == "caption" for n in names):
            para.kind = "caption"
        elif numbered is not None or (names and _LIST_STYLE.match(names[0]) and "paragraph" not in names[0].lower()
                                      and "continue" not in names[0].lower()):
            para.kind, para.level = "list", ilvl
            para.number = numbered[0] if numbered else ""
        return para

    # tables -------------------------------------------------------------------
    def _cell_text(self, tc, pics: list) -> str:
        parts = []
        for c in tc:
            name = _ln(c.tag)
            if name == "p":
                para = self.paragraph(c)
                if para is not None:
                    pics += para.pictures
                    if para.text:
                        parts.append(para.text.replace("\n", " "))
            elif name == "tbl":
                inner = self.table(c)
                pics += inner.pictures
                parts += [" ".join(x for x in row if x) for row in inner.rows]
            elif name in ("sdt", "sdtContent", "customXml"):
                parts.append(self._cell_text(c, pics))
        return " ".join(p for p in parts if p)

    def table(self, tbl) -> Table:
        t = Table()
        for tr in self._direct(tbl, "tr"):
            row = []
            for tc in self._direct(tr, "tc"):
                before = len(t.pictures)
                row.append(self._cell_text(tc, t.pictures))
                for pic in t.pictures[before:]:
                    pic.cell = (len(t.rows), len(row) - 1)
            t.rows.append(row)
        return t

    def _direct(self, el, want: str, stop: str = "tbl") -> list:
        """Descendants named `want` that are not inside a nested table."""
        out = []
        for c in el:
            name = _ln(c.tag)
            if name == want:
                out.append(c)
            elif name != stop:
                out += self._direct(c, want, stop)
        return out

    def blocks(self, container) -> list:
        out = []
        for c in container:
            name = _ln(c.tag)
            if name == "p":
                para = self.paragraph(c)
                if para is not None and (para.text or para.pictures):
                    out.append(para)
            elif name == "tbl":
                out.append(self.table(c))
            elif name in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
                out += self.blocks(c)
            elif name == "AlternateContent":
                out += self.blocks(self._alternate(c))
        return out


def parse_docx(data: bytes) -> tuple[list, list[str]]:
    """(blocks, notes) for the bytes of a .docx / .docm file."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise DocxError(f"not a .docx file (not a zip archive): {e}") from None
    with z:
        part = "word/document.xml"
        root_rels = _xml(z, "_rels/.rels")
        for rel in root_rels if root_rels is not None else []:
            if rel.attrib.get("Type", "").endswith("/officeDocument"):
                part = rel.attrib.get("Target", part).lstrip("/")
        root = _xml(z, part)
        if root is None:
            raise DocxError("not a Word document: it has no main document part (is it a spreadsheet or a deck?)")
        body = _child(root, "body")
        if body is None:
            raise DocxError("the main part has no <w:body>: not a Word document")
        reader = _Reader(z, part)
        return reader.blocks(body), reader.notes
