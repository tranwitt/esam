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
  * pictures, where they appear in the text: inline pictures, pictures in
    table cells, and FLOATING pictures (those the text wraps around),
    which Word keeps in a separate drawing layer ("OfficeArt") and ties to
    an anchor character. PNG, JPEG, BMP and TIFF as they are; EMF and WMF
    drawings (and the preview of an embedded object such as a Visio
    drawing or a chart) are taken out as metafiles, which ESAM can read
    further only where LibreOffice is installed to render them;
  * diagrams DRAWN in Word with shapes and text boxes: the text of the
    shapes anchored in one paragraph, in reading order, as one drawing;
  * a picture used several times is read each time; pictures in page
    headers and footers (logos) are left out, and said so.

What it cannot do, and LibreOffice (esamkit/readers.py) can: automatic
numbers (numbered headings and lists show without their number), and
password-protected files. Word 6/95 files give their text only. Anything
in the drawing layer this reader cannot follow is still found by scanning
for picture data, and placed at the end with a note.

References: [MS-CFB] Compound File Binary File Format and [MS-DOC] Word
(.doc) Binary File Format, both published by Microsoft.
"""

import struct
import zlib
from bisect import bisect_right

from .docmodel import Para, Picture, Table, reading_order

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


# ── The drawing layer (OfficeArt, [MS-ODRAW]) ────────────────────────────────
# Records are nested: an 8-byte header (version/instance, type, length) and a
# payload; version 0xF marks a container of more records.

_DGG, _BSTORE, _DG, _SPGR, _SP, _BSE, _FSP, _FOPT = 0xF000, 0xF001, 0xF002, 0xF003, 0xF004, 0xF007, 0xF00A, 0xF00B
_CHILD_ANCHOR, _TERTIARY = 0xF00F, 0xF122
_BLIP_FIRST, _BLIP_LAST = 0xF018, 0xF117
# blip record type -> (mime, the record instances that carry a second 16-byte id)
_BLIPS = {0xF01A: ("image/x-emf", {0x3D5}), 0xF01B: ("image/x-wmf", {0x217}), 0xF01C: ("image/x-pict", {0x543}),
          0xF01D: ("image/jpeg", {0x46B, 0x6E3}), 0xF02A: ("image/jpeg", {0x46B, 0x6E3}),
          0xF01E: ("image/png", {0x6E1}), 0xF01F: ("image/bmp", {0x7A9}), 0xF029: ("image/tiff", {0x6E5})}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/bmp": "bmp", "image/tiff": "tif",
              "image/x-emf": "emf", "image/x-wmf": "wmf", "image/x-pict": "pict"}
MAX_PICTURE = 64 * 1024 * 1024          # a decompressed metafile larger than this is not taken


def _records(buf: bytes, pos: int, end: int):
    """(version, instance, type, payload start, payload length) for each record in buf[pos:end]."""
    end = min(end, len(buf))
    while pos + 8 <= end:
        vi, kind, length = struct.unpack_from("<HHI", buf, pos)
        if pos + 8 + length > end:
            length = end - pos - 8          # a record cut short: take what is there, then stop
        yield vi & 0xF, vi >> 4, kind, pos + 8, length
        pos += 8 + length


def _dib_to_bmp(dib: bytes) -> bytes:
    """A device-independent bitmap as Word stores it (no file header) -> a .bmp file."""
    if len(dib) < 40:
        return dib
    header, bpp = struct.unpack_from("<I", dib, 0)[0], struct.unpack_from("<H", dib, 14)[0]
    colours = struct.unpack_from("<I", dib, 32)[0] or (1 << bpp if bpp <= 8 else 0)
    bits = 14 + header + 4 * colours
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, bits) + dib


def _blip(buf: bytes, instance: int, kind: int, start: int, length: int) -> tuple[str, bytes] | None:
    """The picture inside one blip record: (mime, bytes), or None."""
    if kind not in _BLIPS:
        return None
    mime, two_ids = _BLIPS[kind]
    body = start + 16 + (16 if instance in two_ids else 0)
    end = start + length
    if mime in ("image/x-emf", "image/x-wmf", "image/x-pict"):
        if body + 34 > end:
            return None
        saved, compression = struct.unpack_from("<I", buf, body + 28)[0], buf[body + 32]
        raw = buf[body + 34: min(end, body + 34 + saved)]
        if compression == 0:
            try:
                raw = zlib.decompressobj().decompress(raw, MAX_PICTURE)
            except zlib.error:
                return None
        return (mime, raw) if raw else None
    data = buf[body + 1: end]                 # one tag byte, then the file itself
    if mime == "image/bmp":
        return mime, _dib_to_bmp(data)
    found = carve(data) if mime in ("image/png", "image/jpeg") else []
    return (found[0][1], found[0][2]) if found else ((mime, data) if data else None)


def _blips_in(buf: bytes, pos: int, end: int, depth: int = 0) -> list[tuple[str, bytes]]:
    """Every picture in the records of buf[pos:end], nested containers included."""
    out = []
    if depth > 12:
        return out
    for ver, inst, kind, start, length in _records(buf, pos, end):
        if _BLIP_FIRST <= kind <= _BLIP_LAST:
            got = _blip(buf, inst, kind, start, length)
            if got:
                out.append(got)
        elif kind == _BSE and length > 36:        # a store entry with the picture inside it
            out += _blips_in(buf, start + 36 + buf[start + 33], start + length, depth + 1)
        elif ver == 0xF:
            out += _blips_in(buf, start, start + length, depth + 1)
    return out


class _Shape:
    __slots__ = ("spid", "top", "pib", "txbx", "box", "order")

    def __init__(self, spid, top, order):
        self.spid, self.top, self.pib, self.txbx, self.box, self.order = spid, top, 0, 0, None, order


class _DrawingLayer:
    """What the document's drawing layer says: the stored pictures and, for
    every shape, the top-level shape it belongs to, its picture and its text box."""

    def __init__(self, table: bytes, fc: int, lcb: int, doc: bytes):
        self.blips: list[tuple[str, bytes] | None] = []      # the store, index 0 = picture number 1
        self.shapes: dict[int, _Shape] = {}
        self.header_shapes: set[int] = set()
        pos, end = fc, min(fc + lcb, len(table))
        first = True
        while pos + 8 <= end:
            if not first:                                    # each drawing is preceded by one byte: 0 main text, 1 headers
                where = table[pos]
                pos += 1
                if pos + 8 > end:
                    break
            ver, inst, kind, start, length = next(_records(table, pos, end))
            if kind == _DGG:
                self._store(table, start, start + length, doc)
            elif kind == _DG:
                before = set(self.shapes)
                self._drawing(table, start, start + length)
                if not first and where == 1:
                    self.header_shapes |= set(self.shapes) - before
            first = False
            pos = start + length

    def _store(self, table: bytes, pos: int, end: int, doc: bytes) -> None:
        for ver, inst, kind, start, length in _records(table, pos, end):
            if kind != _BSTORE:
                continue
            for _, _, k, s, n in _records(table, start, start + length):
                if k != _BSE or n < 36:
                    self.blips.append(None)
                    continue
                size, _refs, delay = struct.unpack_from("<III", table, s + 20)
                got = None
                if n > 36 + table[s + 33]:                    # the picture sits in the entry
                    inside = _blips_in(table, s + 36 + table[s + 33], s + n)
                    got = inside[0] if inside else None
                elif delay != 0xFFFFFFFF and delay + 8 <= len(doc):   # the usual place: the WordDocument stream
                    inside = _blips_in(doc, delay, min(len(doc), delay + size + 8))
                    got = inside[0] if inside else None
                self.blips.append(got)

    def _drawing(self, buf: bytes, pos: int, end: int) -> None:
        for ver, inst, kind, start, length in _records(buf, pos, end):
            if kind == _SPGR:                                 # the drawing's own group: its children are the top-level shapes
                for v2, _, k2, s2, n2 in _records(buf, start, start + length):
                    if k2 == _SP:
                        self._shape(buf, s2, s2 + n2, None)
                    elif k2 == _SPGR:
                        self._group(buf, s2, s2 + n2, None, 0)

    def _group(self, buf: bytes, pos: int, end: int, top: int | None, depth: int) -> None:
        if depth > 12:
            return
        for _, _, kind, start, length in _records(buf, pos, end):
            if kind == _SP:
                spid = self._shape(buf, start, start + length, top)
                if top is None and spid:
                    top = spid                                # the first shape of a group is the group itself
            elif kind == _SPGR:
                self._group(buf, start, start + length, top, depth + 1)

    def _shape(self, buf: bytes, pos: int, end: int, top: int | None) -> int | None:
        spid, pib, txbx, box = None, 0, 0, None
        for _, inst, kind, start, length in _records(buf, pos, end):
            if kind == _FSP and length >= 8:
                spid = struct.unpack_from("<I", buf, start)[0]
            elif kind in (_FOPT, _TERTIARY):
                for k in range(min(inst, length // 6)):
                    opid, value = struct.unpack_from("<HI", buf, start + 6 * k)
                    if opid & 0x3FFF == 0x0104 and opid & 0x4000:      # pib: which stored picture the shape shows
                        pib = value
                    elif opid & 0x3FFF == 0x0080:                      # lTxid: which text box holds its text
                        txbx = value >> 16
            elif kind == _CHILD_ANCHOR and length >= 16:
                box = struct.unpack_from("<4i", buf, start)
        if spid is None:
            return None
        shape = _Shape(spid, top if top is not None else spid, len(self.shapes))
        shape.pib, shape.txbx, shape.box = pib, txbx, box
        self.shapes[spid] = shape
        return spid

    def members(self, top: int) -> list[_Shape]:
        return sorted((s for s in self.shapes.values() if s.top == top), key=lambda s: s.order)

    def picture(self, shape: _Shape) -> tuple[str, bytes] | None:
        return self.blips[shape.pib - 1] if 0 < shape.pib <= len(self.blips) else None


def _anchors(table: bytes, fc: int, lcb: int) -> dict[int, tuple[int, int, int, int, int]]:
    """Floating shapes: {character position of the anchor: (shape id, left, top, right, bottom)}."""
    n = (lcb - 4) // 30
    if n <= 0 or fc + lcb > len(table):
        return {}
    cps = struct.unpack_from(f"<{n + 1}i", table, fc)
    out = {}
    for k in range(n):
        spid, left, top, right, bottom = struct.unpack_from("<I4i", table, fc + 4 * (n + 1) + 26 * k)
        out[cps[k]] = (spid, left, top, right, bottom)
    return out


def _textboxes(table: bytes, fc: int, lcb: int) -> list[tuple[int, int, int]]:
    """Text boxes in story order: [(first character, end character, shape id)], positions within the text-box text."""
    n = (lcb - 4) // 26
    if n <= 0 or fc + lcb > len(table):
        return []
    cps = struct.unpack_from(f"<{n + 1}i", table, fc)
    return [(cps[k], cps[k + 1], struct.unpack_from("<I", table, fc + 4 * (n + 1) + 22 * k + 14)[0]) for k in range(n)]


def _inline_picture(datastream: bytes, loc: int) -> tuple[list[tuple[str, bytes]], bool]:
    """The picture an inline picture character points at in the Data stream:
    ([(mime, bytes)], True when something is there that this reader cannot take out)."""
    if loc < 0 or loc + 8 > len(datastream):
        return [], False
    size, header, mm = struct.unpack_from("<iHh", datastream, loc)
    end = min(len(datastream), loc + max(size, 0))
    start = loc + header
    if mm == 0x66 and start < end:                    # a linked picture: its file name comes first
        start += 1 + datastream[start]
    found: list[tuple[str, bytes]] = []
    if mm in (0x64, 0x66):
        try:
            found = _blips_in(datastream, start, end)
        except (struct.error, IndexError):
            found = []
    if not found:
        found = [(mime, blob) for _, mime, blob in carve(datastream, loc, end)][:1]
    # mm 0x64 with nothing in it is a drawn shape, whose text is read from its text box
    return found[:1], not found and mm != 0x64 and size > 0x44


class _Floating:
    """The floating things of one text: which pictures and which shape text
    sit at each anchor character."""

    def __init__(self, layer: _DrawingLayer | None, anchors: dict, box_text: dict[int, list[str]]):
        self.layer, self.anchors, self.box_text = layer, anchors, box_text
        self.used: set[int] = set()

    def at(self, cp: int) -> tuple[list[tuple[str, bytes]], list[tuple[tuple | None, list[str]]]]:
        """(pictures, [(box, the text lines of one shape)]) anchored at character cp."""
        if cp not in self.anchors:
            return [], []
        spid, *rect = self.anchors[cp]
        members = self.layer.members(spid) if self.layer else []
        pictures, texts = [], []
        for m in members:
            pic = self.layer.picture(m)
            if pic:
                pictures.append(pic)
        for sid, box in [(m.spid, m.box if m.spid != spid and m.box else tuple(rect)) for m in members] or [(spid, tuple(rect))]:
            if sid in self.box_text and sid not in self.used:
                self.used.add(sid)
                if self.box_text[sid]:
                    texts.append((box, self.box_text[sid]))
        return pictures, texts


# ── The Word document ────────────────────────────────────────────────────────

_FIELD_BEGIN, _FIELD_SEP, _FIELD_END = "\x13", "\x14", "\x15"
_CELL, _PARA, _BREAK = "\x07", "\r", "\x0c"
_REPLACE = {"\x0b": "\n", "\x1e": "-", "\x1f": "", "\xa0": " ", "\x0e": " ", "\t": " ",
            "\x02": "", "\x03": "", "\x04": "", "\x05": "", "\x08": "", "\x01": ""}


_UNREAD = ("a picture or embedded object in a format this reader does not take out was skipped: "
           "install LibreOffice to read it")
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

    # The drawing layer: floating pictures and drawn shapes, each tied to an anchor character in the text.
    layer, anchors, box_text = None, {}, {}
    try:
        if pair(50)[1]:
            layer = _DrawingLayer(table, *pair(50), doc)
        anchors = _anchors(table, *pair(40))
    except (struct.error, IndexError, StopIteration, zlib.error):
        layer, anchors = None, {}
        notes.append("the drawing layer of this document could not be followed: floating pictures are placed "
                     "at the end")
    # Text typed inside drawing shapes lives after the main text, footnotes, headers, macros, comments and endnotes.
    box_start = sum(ccp[:6])
    stray: list[str] = []
    if ccp[6] > 0:
        try:
            boxes = _textboxes(table, *pair(56))
        except struct.error:
            boxes = []
        lines = lambda a, b: [x for x in (" ".join(_clean(_pair(y)).split()) for y in    # noqa: E731
                                          text_of(box_start + a, box_start + b)[0].replace(_CELL, _PARA).split(_PARA)) if x]
        for a, b, lid in boxes:
            if 0 <= a <= b <= ccp[6]:
                box_text.setdefault(lid, [])
                box_text[lid] += lines(a, b)
        if not boxes:
            stray = lines(0, ccp[6])
    floating = _Floating(layer, anchors, box_text)
    blocks = _blocks(main, fcs, papx, chpx, styles, datastream, notes, floating)

    # Shape text that no anchor led to: kept, at the end.
    stray += [x for lid, found in box_text.items() if lid not in floating.used for x in found]
    stray = list(dict.fromkeys(stray))
    if stray:
        n = sum(1 for b in blocks for q in getattr(b, "pictures", []) if not q.data)
        blocks.append(Para(pictures=[Picture(f"drawing{n + 1}", labels=stray)]))

    # Pictures in page headers and footers (a logo, a classification banner) are not content.
    skip: set[bytes] = set()
    head0 = ccp[0] + ccp[1]
    if ccp[2] > 0:
        head, head_fcs = text_of(head0, head0 + ccp[2])
        for i, ch in enumerate(head):
            if ch == "\x01":
                props = chpx.at(head_fcs[i]) or {}
                if SPRM_C_PIC in props and not _u(props.get(SPRM_C_DATA)) and not _u(props.get(SPRM_C_OLE2)):
                    skip |= {blob for _, blob in _inline_picture(datastream, _u(props[SPRM_C_PIC]))[0]}
    if layer:
        skip |= {pic[1] for sid in layer.header_shapes for pic in [layer.picture(layer.shapes[sid])] if pic}

    # Pictures Word stored without a position we could follow.
    placed = {q.data for b in blocks for q in getattr(b, "pictures", []) if q.data}
    skip -= placed
    stored = [x for x in (layer.blips if layer else []) if x]
    loose = [x for x in stored + [(mime, blob) for _, mime, blob in carve(datastream) + carve(doc)]
             if x[1] not in placed and x[1] not in skip]
    # a picture found by scanning that lies inside a stored one (a preview inside a metafile) is not a second picture
    loose = [x for x in dict.fromkeys(loose) if not any(x[1] != big and x[1] in big for big in placed | skip)]
    for i, (mime, blob) in enumerate(loose, 1):
        blocks.append(Para(pictures=[Picture(f"picture{len(placed) + i}.{EXTENSIONS.get(mime, 'bin')}", blob, mime)]))
    if loose:
        notes.append(f"{len(loose)} picture(s) are placed at the end: their position in the text "
                     "is not recorded in a way this reader follows")
    if skip:
        notes.append(f"{len(skip)} picture(s) in the page headers or footers (such as a logo) were left out")
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
            notes: list[str], floating: "_Floating | None" = None) -> list:
    blocks: list = []
    table: Table | None = None
    row: list[str] = []
    cell: list[str] = []
    runs: list[tuple[str, bool]] = []
    pics: list[Picture] = []
    field: list[bool] = []             # one entry per open field: True while in its instruction part
    shape_text: list[tuple[tuple | None, list[str]]] = []   # the text of the shapes anchored in this paragraph
    n_pics = n_drawings = 0
    have_props = bool(papx.items)

    def picture(mime: str, blob: bytes) -> Picture:
        nonlocal n_pics
        n_pics += 1
        return Picture(f"picture{n_pics}.{EXTENSIONS.get(mime, 'bin')}", blob, mime)

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
                found, unread = _inline_picture(datastream, _u(props[SPRM_C_PIC]))
                pics += [picture(*x) for x in found]
                if unread and _UNREAD not in notes:
                    notes.append(_UNREAD)
                continue
            if ch == "\x08" and floating is not None and _u(props.get(SPRM_C_SPEC)):
                found, texts = floating.at(i)                 # the anchor of a floating picture or a drawn shape
                pics += [picture(*x) for x in found]
                shape_text += texts
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
        if shape_text:                     # the shapes drawn in this paragraph are one drawing, read row by row
            n_drawings += 1
            labels = list(dict.fromkeys(x for found in reading_order(shape_text) for x in found))
            pics.append(Picture(f"drawing{n_drawings}", labels=labels))
            shape_text = []
        runs = [(_pair(t), b) for t, b in runs]
        text = "".join(t for t, _ in runs).strip()
        if in_table:
            if table is None:
                table = Table()
            for q in pics:
                q.cell = (len(table.rows), len(row))
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
