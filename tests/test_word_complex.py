"""Complex Word files: pictures the text wraps around, pictures in table cells
and page headers, diagrams drawn with Word shapes, vector pictures.

The two sample documents of the corpus are such files: Word 97-2003 originals
in data/documents/originals/ (written by LibreOffice from the .docx copies in
tests/fixtures/word/, which tests/fixtures/word/make_samples.py builds). Each
is read three ways here and must come out the same: the built-in .doc reader,
the built-in .docx reader, and LibreOffice (.doc to .docx) where installed."""

import io
import struct
import zlib
from dataclasses import replace

import pytest
import yaml

from esamkit import office, word_doc
from esamkit.baseline import load_baseline
from esamkit.config import PROJECT_ROOT, settings
from esamkit.diagrams import figure_block
from esamkit.docmodel import Para, Picture, Table, merge_drawings, reading_order, to_markdown
from esamkit.intake import read_header, receive
from esamkit.ontology import check_baseline, load_ontology
from esamkit.readers import convert
from esamkit.word_doc import parse_doc
from esamkit.word_docx import parse_docx

WORD = PROJECT_ROOT / "tests" / "fixtures" / "word"
ORIGINALS = settings.documents_dir / "originals"
BUILTIN = replace(settings, doc_converter="builtin", diagrams="local", vision_model="")
HAVE_OFFICE = office.soffice(replace(settings, doc_converter="auto", soffice_path="")) is not None

PATHS = [pytest.param(ORIGINALS, ".doc", "builtin", id="doc-builtin"),
         pytest.param(WORD, ".docx", "builtin", id="docx"),
         pytest.param(ORIGINALS, ".doc", "libreoffice", id="doc-libreoffice",
                      marks=pytest.mark.skipif(not HAVE_OFFICE, reason="LibreOffice is not installed"))]


def read(folder, ext, converter, doc_id):
    return convert(folder / f"{doc_id}{ext}", replace(BUILTIN, doc_converter=converter))


def pictures(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("> **Picture")]


def between(text: str, start: str, end: str) -> str:
    return text[text.index(start): text.index(end)]


# ── the two sample documents, every way of reading them ──────────────────────

@pytest.mark.parametrize("folder, ext, converter", PATHS)
def test_pictures_and_drawings_stand_where_they_are_in_the_document(folder, ext, converter):
    res = read(folder, ext, converter, "SSDD-SUSP-001")
    text = res.text
    assert read_header(text) == {"id": "SSDD-SUSP-001", "name": "Suspension/Steering System Design Description",
                                 "type": "ssdd", "revision": "A", "attached_to": "SYS-SUSP", "file": "SSDD-SUSP-001.md"}
    pics = pictures(text)
    assert len(pics) == 6                                        # the header logo is not one of them
    assert "Caption: Figure 1 — Brake system in the vehicle" in pics[0]
    # the floating picture (the text wraps around it) is under its own heading, with its caption
    assert "Caption: Figure 2 — Valve command path" in pics[1]
    assert pics[1] in between(text, "2.1 Hydraulic control", "3. Unit interfaces")
    # the picture in a table cell: the cell says which picture stood there, the picture follows the table
    assert "| Wheel speed inputs | Four variable-reluctance sensors, one per wheel | (Picture 3, below) |" in text
    assert text.index("| J1939 | EBC1 (PGN 61441) every 100 ms | see ICD-BRK-001 |") < text.index(pics[2]) \
        < text.index("4. Fault reporting")
    # three shapes drawn side by side are one drawing, read left to right
    assert "(`drawing1`)" in pics[3] and "Caption: Figure 3 — Fault reporting chain" in pics[3]
    assert "> Text in the picture: Brake Control ECU; J1939 Backbone; Driver Digital Cluster." in text
    # a floating text box stays with the paragraph it is anchored in; no doubled full stop
    assert "(`drawing2`)" in pics[4]
    assert "> Text in the picture: Design note: 100 ms of the budget is the EBC1 period.\n" in text
    assert pics[4] in between(text, "The budget from detection", "EBC1 broadcast period: 100 ms")
    # the same picture used a second time is read a second time, in its place
    assert "Caption: Figure 4 — Figure 1 again, reduced" in pics[5]
    assert text.index("Display software is never in the path") < text.index(pics[5])
    assert not any("placed at the end" in n or "skipped" in n for n in res.notes)
    assert [p["n"] for p in res.report()["pictures"]] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("folder, ext, converter", PATHS)
def test_shapes_drawn_in_rows_are_read_row_by_row(folder, ext, converter):
    """The file holds the four boxes bottom row first and right to left."""
    res = read(folder, ext, converter, "SSDD-PWR-001")
    text = res.text
    assert read_header(text)["attached_to"] == "SYS-PWR"
    pics = pictures(text)
    assert len(pics) == 4                                        # not the logos in the header and the footer
    assert "> Text in the picture: Brake Control ECU; J1939 Backbone; Engine Control Module; Fuel injection." in text
    assert "Caption: Figure 2 — Torque reduction path" in pics[1]
    assert "> Text in the picture: Open point: no interface document states this use." in text
    assert "| Crank speed input | One sensor on the flywheel housing | (Picture 4, below) |" in text
    assert not any("placed at the end" in n for n in res.notes)


def test_the_builtin_doc_reader_says_it_left_the_header_pictures_out():
    for doc_id in ("SSDD-SUSP-001", "SSDD-PWR-001"):
        blocks, notes = parse_doc((ORIGINALS / f"{doc_id}.doc").read_bytes())
        assert "1 picture(s) in the page headers or footers (such as a logo) were left out" in notes
    table = [b for b in blocks if isinstance(b, Table)][-1]
    assert [(p.cell, p.mime) for p in table.pictures] == [((1, 2), "image/png")]


def test_a_floating_picture_is_a_real_picture_file():
    from PIL import Image
    blocks, _ = parse_doc((ORIGINALS / "SSDD-SUSP-001.doc").read_bytes())
    pics = [p for b in blocks for p in b.pictures if p.data]
    assert [p.name for p in pics] == ["picture1.png", "picture2.jpg", "picture3.png", "picture4.png"]
    assert pics[0].data == pics[3].data
    for p in pics:
        assert Image.open(io.BytesIO(p.data)).size[0] > 100


def test_a_drawing_layer_that_cannot_be_followed_still_gives_the_pictures(monkeypatch):
    def broken(*a, **k):
        raise struct.error("cut short")
    monkeypatch.setattr(word_doc, "_DrawingLayer", broken)
    blocks, notes = parse_doc((ORIGINALS / "SSDD-SUSP-001.doc").read_bytes())
    assert any("drawing layer of this document could not be followed" in n for n in notes)
    assert any("placed at the end" in n for n in notes)
    assert blocks[-1].pictures and blocks[-1].pictures[0].mime == "image/jpeg"     # the floating one, found by scanning
    text = to_markdown(blocks)
    assert "5. Design decisions" in text


# ── the drawing layer, record by record ──────────────────────────────────────

def record(kind: int, payload: bytes, instance: int = 0, version: int = 0) -> bytes:
    return struct.pack("<HHI", version | instance << 4, kind, len(payload)) + payload


def metafile(body: bytes, compressed: bool) -> bytes:
    stored = zlib.compress(body) if compressed else body
    head = b"\x11" * 16 + struct.pack("<I", len(body)) + b"\0" * 24 + struct.pack("<I", len(stored))
    return head + bytes([0 if compressed else 0xFE, 0xFE]) + stored


@pytest.mark.parametrize("compressed", [True, False])
def test_vector_pictures_are_taken_out_of_their_wrapping(compressed):
    emf = b"\x01\x00\x00\x00" + b"E" * 36 + b" EMF" + b"x" * 500
    buf = record(0xF01A, metafile(emf, compressed), instance=0x3D4)
    assert word_doc._blips_in(buf, 0, len(buf)) == [("image/x-emf", emf)]
    wmf = b"\xd7\xcd\xc6\x9a" + b"w" * 300
    buf = record(0xF01B, metafile(wmf, compressed), instance=0x216)
    assert word_doc._blips_in(buf, 0, len(buf)) == [("image/x-wmf", wmf)]


def test_a_bitmap_without_a_file_header_becomes_a_bmp_file():
    from PIL import Image
    img = Image.new("RGB", (7, 5), "red")
    out = io.BytesIO()
    img.save(out, "BMP")
    dib = out.getvalue()[14:]                                    # Word stores the bitmap without the 14-byte file header
    buf = record(0xF01F, b"\x22" * 16 + b"\xff" + dib, instance=0x7A8)
    [(mime, data)] = word_doc._blips_in(buf, 0, len(buf))
    assert mime == "image/bmp" and data == out.getvalue()
    assert Image.open(io.BytesIO(data)).getpixel((3, 3)) == (255, 0, 0)


def test_an_inline_vector_picture_is_found_and_a_bare_shape_is_not_called_unreadable():
    emf = b"\x01\x00\x00\x00" + b"E" * 36 + b" EMF" + b"x" * 200
    shape = record(0xF004, record(0xF00A, struct.pack("<II", 1025, 0xA00), instance=75, version=2), version=0xF)
    store_entry = record(0xF007, b"\0" * 36 + record(0xF01A, metafile(emf, True), instance=0x3D4), instance=2, version=2)
    body = shape + store_entry
    picf = struct.pack("<iHh", 68 + len(body), 68, 0x64) + b"\0" * 60 + body
    assert word_doc._inline_picture(b"pad!" + picf, 4) == ([("image/x-emf", emf)], False)
    bare = struct.pack("<iHh", 68 + len(shape), 68, 0x64) + b"\0" * 60 + shape
    assert word_doc._inline_picture(bare, 0) == ([], False)       # a drawn shape: its text comes from its text box
    old = struct.pack("<iHh", 68 + 200, 68, 8) + b"\0" * 60 + b"m" * 200
    assert word_doc._inline_picture(old, 0) == ([], True)         # an old-style metafile: said, not silently dropped
    assert word_doc._inline_picture(b"", 0) == ([], False)


def test_a_vector_picture_in_a_doc_file_keeps_its_place(tmp_path):
    """LibreOffice writes the file; skipped where it is not installed."""
    if not HAVE_OFFICE:
        pytest.skip("LibreOffice is not installed")
    docx = pytest.importorskip("docx")
    import zipfile
    from PIL import Image
    Image.new("RGB", (120, 60), "white").save(tmp_path / "a.png")
    emf = office.convert((tmp_path / "a.png").read_bytes(), ".png", "emf", settings)
    d = docx.Document()
    d.add_paragraph("Before")
    d.add_picture(str(tmp_path / "a.png"))
    d.add_paragraph("After")
    d.save(tmp_path / "p.docx")
    with zipfile.ZipFile(tmp_path / "p.docx") as zin, zipfile.ZipFile(tmp_path / "e.docx", "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("image1.png"):
                zout.writestr("word/media/image1.emf", emf)
                continue
            if item.filename.endswith(".rels"):
                data = data.replace(b"image1.png", b"image1.emf")
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", b'<Default Extension="emf" ContentType="image/x-emf"/></Types>')
            zout.writestr(item, data)
    doc = office.convert((tmp_path / "e.docx").read_bytes(), ".docx", "doc:MS Word 97", settings)
    blocks, notes = parse_doc(doc)
    assert [(b.text, [(p.name, p.mime) for p in b.pictures]) for b in blocks] == \
        [("Before", []), ("", [("picture1.emf", "image/x-emf")]), ("After", [])]
    assert blocks[1].pictures[0].data[40:44] == b" EMF" and notes == []


# ── the document model ───────────────────────────────────────────────────────

def test_reading_order_is_rows_then_columns():
    box = lambda x, y: (x, y, x + 100, y + 40)                   # noqa: E731
    items = [(box(200, 62), "d"), (box(0, 60), "c"), (None, "loose"), (box(210, 5), "b"), (box(0, 0), "a")]
    assert reading_order(items) == ["a", "b", "c", "d", "loose"]
    assert reading_order([(None, "x"), (None, "y")]) == ["x", "y"]


def test_neighbouring_text_only_drawings_become_one_and_pictures_keep_them_apart():
    a = Picture("drawing1", labels=["right"], box=(500, 0, 600, 50))
    b = Picture("drawing2", labels=["left"], box=(0, 0, 100, 50), alt="A chain")
    photo = Picture("image1.png", b"png", "image/png")
    c = Picture("drawing3", labels=["alone"])
    out = merge_drawings([a, b, photo, c])
    assert [(p.name, p.labels) for p in out] == [("drawing1", ["left", "right"]), ("image1.png", []), ("drawing3", ["alone"])]
    assert out[0].alt == "A chain"


def test_a_picture_in_a_table_cell_is_named_in_the_cell():
    table = Table(rows=[["Unit", "Drawing"], ["Pump", ""], ["Valve", "see below"]],
                  pictures=[Picture("a.png", b"x", "image/png", cell=(1, 1)), Picture("b.png", b"y", "image/png", cell=(2, 1)),
                            Picture("c.png", b"z", "image/png")])
    blocks = [Para(runs=[("Intro", False)], pictures=[Picture("first.png", b"f", "image/png")]), table]
    text = to_markdown(blocks, figure=lambda pic, n: f"> **Picture {n}** (`{pic.name}`).")
    assert "| Pump | (Picture 2, below) |" in text and "| Valve | see below (Picture 3, below) |" in text
    assert text.index("| Valve |") < text.index("> **Picture 2** (`a.png`).") < text.index("> **Picture 4** (`c.png`).")
    assert "(Picture" not in to_markdown(blocks)                 # pictures left out: nothing points at them


def test_a_label_that_ends_a_sentence_does_not_get_a_second_full_stop():
    block, _ = figure_block(Picture("drawing1", labels=["Note: check the valve."]), 1, BUILTIN)
    assert block.endswith("> Text in the picture: Note: check the valve.")


def test_docx_shapes_of_one_group_are_read_by_position_not_by_file_order():
    blocks, _ = parse_docx((WORD / "SSDD-PWR-001.docx").read_bytes())
    drawings = [p for b in blocks for p in b.pictures if not p.data]
    assert [p.name for p in drawings] == ["drawing1", "drawing2"]
    assert drawings[0].labels == ["Brake Control ECU", "J1939 Backbone", "Engine Control Module", "Fuel injection"]
    assert drawings[0].alt == "Torque reduction path"


# ── the samples are part of the sample graph ─────────────────────────────────

def test_the_sample_word_documents_are_in_the_baseline_with_their_originals():
    data = yaml.safe_load(settings.baseline_yaml.read_text())
    arts = {a["id"]: a for a in data["artifacts"]}
    assert (arts["SSDD-SUSP-001"]["attached_to"], arts["SSDD-PWR-001"]["attached_to"]) == ("SYS-SUSP", "SYS-PWR")
    assert check_baseline(load_ontology(), data, settings.documents_dir) == []
    base = load_baseline(settings.baseline_yaml)
    edges = {(e.from_eid, e.to_eid) for e in base.edges if e.type == "HAS_ARTIFACT"}
    assert {("SYS-SUSP", "SSDD-SUSP-001"), ("SYS-PWR", "SSDD-PWR-001")} <= edges
    index = base.artifact_index()
    assert index["SSDD-SUSP-001.md"]["wbs_code"] == "1.1.1.5" and index["SSDD-PWR-001.md"]["owner_class"] == "System"
    for doc_id in ("SSDD-SUSP-001", "SSDD-PWR-001"):
        assert (ORIGINALS / f"{doc_id}.doc").read_bytes()[:8] == bytes.fromhex("d0cf11e0a1b11ae1")   # a real Word 97-2003 file
        assert (settings.documents_dir / f"{doc_id}.md").is_file()


@pytest.mark.parametrize("doc_id", ["SSDD-SUSP-001", "SSDD-PWR-001"])
def test_the_markdown_in_the_corpus_is_what_the_original_says(doc_id):
    """Headings, tables and what the drawings say: the same from the original
    read here as in the committed markdown (which LibreOffice and OCR helped write)."""
    kept = (settings.documents_dir / f"{doc_id}.md").read_text()
    now = convert(ORIGINALS / f"{doc_id}.doc", BUILTIN).text
    want = lambda t: [ln for ln in t.splitlines() if ln.startswith(("#", "|", "> Text in the picture:"))]   # noqa: E731
    assert want(now) == want(kept) and len(want(kept)) > 12


def test_a_delivered_doc_file_goes_through_intake(tmp_path):
    got = receive(ORIGINALS / "SSDD-PWR-001.doc", tmp_path / "incoming", BUILTIN)
    assert got["entry"] == {"id": "SSDD-PWR-001", "name": "Power Package/Drive Train System Design Description",
                            "type": "ssdd", "revision": "A", "attached_to": "SYS-PWR", "file": "SSDD-PWR-001.md"}
    assert got["original"] == "incoming/original/SSDD-PWR-001.doc"
    assert got["conversion"]["converter"] == "built-in .doc reader" and len(got["conversion"]["pictures"]) == 4
    assert [p["read_by"][0] for p in got["conversion"]["pictures"][1:3]] == ["text in the drawing"] * 2
