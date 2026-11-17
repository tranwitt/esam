"""
Read a legacy Word .doc (the Word 97-2003 binary format) into blocks, with
the standard library only.

A .doc is an OLE2 "compound file": a small file system holding streams. The
text is in the `WordDocument` stream, but not in order and not in one
encoding: a PIECE TABLE (in the `0Table` / `1Table` stream) lists the pieces
of text, each either 8-bit (Windows-1252) or UTF-16. Paragraph properties
(style, table membership, list membership) and character properties (bold,
"this character is a picture") are stored apart from the text, keyed by file
offset. This module reads all of that:

  * text of the main document, from the piece table (any piece order,
    both encodings), with field codes removed and field results kept;
  * headings (the built-in Heading 1-9 styles, styles based on them, and
    outline levels), the Title and Caption styles, list items;
  * tables, row by row and cell by cell;
  * bold runs (so `**SW-SRS-BRK-011**` requirement ids survive);
  * the text typed inside drawing shapes (text boxes);
  * embedded PNG and JPEG pictures, placed where they appear when Word
    recorded a position for them.

What it cannot do, and LibreOffice (esamkit/readers.py) can: automatic
numbers (numbered headings and lists show without their number), vector
pictures (EMF/WMF), and password-protected files. Word 6/95 files give
their text only.

References: [MS-CFB] Compound File Binary File Format and [MS-DOC] Word
(.doc) Binary File Format, both published by Microsoft.
"""

import struct
from bisect import bisect_right

from .docmodel import Para, Picture, Table

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END, _FREE = 0xFFFFFFFE, 0xFFFFFFFF
_NO_STREAM = 0xFFFFFFFF


class DocError(ValueError):
    pass


# ── The compound file (a FAT file system in a file) ──────────────────────────

class CompoundFile:
    def __init__(self, data: bytes):
        if data[:8] != OLE_MAGIC:
            raise DocError("not a Word .doc file (no OLE2 compound-file signature)")
        if len(data) < 512:
            raise DocError("the .doc file is truncated")
        self.data = data
        (self.sector_shift, self.mini_shift) = struct.unpack_from("<HH", data, 0x1E)
        if self.sector_shift not in (9, 12) or self.mini_shift != 6:
            raise DocError("the .doc file has a damaged header")
        self.sector = 1 << self.sector_shift
        (first_dir,) = struct.unpack_from("<I", data, 0x30)
        (self.cutoff, first_mini, _n_mini, first_difat, _n_difat) = struct.unpack_from("<IIIII", data, 0x38)
        # The FAT's own sectors are listed in the header, then in DIFAT sectors.
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        seen, sect = set(), first_difat
        while sect not in (_END, _FREE) and sect not in seen and len(seen) < 100000:
            seen.add(sect)
            entries = struct.unpack_from(f"<{self.sector // 4}I", self._sector(sect))
            difat += entries[:-1]
            sect = entries[-1]
        self.fat: list[int] = []
        for s in difat:
            if s not in (_END, _FREE):
                self.fat += struct.unpack_from(f"<{self.sector // 4}I", self._sector(s))
        self.entries = self._directory(self._chain(first_dir))
        root = self.entries[0] if self.entries else None
        if root is None:
            raise DocError("the .doc file has no directory")
        self._mini_stream = self._chain(root["start"])[: root["size"]]
        mini_fat_bytes = self._chain(first_mini) if first_mini not in (_END, _FREE) else b""
        self.mini_fat = list(struct.unpack_from(f"<{len(mini_fat_bytes) // 4}I", mini_fat_bytes))
        self.top = self._children(root)

    def _sector(self, n: int) -> bytes:
        off = (n + 1) << self.sector_shift
        chunk = self.data[off: off + self.sector]
        return chunk + b"\0" * (self.sector - len(chunk))

    def _chain(self, start: int) -> bytes:
        out, seen, s = [], set(), start
        while s not in (_END, _FREE) and s < len(self.fat) and s not in seen:
            seen.add(s)
            out.append(self._sector(s))
            s = self.fat[s]
        return b"".join(out)

    def _mini_chain(self, start: int, size: int) -> bytes:
        out, seen, s = [], set(), start
        while s not in (_END, _FREE) and s < len(self.mini_fat) and s not in seen:
            seen.add(s)
            out.append(self._mini_stream[s * 64: s * 64 + 64])
            s = self.mini_fat[s]
        return b"".join(out)[:size]

    def _directory(self, raw: bytes) -> list[dict]:
        out = []
        for off in range(0, len(raw) - 127, 128):
            e = raw[off: off + 128]
            (name_len,) = struct.unpack_from("<H", e, 0x40)
            name = e[: max(0, min(name_len, 64) - 2)].decode("utf-16-le", "replace")
            left, right, child = struct.unpack_from("<III", e, 0x44)
            start, size = struct.unpack_from("<IQ", e, 0x74)
            if self.sector_shift == 9:
                size &= 0xFFFFFFFF                    # version 3 files: only 32 bits are valid
            out.append({"name": name, "type": e[0x42], "left": left, "right": right, "child": child,
                        "start": start, "size": size})
        return out

    def _children(self, storage: dict) -> dict[str, dict]:
        """The entries directly inside a storage (they form a binary tree)."""
        out, stack, seen = {}, [storage["child"]], set()
        while stack:
            i = stack.pop()
            if i == _NO_STREAM or i >= len(self.entries) or i in seen:
                continue
            seen.add(i)
            e = self.entries[i]
            out[e["name"]] = e
            stack += [e["left"], e["right"]]
        return out

    def stream(self, name: str) -> bytes | None:
        e = self.top.get(name)
        if e is None or e["type"] != 2:
            return None
        if e["size"] < self.cutoff:
            return self._mini_chain(e["start"], e["size"])
        return self._chain(e["start"])[: e["size"]]


# ── Property modifiers ("sprms") ─────────────────────────────────────────────

SPRM_P_IN_TABLE, SPRM_P_TTP, SPRM_P_ITAP = 0x2416, 0x2417, 0x6649
SPRM_P_INNER_CELL, SPRM_P_INNER_TTP = 0x244B, 0x244C
SPRM_P_ILVL, SPRM_P_ILFO, SPRM_P_OUTLVL = 0x260A, 0x460B, 0x2640
SPRM_C_BOLD, SPRM_C_SPEC, SPRM_C_PIC, SPRM_C_DATA, SPRM_C_OLE2, SPRM_C_OBJ = 0x0835, 0x0855, 0x6A03, 0x0806, 0x080A, 0x0856
_SIZES = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}


def sprms(grpprl: bytes) -> dict[int, bytes]:
    """{opcode: operand} of a property list. The operand size is encoded in
    the opcode's top three bits; size class 6 carries its own length."""
    out, i, n = {}, 0, len(grpprl)
    while i + 2 <= n:
        (op,) = struct.unpack_from("<H", grpprl, i)
        i += 2
        spra = op >> 13
        if spra == 6:
            if op == 0xD608 and i + 2 <= n:           # sprmTDefTable: 2-byte length, counted from itself
                size = struct.unpack_from("<H", grpprl, i)[0] - 1
                i += 2
            elif i < n:
                size = grpprl[i]
                i += 1
                if op == 0xC615 and size == 255 and i < n:      # sprmPChgTabs, long form
                    dels = grpprl[i]
                    adds = grpprl[i + 1 + 4 * dels] if i + 1 + 4 * dels < n else 0
                    size = 2 + 4 * dels + 3 * adds
            else:
                break
        else:
            size = _SIZES[spra]
        if size < 0 or i + size > n:
            break
        out[op] = grpprl[i: i + size]
        i += size
    return out


def _u(operand: bytes | None) -> int:
    return int.from_bytes(operand, "little") if operand else 0


class _Ranges:
    """Properties keyed by file-offset range, as Word stores them."""

    def __init__(self):
        self.items: list[tuple[int, int, object]] = []
        self._starts: list[int] = []

    def add(self, start: int, end: int, value) -> None:
        self.items.append((start, end, value))

    def freeze(self) -> None:
        self.items.sort(key=lambda x: x[0])
        self._starts = [x[0] for x in self.items]

    def at(self, fc: int):
        i = bisect_right(self._starts, fc) - 1
        if i >= 0 and self.items[i][0] <= fc < self.items[i][1]:
            return self.items[i][2]
        return None


def _formatted_disk_pages(doc: bytes, table: bytes, fc: int, lcb: int) -> list[bytes]:
    """The 512-byte pages a 'bin table' (PlcfBte) points to."""
    if not lcb or fc + lcb > len(table) or lcb < 12:
        return []
    n = (lcb - 4) // 8
    pages = []
    for (pn,) in struct.iter_unpack("<I", table[fc + 4 * (n + 1): fc + 4 * (n + 1) + 4 * n]):
        off = (pn & 0x3FFFFF) * 512
        if off + 512 <= len(doc):
            pages.append(doc[off: off + 512])
    return pages


def _paragraph_properties(doc: bytes, table: bytes, fc: int, lcb: int) -> _Ranges:
    out = _Ranges()
    for page in _formatted_disk_pages(doc, table, fc, lcb):
        crun = page[511]
        if not crun or 4 * (crun + 1) + 13 * crun > 511:
            continue
        fcs = struct.unpack_from(f"<{crun + 1}I", page, 0)
        for i in range(crun):
            off = page[4 * (crun + 1) + 13 * i] * 2
            if not off or off >= 510:
                continue
            cb = page[off]
            if cb == 0:
                size, start = page[off + 1] * 2, off + 2
            else:
                size, start = cb * 2 - 1, off + 1
            body = page[start: start + size]
            if len(body) < 2:
                continue
            out.add(fcs[i], fcs[i + 1], (struct.unpack_from("<H", body, 0)[0], sprms(body[2:])))
    out.freeze()
    return out


def _character_properties(doc: bytes, table: bytes, fc: int, lcb: int) -> _Ranges:
    out = _Ranges()
    for page in _formatted_disk_pages(doc, table, fc, lcb):
        crun = page[511]
        if not crun or 4 * (crun + 1) + crun > 511:
            continue
        fcs = struct.unpack_from(f"<{crun + 1}I", page, 0)
        for i in range(crun):
            off = page[4 * (crun + 1) + i] * 2
            props = sprms(page[off + 1: off + 1 + page[off]]) if off and off < 511 else {}
            out.add(fcs[i], fcs[i + 1], props)
    out.freeze()
    return out


def _styles(table: bytes, fc: int, lcb: int) -> dict[int, tuple[int, int]]:
    """{style index: (built-in style id 'sti', index of the style it is based on)}."""
    out: dict[int, tuple[int, int]] = {}
    if not lcb or fc + lcb > len(table) or lcb < 6:
        return out
    (cb_stshi,) = struct.unpack_from("<H", table, fc)
    cstd, cb_base = struct.unpack_from("<HH", table, fc + 2)
    pos, end = fc + 2 + cb_stshi, fc + lcb
    for istd in range(cstd):
        if pos + 2 > end:
            break
        (cb_std,) = struct.unpack_from("<H", table, pos)
        pos += 2
        if cb_std >= 4 and pos + 4 <= end:
            w0, w1 = struct.unpack_from("<HH", table, pos)
            out[istd] = (w0 & 0x0FFF, w1 >> 4)
        pos += cb_std + (cb_std & 1)                 # entries are padded to an even length
    return out


# Built-in style ids ("sti") that matter here.
_STI_TITLE, _STI_CAPTION = 62, 34
_STI_TOC = set(range(19, 28)) | {10, 11, 12, 13, 14, 15, 16, 17, 18, 32, 33, 35}      # TOC 1-9, index, table of figures
_STI_LIST = set(range(47, 62))                                                     # List, List Bullet, List Number, ...


def _style_role(styles: dict[int, tuple[int, int]], istd: int) -> tuple[str, int]:
    """('heading', level) / ('title', 0) / ('caption', 0) / ('toc', 0) / ('list', 0) / ('', 0)."""
    seen = set()
    while istd in styles and istd not in seen:
        seen.add(istd)
        sti, base = styles[istd]
        if 1 <= sti <= 9:
            return "heading", sti
        if sti == _STI_TITLE:
            return "title", 0
        if sti == _STI_CAPTION:
            return "caption", 0
        if sti in _STI_TOC:
            return "toc", 0
        if sti in _STI_LIST:
            return "list", 0
        istd = base
    if not styles and 1 <= istd <= 9:                # no readable stylesheet: the fixed slots still hold
        return "heading", istd
    return "", 0


# ── Pictures ─────────────────────────────────────────────────────────────────

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG_NEXT = set(range(0xE0, 0xF0)) | {0xDB, 0xC0, 0xC2, 0xC4, 0xFE, 0xDD}


def _png_end(data: bytes, start: int) -> int:
    pos = start + 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4: pos + 8]
        pos += 12 + length
        if kind == b"IEND":
            return pos if pos <= len(data) else -1
        if length > 0x7FFFFFFF:
            break
    return -1


def carve(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[int, str, bytes]]:
    """PNG and JPEG images embedded in a byte range: [(offset, mime, bytes)]."""
    end = len(data) if end is None else min(end, len(data))
    out, pos = [], start
    while pos < end:
        png, jpg = data.find(_PNG, pos, end), data.find(b"\xff\xd8\xff", pos, end)
        if png < 0 and jpg < 0:
            break
        if png >= 0 and (jpg < 0 or png < jpg):
            stop = _png_end(data, png)
            if stop > 0:
                out.append((png, "image/png", data[png:stop]))
                pos = stop
            else:
                pos = png + 8
        else:
            if jpg + 3 < end and data[jpg + 3] in _JPEG_NEXT:
                limit = min(x for x in (data.find(_PNG, jpg + 3, end), data.find(b"\xff\xd8\xff", jpg + 3, end), end)
                            if x >= 0)
                stop = data.rfind(b"\xff\xd9", jpg, limit)
                if stop > jpg + 256:
                    out.append((jpg, "image/jpeg", data[jpg: stop + 2]))
                    pos = stop + 2
                    continue
            pos = jpg + 3
    return out


# ── The Word document ────────────────────────────────────────────────────────

_FIELD_BEGIN, _FIELD_SEP, _FIELD_END = "\x13", "\x14", "\x15"
_CELL, _PARA, _BREAK = "\x07", "\r", "\x0c"
_REPLACE = {"\x0b": "\n", "\x1e": "-", "\x1f": "", "\xa0": " ", "\x0e": " ", "\t": " ",
            "\x02": "", "\x03": "", "\x04": "", "\x05": "", "\x08": "", "\x01": ""}


_NUMBERS = ("automatic numbers (numbered headings and lists) are not shown by the built-in .doc reader: "
            "install LibreOffice for a full conversion")


def parse_doc(data: bytes) -> tuple[list, list[str]]:
    """(blocks, notes) for the bytes of a Word 97-2003 .doc file."""
    cf = CompoundFile(data)
    doc = cf.stream("WordDocument")
    if doc is None or len(doc) < 0x60:
        raise DocError("no WordDocument stream: this OLE file is not a Word document (an .xls or .ppt renamed?)")
    ident, nfib = struct.unpack_from("<HH", doc, 0)
    (flags,) = struct.unpack_from("<H", doc, 0x0A)
    if flags & 0x0100:
        raise DocError("the document is password-protected (encrypted): remove the password in Word and try again")
    notes: list[str] = []
    if ident != 0xA5EC or nfib < 0x00C1 or len(doc) < 0x1AA:
        return _word6(doc, notes)

    table = cf.stream("1Table" if flags & 0x0200 else "0Table") or cf.stream("0Table") or cf.stream("1Table")
    if table is None:
        raise DocError("the document's table stream is missing: the file is damaged")
    ccp = struct.unpack_from("<8i", doc, 0x4C)          # text, footnotes, headers, macros, comments, endnotes, textboxes, header textboxes
    pair = lambda i: struct.unpack_from("<II", doc, 0x9A + 8 * i)  # noqa: E731
    pieces = _pieces(table, *pair(33))
    if not pieces:
        (fc_min, fc_mac) = struct.unpack_from("<II", doc, 0x18)
        pieces = [(0, max(0, fc_mac - fc_min), fc_min, True)]

    def text_of(cp_from: int, cp_to: int) -> tuple[str, list[int]]:
        """Characters cp_from..cp_to and the file offset of each."""
        chars, fcs = [], []
        for cp0, cp1, fc, compressed in pieces:
            a, b = max(cp0, cp_from), min(cp1, cp_to)
            if a >= b:
                continue
            width = 1 if compressed else 2
            raw = doc[fc + (a - cp0) * width: fc + (b - cp0) * width]
            # One character per code unit, so that characters and file offsets stay aligned
            # (surrogate pairs are joined again in _pair).
            s = raw.decode("cp1252", "replace") if compressed else raw[: len(raw) & ~1].decode("utf-16-le", "surrogatepass")
            chars.append(s)
            fcs += range(fc + (a - cp0) * width, fc + (a - cp0) * width + len(s) * width, width)
        return "".join(chars), fcs

    papx = _paragraph_properties(doc, table, *pair(13))
    chpx = _character_properties(doc, table, *pair(12))
    styles = _styles(table, *pair(1))
    if not papx.items:
        notes.append("paragraph properties could not be read: headings are plain text, and table rows are "
                     "worked out from the cell marks")

    main, fcs = text_of(0, max(0, ccp[0]))
    datastream = cf.stream("Data") or b""
    blocks = _blocks(main, fcs, papx, chpx, styles, datastream, notes)

    # Text typed inside drawing shapes lives after the main text, footnotes, headers, macros, comments and endnotes.
    box_start = sum(ccp[:6])
    if ccp[6] > 0:
        boxes, _ = text_of(box_start, box_start + ccp[6])
        labels = [" ".join(_clean(x).split()) for x in boxes.replace(_CELL, _PARA).split(_PARA)]
        labels = [x for x in dict.fromkeys(labels) if x]
        if labels:
            blocks.append(Para(pictures=[Picture("drawing1", labels=labels)]))

    # Pictures Word stored without a position we could follow (floating pictures).
    placed = {p.data for b in blocks for p in getattr(b, "pictures", [])}
    loose = [(mime, blob) for _, mime, blob in carve(datastream) + carve(doc) if blob not in placed]
    for i, (mime, blob) in enumerate(dict.fromkeys(loose), 1):
        ext = "png" if mime == "image/png" else "jpg"
        blocks.append(Para(pictures=[Picture(f"picture{len(placed) + i}.{ext}", blob, mime)]))
    if loose:
        notes.append(f"{len(dict.fromkeys(loose))} picture(s) are placed at the end: their position in the text "
                     "is not recorded in a way this reader follows")
    return blocks, notes


def _pieces(table: bytes, fc: int, lcb: int) -> list[tuple[int, int, int, bool]]:
    """The piece table: [(first cp, end cp, file offset, is 8-bit)]."""
    pos, end = fc, min(fc + lcb, len(table))
    while pos < end:
        kind = table[pos]
        if kind == 1:                                  # property modifications: skip
            pos += 3 + struct.unpack_from("<H", table, pos + 1)[0]
        elif kind == 2:
            (size,) = struct.unpack_from("<I", table, pos + 1)
            body = table[pos + 5: pos + 5 + size]
            n = (len(body) - 4) // 12
            if n <= 0:
                return []
            cps = struct.unpack_from(f"<{n + 1}I", body, 0)
            out = []
            for i in range(n):
                (raw,) = struct.unpack_from("<I", body, 4 * (n + 1) + 8 * i + 2)
                compressed = bool(raw & 0x40000000)
                where = (raw & 0x3FFFFFFF) // 2 if compressed else raw & 0x3FFFFFFF
                out.append((cps[i], cps[i + 1], where, compressed))
            return out
        else:
            break
    return []


def _pair(text: str) -> str:
    """Join UTF-16 surrogate pairs that were kept as two characters."""
    if not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _clean(text: str) -> str:
    for k, v in _REPLACE.items():
        if k in text:
            text = text.replace(k, v)
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")


def _blocks(main: str, fcs: list[int], papx: _Ranges, chpx: _Ranges, styles: dict, datastream: bytes,
            notes: list[str]) -> list:
    blocks: list = []
    table: Table | None = None
    row: list[str] = []
    cell: list[str] = []
    runs: list[tuple[str, bool]] = []
    pics: list[Picture] = []
    field: list[bool] = []             # one entry per open field: True while in its instruction part
    n_pics = 0
    have_props = bool(papx.items)

    def flush_table():
        nonlocal table, row, cell
        if table is not None:
            if cell or row:
                row.append(" ".join(cell))
                table.rows.append(row)
            if table.rows:
                blocks.append(table)
        table, row, cell = None, [], []

    for i, ch in enumerate(main):
        if ch == _FIELD_BEGIN:
            field.append(True)
            continue
        if ch == _FIELD_SEP:
            if field:
                field[-1] = False
            continue
        if ch == _FIELD_END:
            if field:
                field.pop()
            continue
        if any(field):                     # inside a field's instruction (e.g. HYPERLINK "..."): not text
            continue
        if ch not in (_PARA, _CELL, _BREAK):
            props = chpx.at(fcs[i]) or {}
            if ch == "\x01" and _u(props.get(SPRM_C_SPEC)) and SPRM_C_PIC in props \
                    and not _u(props.get(SPRM_C_DATA)) and not _u(props.get(SPRM_C_OLE2)):
                loc = _u(props[SPRM_C_PIC])
                if 0 <= loc and loc + 4 <= len(datastream):
                    (size,) = struct.unpack_from("<i", datastream, loc)
                    found = carve(datastream, loc, loc + max(size, 0))
                    if found:
                        n_pics += 1
                        mime, blob = found[0][1], found[0][2]
                        pics.append(Picture(f"picture{n_pics}.{'png' if mime == 'image/png' else 'jpg'}", blob, mime))
                    elif size > 0x44:
                        notes.append("a picture in a vector format (EMF/WMF) or an embedded object was skipped: "
                                     "install LibreOffice to read it")
                continue
            bold = _u(props.get(SPRM_C_BOLD)) in (1, 0x81)
            text = _clean(ch)
            if text:
                if runs and runs[-1][1] == bold:
                    runs[-1] = (runs[-1][0] + text, bold)
                else:
                    runs.append((text, bold))
            continue

        # A paragraph mark, a cell mark or a page / section break ends the paragraph.
        istd, pp = papx.at(fcs[i]) or (0, {})
        in_table = bool(_u(pp.get(SPRM_P_IN_TABLE)) or _u(pp.get(SPRM_P_ITAP))) or ch == _CELL
        runs = [(_pair(t), b) for t, b in runs]
        text = "".join(t for t, _ in runs).strip()
        if in_table:
            if table is None:
                table = Table()
            table.pictures += pics
            row_end = bool(_u(pp.get(SPRM_P_TTP))) if have_props else (ch == _CELL and not text and not cell and bool(row))
            if row_end:
                if cell or text:
                    row.append(" ".join(cell + ([text] if text else [])))
                table.rows.append(row)
                row, cell = [], []
            elif ch == _CELL and not _u(pp.get(SPRM_P_INNER_CELL)):
                row.append(" ".join(cell + ([text] if text else [])))
                cell = []
            elif text:
                cell.append(text)             # a paragraph inside a cell, or a nested table's cell
        else:
            flush_table()
            role, level = _style_role(styles, istd)
            if role != "toc" and (text or pics):
                para = Para(runs=runs, pictures=pics)
                outline = _u(pp[SPRM_P_OUTLVL]) if SPRM_P_OUTLVL in pp else 9
                if role == "heading":
                    para.kind, para.level = "heading", level
                elif role in ("title", "caption"):
                    para.kind = role
                elif outline < 9:
                    para.kind, para.level = "heading", outline + 1
                elif role == "list" or _u(pp.get(SPRM_P_ILFO)) not in (0, 0xF801):
                    para.kind, para.level = "list", min(_u(pp.get(SPRM_P_ILVL)), 8)
                if _u(pp.get(SPRM_P_ILFO)) not in (0, 0xF801) and _NUMBERS not in notes:
                    notes.append(_NUMBERS)
                blocks.append(para)
        runs, pics = [], []
    flush_table()
    if runs or pics:
        blocks.append(Para(runs=[(_pair(t), b) for t, b in runs], pictures=pics))
    return blocks


def _word6(doc: bytes, notes: list[str]) -> tuple[list, list[str]]:
    """Word 6 / Word 95: the text is one 8-bit run between fcMin and fcMac."""
    fc_min, fc_mac = struct.unpack_from("<II", doc, 0x18)
    if not (0 < fc_min <= fc_mac <= len(doc)):
        raise DocError("this Word 6/95 file could not be read: open it in Word or LibreOffice and save it as .docx")
    text = doc[fc_min:fc_mac].decode("cp1252", "replace")
    out, depth = [], 0
    for ch in text:                                    # drop field instructions, keep results
        if ch == _FIELD_BEGIN:
            depth += 1
        elif ch == _FIELD_SEP:
            depth = max(0, depth - 1)
        elif ch != _FIELD_END and not depth:
            out.append(ch)
    paras = "".join(out).replace(_CELL, _PARA).replace(_BREAK, _PARA).split(_PARA)
    notes.append("Word 6/95 file: text only (no headings, tables or pictures). Install LibreOffice, or save it as "
                 ".docx, for a full conversion")
    return [Para(runs=[(_clean(p), False)]) for p in paras if _clean(p).strip()], notes
