"""Word files built from scratch for the tests, with the standard library only:

    make_docx(body_xml, ...)   a minimal .docx around a document body
    make_doc(text_pieces)      a minimal Word 97 .doc: an OLE2 compound file with a
                               WordDocument stream and a piece table, written the way
                               Word itself does (8-bit and UTF-16 pieces, in any order)

The .doc files under tests/fixtures/word/ are real ones written by LibreOffice;
these builders cover what LibreOffice never writes (8-bit pieces, pieces out of
order, the mini stream, an encrypted flag)."""

import io
import struct
import zipfile
import zlib

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = (f'xmlns:w="{W}" xmlns:r="{R}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')

STYLES = f"""<w:styles xmlns:w="{W}">
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
<w:style w:type="paragraph" w:styleId="Berschrift2"><w:name w:val="Mein Abschnitt"/><w:basedOn w:val="Heading2"/></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/></w:style>
<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/></w:style>
</w:styles>"""

# numId 1: bullets. numId 2: "1." / "a)" list. numId 3: legal heading numbers "1", "1.1".
NUMBERING = f"""<w:numbering xmlns:w="{W}">
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1">
 <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
 <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="2">
 <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1"/></w:lvl>
 <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>"""


def p(text="", style="", bold_prefix="", num=None, extra=""):
    """A paragraph. `bold_prefix` is a bold run before the text; `num` is (numId, ilvl)."""
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + (
        f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>' if num else "")
    runs = (f'<w:r><w:rPr><w:b/></w:rPr><w:t>{bold_prefix}</w:t></w:r>' if bold_prefix else "")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        runs += f'<w:r><w:t xml:space="preserve">{line}</w:t></w:r>' + ("<w:r><w:br/></w:r>" if i < len(lines) - 1 else "")
    return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}{extra}</w:p>"


def table(rows):
    return "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{c if c.startswith('<w:') else p(c)}</w:tc>" for c in row)
                               + "</w:tr>" for row in rows) + "</w:tbl>"


def picture(rid="rId1", alt=""):
    return (f'<w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="{alt}"/><a:graphic><a:graphicData>'
            f'<pic:pic><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r>')


def shapes(*labels):
    """A drawing made of Word shapes with text in them, wrapped the way Word writes it."""
    boxes = "".join(f"<wps:wsp><wps:txbx><w:txbxContent>{p(x)}</w:txbxContent></wps:txbx></wps:wsp>" for x in labels)
    drawing = f"<w:drawing><wp:anchor><a:graphic><a:graphicData>{boxes}</a:graphicData></a:graphic></wp:anchor></w:drawing>"
    legacy = "<w:pict>" + "".join(f"<w:txbxContent>{p(x)}</w:txbxContent>" for x in labels) + "</w:pict>"
    return f"<w:r><mc:AlternateContent><mc:Choice>{drawing}</mc:Choice><mc:Fallback>{legacy}</mc:Fallback></mc:AlternateContent></w:r>"


def png(width=8, height=8) -> bytes:
    raw = b"".join(b"\x00" + bytes([200, 30, 30] * width) for _ in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_docx(body: str, media: dict[str, bytes] | None = None) -> bytes:
    """media: {"image1.png": bytes} becomes relationship rId1, rId2, ... in order."""
    media = media or {}
    rels = "".join(f'<Relationship Id="rId{i}" Type="{R}/image" Target="media/{name}"/>'
                   for i, name in enumerate(media, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}<w:sectPr/></w:body></w:document>")
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", NUMBERING)
        z.writestr("word/_rels/document.xml.rels",
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for name, data in media.items():
            z.writestr(f"word/media/{name}", data)
    return buf.getvalue()


# ── A Word 97 .doc ───────────────────────────────────────────────────────────

_END, _FREE, _FAT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def compound_file(streams: dict[str, bytes]) -> bytes:
    """An OLE2 compound file (512-byte sectors). Streams under 4096 bytes go to
    the mini stream, as the format requires."""
    sectors: list[bytes] = []
    fat: list[int] = []

    def put(data: bytes) -> int:
        if not data:
            return _END
        first = len(sectors)
        chunks = [data[i:i + 512].ljust(512, b"\0") for i in range(0, len(data), 512)]
        for i, c in enumerate(chunks):
            sectors.append(c)
            fat.append(first + i + 1 if i < len(chunks) - 1 else _END)
        return first

    mini, mini_fat, entries = b"", [], []
    for name, data in streams.items():
        if len(data) < 4096:
            start = len(mini) // 64
            n = max(1, -(-len(data) // 64))
            mini += data.ljust(n * 64, b"\0")
            mini_fat += [start + i + 1 for i in range(n - 1)] + [_END]
        else:
            start = put(data)
        entries.append((name, 2, start, len(data)))
    mini_start = put(mini)
    mini_fat_start = put(struct.pack(f"<{len(mini_fat)}I", *mini_fat)) if mini_fat else _END

    def entry(name, kind, start, size, child=_FREE, right=_FREE):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw), kind, 1) + struct.pack("<III", _FREE, right, child)
                + b"\0" * 36 + struct.pack("<IQ", start, size))

    directory = entry("Root Entry", 5, mini_start, len(mini), child=1 if entries else _FREE)
    for i, (name, kind, start, size) in enumerate(entries, 1):
        directory += entry(name, kind, start, size, right=i + 1 if i < len(entries) else _FREE)
    dir_start = put(directory.ljust(-(-len(directory) // 512) * 512, b"\0"))
    fat_sector = len(sectors)
    fat.append(_FAT)
    assert len(fat) <= 128, "this little writer handles one FAT sector"
    sectors.append(struct.pack("<128I", *(fat + [_FREE] * (128 - len(fat)))))
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<III", 0, 1, dir_start) + struct.pack("<I", 0) + struct.pack("<I", 4096)
              + struct.pack("<II", mini_fat_start, 1 if mini_fat else 0) + struct.pack("<II", _END, 0)
              + struct.pack("<109I", fat_sector, *([_FREE] * 108)))
    assert len(header) == 512
    return header + b"".join(sectors)


def make_doc(pieces: list[tuple[str, bool]], table_stream: str = "0Table", encrypted: bool = False,
             pad_to: int = 0) -> bytes:
    """A Word 97 .doc whose text is `pieces`: [(text, is_8bit)], in reading
    order. In the FILE the pieces are stored in reverse order, as a document
    edited in Word has them. Paragraphs end with \\r, cells with \\x07."""
    fib = bytearray(0x400)
    stored, placed = bytearray(), []
    for text, eight_bit in reversed(pieces):
        raw = text.encode("cp1252") if eight_bit else text.encode("utf-16-le")
        if not eight_bit and len(stored) % 2:
            stored += b"\0"
        placed.append((len(fib) + len(stored), eight_bit))
        stored += raw
    placed.reverse()
    cps = [0]
    for text, _ in pieces:
        cps.append(cps[-1] + len(text))
    pcds = b"".join(struct.pack("<HIH", 0, (fc * 2) | 0x40000000 if eight_bit else fc, 0) for fc, eight_bit in placed)
    plc = struct.pack(f"<{len(cps)}I", *cps) + pcds
    clx = b"\x01" + struct.pack("<H", 4) + b"\0" * 4 + b"\x02" + struct.pack("<I", len(plc)) + plc   # a Prc, then the piece table
    flags = (0x0200 if table_stream == "1Table" else 0) | (0x0100 if encrypted else 0)
    struct.pack_into("<HH", fib, 0, 0xA5EC, 0x00C1)
    struct.pack_into("<H", fib, 0x0A, flags)
    struct.pack_into("<i", fib, 0x4C, cps[-1])
    struct.pack_into("<II", fib, 0x9A + 8 * 33, 0, len(clx))
    doc = bytes(fib) + bytes(stored)
    return compound_file({"WordDocument": doc.ljust(pad_to, b"\0"), table_stream: clx})
