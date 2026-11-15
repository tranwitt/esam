"""Document readers: Word (.docx, .doc), RTF, markdown and pictures all become
the markdown the rest of ESAM reads.

The .doc files in tests/fixtures/word/ are real Word 97-2003 files written by
LibreOffice 24.2 (how: tests/fixtures/word/README.md); tests/wordfiles.py
builds the cases LibreOffice never writes (8-bit text pieces, pieces out of
order, the mini stream)."""

import shutil
from dataclasses import replace

import pytest

import wordfiles as wf
from esamkit import office
from esamkit.config import PROJECT_ROOT, settings
from esamkit.docmodel import to_markdown
from esamkit.intake import read_header
from esamkit.readers import (SUPPORTED, UnsupportedDocument, convert, document_files, read_document, rtf_text)
from esamkit.traceability import read_requirements, read_test_cases
from esamkit.word_doc import CompoundFile, DocError, carve, parse_doc, sprms
from esamkit.word_docx import DocxError, parse_docx

WORD = PROJECT_ROOT / "tests" / "fixtures" / "word"
STAGED_STD = PROJECT_ROOT / "lessons" / "06_missing_document" / "staged" / "STD-BRK-001.md"
BUILTIN = replace(settings, doc_converter="builtin", diagrams="off", vision_model="")
HAVE_OFFICE = office.soffice(replace(settings, doc_converter="auto", soffice_path="")) is not None


def facts(text: str, doc_id: str):
    """What traceability reads out of a document: requirements and test cases."""
    reqs, tcs = read_requirements(text, doc_id, "SUB"), read_test_cases(text, doc_id)
    return ([(v.props["id"], v.props["text"], v.props["section"]) for v in reqs.vertices],
            [(v.props["id"], v.props["name"], v.props["method"], v.props["verifies"], v.props["pass_criteria"])
             for v in tcs.vertices])


# ── .doc: real files ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, source", [("STD-BRK-001.doc", STAGED_STD), ("STD-BRK-001.docx", STAGED_STD),
                                          ("SRS-BRK-001.doc", settings.documents_dir / "SRS-BRK-001.md")])
def test_a_word_copy_of_a_corpus_document_reads_the_same_as_its_markdown(name, source):
    """The same header, requirements and test cases, word for word."""
    text = convert(WORD / name, BUILTIN).text
    doc_id = source.stem
    assert read_header(text) == read_header(source.read_text())
    assert facts(text, doc_id) == facts(source.read_text(), doc_id)
    assert sum(len(x) for x in facts(text, doc_id)) in (4, 9)     # 4 test cases, or 9 requirements


def test_doc_structure_title_table_bold_ids_attributes_picture():
    res = convert(WORD / "SRS-PWR-001.doc", replace(BUILTIN, diagrams="local"))
    assert res.source_format == "doc" and res.converter == "built-in .doc reader"
    lines = res.text.splitlines()
    assert lines[0] == "# SRS-PWR-001 — Power Distribution Unit Software Requirements"
    assert "| Document | Type | Revision | Subsystem |" in lines and "| SRS-PWR-001 | SRS | A | SUB-PWR-PDU |" in lines
    assert "## 1. Control requirements" in lines and "### 2.1 Modes" in lines
    assert read_header(res.text) == {"id": "SRS-PWR-001", "name": "Power Distribution Unit Software Requirements",
                                     "type": "srs", "revision": "A", "attached_to": "SUB-PWR-PDU", "file": "SRS-PWR-001.md"}
    reqs = read_requirements(res.text, "SRS-PWR-001", "SUB-PWR-PDU").vertices
    assert [v.props["id"] for v in reqs] == ["SRS-PWR-010", "SRS-PWR-011"]
    assert reqs[1].props["text"].endswith("every 1 s ± 100 ms — “as measured”.")      # non-Latin-1 text survives
    assert reqs[1].props["safety"] == "SwCI 3" and reqs[1].props["timing"] == "50 ms max"   # the attribute line
    [fig] = res.figures
    assert fig["type"] == "image/png" and fig["caption"] == "Figure 1 — Load-shedding path" and fig["bytes"] > 1000
    assert "> **Picture 1** (`picture1.png`). Caption: Figure 1 — Load-shedding path." in res.text
    assert any("automatic numbers" in n for n in res.notes)       # the numbered list lost its numbers, and says so


def test_doc_fields_nested_tables_and_lists():
    text = convert(WORD / "ICD-PWR-001.doc", BUILTIN).text
    assert "with a link to the spec and **bold words** inside." in text      # the hyperlink's text, not its field code
    assert "HYPERLINK" not in text and "example.org" not in text
    assert "| PGN | Name | Rate |\n|---|---|---|\n| 65271 | VEP1 Vehicle electrical power | 1 s |\n| 61441 | EBC1 |  |" in text
    assert "n1 n2" in text and "\nAfter the table.\n" in text                 # the nested table's cells; the table ended
    assert "- Connect the harness\n- Apply 24 V" in text and "\n  - nested bullet" in text
    assert "## Topology" in text and "### Deep heading" in text


def test_doc_pictures_png_and_jpeg_and_text_outside_the_bmp():
    res = convert(WORD / "ICD-PWR-002.doc", BUILTIN)
    assert [(f["name"], f["type"]) for f in res.figures] == [("picture1.jpg", "image/jpeg"), ("picture2.png", "image/png")]
    assert res.figures[0]["caption"] == "Figure 1 — Bus topology" and res.figures[1]["caption"] == ""
    assert "Emoji test 😀 and CJK 汉字 and symbols ≤ µ Ω." in res.text
    assert res.text.index("Picture 1") < res.text.index("Figure 1 — Bus topology") < res.text.index("Picture 2")


@pytest.mark.skipif(not HAVE_OFFICE, reason="LibreOffice is not installed")
def test_doc_through_libreoffice_agrees_with_the_built_in_reader():
    auto = replace(BUILTIN, doc_converter="auto")
    for name in ("STD-BRK-001.doc", "SRS-BRK-001.doc"):
        full, own = convert(WORD / name, auto), convert(WORD / name, BUILTIN)
        assert full.converter.startswith("LibreOffice") and own.converter == "built-in .doc reader"
        assert facts(full.text, "X") == facts(own.text, "X") and read_header(full.text) == read_header(own.text)
    full = convert(WORD / "SRS-PWR-001.doc", auto)
    assert "1. First step\n2. Second step" in full.text and full.notes == []   # LibreOffice keeps the automatic numbers


def test_doc_converter_setting():
    none = replace(BUILTIN, doc_converter="libreoffice", soffice_path="/nonexistent/soffice")
    with pytest.raises(UnsupportedDocument, match="LibreOffice .* is not installed"):
        convert(WORD / "STD-BRK-001.doc", none)
    assert office.soffice(BUILTIN) is None                         # builtin never starts another program


# ── .doc: the cases Word writes and LibreOffice does not ─────────────────────

PIECES = [("ICD-X-001 — Tïtle 漢\r", False),                    # UTF-16
          ("Café costs € 5; see \x13 HYPERLINK \"http://x\" \x14the link\x15 now.\r", True),   # 8-bit, with a field
          ("A\x07B\x07\x07C\x07D\x07\x07", True),                              # a 2 x 2 table
          ("After the table.\x0b2nd line\r", False)]
EXPECTED = ("# ICD-X-001 — Tïtle 漢\n\nCafé costs € 5; see the link now.\n\n| A | B |\n|---|---|\n| C | D |\n\n"
            "After the table.\n2nd line\n")


@pytest.mark.parametrize("kw", [{}, {"table_stream": "1Table", "pad_to": 5000}], ids=["mini-stream", "regular-stream"])
def test_doc_piece_table_8bit_and_utf16_pieces_stored_out_of_order(kw):
    blocks, notes = parse_doc(wf.make_doc(PIECES, **kw))
    assert to_markdown(blocks) == EXPECTED
    assert any("paragraph properties" in n for n in notes)


def test_doc_refuses_what_it_cannot_read_and_says_why(tmp_path):
    with pytest.raises(DocError, match="password-protected"):
        parse_doc(wf.make_doc(PIECES, encrypted=True))
    with pytest.raises(DocError, match="not a Word document"):
        parse_doc(wf.compound_file({"Workbook": b"x" * 5000}))           # an .xls renamed .doc
    with pytest.raises(DocError, match="not a Word .doc file"):
        parse_doc(b"plain text, not OLE" * 40)
    truncated = (WORD / "STD-BRK-001.doc").read_bytes()[:3000]
    with pytest.raises((DocError, UnsupportedDocument)):
        (tmp_path / "cut.doc").write_bytes(truncated)
        convert(tmp_path / "cut.doc", BUILTIN)


def test_compound_file_streams_and_property_lists():
    cf = CompoundFile(wf.compound_file({"small": b"abc" * 100, "big": bytes(range(256)) * 20, "empty-ish": b"z"}))
    assert cf.stream("small") == b"abc" * 100 and cf.stream("big") == bytes(range(256)) * 20
    assert cf.stream("empty-ish") == b"z" and cf.stream("missing") is None
    # sprmPFInTable=1, sprmPIlfo=3, a 4-byte one, a variable-length one, then sprmCFBold=1
    grpprl = (b"\x16\x24\x01" + b"\x0b\x46\x03\x00" + b"\x03\x6a\x10\x00\x00\x00" + b"\x15\xc6\x03\x01\x02\x03"
              + b"\x35\x08\x01")
    assert sprms(grpprl) == {0x2416: b"\x01", 0x460B: b"\x03\x00", 0x6A03: b"\x10\x00\x00\x00", 0xC615: b"\x01\x02\x03",
                             0x0835: b"\x01"}
    assert sprms(b"\x16\x24") == {}                                # cut short: no crash, nothing invented


def test_carve_finds_embedded_png_and_jpeg():
    jpeg = b"\xff\xd8\xff\xe0" + b"J" * 400 + b"\xff\xd9"
    blob = b"junk" + wf.png() + b"\x00" * 9 + jpeg + b"tail\xff\xd8\xff\x00not a jpeg"
    found = carve(blob)
    assert [(m, d) for _, m, d in found] == [("image/png", wf.png()), ("image/jpeg", jpeg)]


# ── .docx ────────────────────────────────────────────────────────────────────

BODY = (wf.p("SRS-X-001 — Title", "Title") + wf.p("Contents line 1 ...... 3", "TOC1")
        + wf.table([["Document", "Type", "Revision", "Subsystem"], ["SRS-X-001", "SRS", "B", "SUB-BRK-ECU"]])
        + wf.p("Scope", "Heading1", num=(3, 0)) + wf.p("Sub scope", "Berschrift2", num=(3, 1))
        + wf.p(" The software shall do x within 5 ms.", bold_prefix="SRS-X-010", num=(1, 0))
        + wf.p("Safety: SwCI 2 | Verification: T")
        + wf.p(" The software shall do y.", bold_prefix="SRS-X-011:")                     # bold id, no bullet, a colon
        + wf.p("Tests", "Heading1", num=(3, 0))
        + wf.p("one", num=(2, 0)) + wf.p("sub", num=(2, 1)) + wf.p("two", num=(2, 0))
        + wf.p("Verifies: SRS-X-010.\nMethod: Test.")
        + wf.p("", extra=wf.picture("rId1", "alt words")) + wf.p("Figure 1 — Cap", "Caption")
        + wf.p("", extra=wf.shapes("Brake ECU", "Cluster")))


def test_docx_structure_numbering_and_pictures(tmp_path):
    (tmp_path / "x.docx").write_bytes(wf.make_docx(BODY, {"image1.png": wf.png()}))
    res = convert(tmp_path / "x.docx", replace(BUILTIN, diagrams="local"))
    assert res.converter == "built-in .docx reader" and res.source_format == "docx"
    text = res.text
    assert text.startswith("# SRS-X-001 — Title\n\n| Document | Type | Revision | Subsystem |")
    assert "Contents line" not in text                                    # the table of contents repeats the headings
    assert "## 1 Scope" in text and "### 1.1 Sub scope" in text and "## 2 Tests" in text   # Word's automatic numbers
    assert "- **SRS-X-010** The software shall do x within 5 ms.\n  Safety: SwCI 2 | Verification: T" in text
    assert "- **SRS-X-011** The software shall do y." in text
    assert "1. one\n  - a) sub\n2. two" in text
    assert "Verifies: SRS-X-010.\nMethod: Test." in text                 # a line break stays a line break
    assert read_header(text)["revision"] == "B" and read_header(text)["attached_to"] == "SUB-BRK-ECU"
    reqs = read_requirements(text, "SRS-X-001", "SUB-BRK-ECU").vertices
    assert [(v.props["id"], v.props["section"]) for v in reqs] == [("SRS-X-010", "1.1 Sub scope"), ("SRS-X-011", "1.1 Sub scope")]
    assert reqs[0].props["safety"] == "SwCI 2"
    pic, drawing = res.figures
    assert (pic["name"], pic["type"], pic["caption"]) == ("image1.png", "image/png", "Figure 1 — Cap")
    assert "Alt text: alt words." in text
    assert drawing["read_by"] == ["text in the drawing"] and "> Text in the picture: Brake ECU; Cluster." in text


def test_docx_heading_levels_without_a_title_style(tmp_path):
    body = wf.p("ICD-X-002 — Bus", "Heading1") + wf.p("Messages", "Heading2") + wf.p("text")
    assert to_markdown(parse_docx(wf.make_docx(body))[0]) == "# ICD-X-002 — Bus\n\n## Messages\n\ntext\n"
    body = wf.p("ICD-X-002 — Bus") + wf.p("Messages", "Heading1") + wf.p("More", "Heading1")
    assert to_markdown(parse_docx(wf.make_docx(body))[0]) == "# ICD-X-002 — Bus\n\n## Messages\n\n## More\n"


def test_docx_tracked_changes_and_table_pipes():
    body = (f'<w:p><w:r><w:t>kept </w:t></w:r><w:ins><w:r><w:t>inserted</w:t></w:r></w:ins>'
            f'<w:del><w:r><w:delText>deleted</w:delText></w:r></w:del></w:p>' + wf.table([["a|b", "c"], ["d", ""]]))
    assert to_markdown(parse_docx(wf.make_docx(body))[0]) == "kept inserted\n\n| a\\|b | c |\n|---|---|\n| d |  |\n"


def test_docx_refuses_other_zips_and_dtds():
    import io
    import zipfile
    with pytest.raises(DocxError, match="not a zip"):
        parse_docx(b"PK\x03\x04 but not really")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(DocxError, match="no main document part"):
        parse_docx(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<!DOCTYPE d [<!ENTITY a "aaaa">]><w:document xmlns:w="%s"><w:body/></w:document>' % wf.W)
    with pytest.raises(DocxError, match="DTD"):
        parse_docx(buf.getvalue())


# ── what the file IS decides, not its name ───────────────────────────────────

def test_content_decides_how_a_file_is_read(tmp_path):
    docx = wf.make_docx(wf.p("ICD-X-003 — Renamed", "Title") + wf.p("body"))
    (tmp_path / "renamed.doc").write_bytes(docx)                             # a .docx saved as .doc
    assert convert(tmp_path / "renamed.doc", BUILTIN).source_format == "docx"
    (tmp_path / "word.rtf").write_bytes((WORD / "STD-BRK-001.doc").read_bytes())
    assert convert(tmp_path / "word.rtf", BUILTIN).source_format == "doc"
    (tmp_path / "page.doc").write_text("<html><body><p>exported</p></body></html>")
    with pytest.raises(UnsupportedDocument, match="HTML page"):
        convert(tmp_path / "page.doc", BUILTIN)
    (tmp_path / "sheet.xlsx").write_bytes(b"PK\x03\x04")
    with pytest.raises(UnsupportedDocument, match="unsupported document type .xlsx"):
        convert(tmp_path / "sheet.xlsx", BUILTIN)
    with pytest.raises(FileNotFoundError):
        convert(tmp_path / "nope.docx", BUILTIN)


def test_rtf_text_only(tmp_path):
    emdash = "\\" + "u8212?"                                  # the RTF escape for an em dash
    rtf = (r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}{\*\generator Word;}"
           r"\pard\b ICD-X-004 " + emdash + r" Rich text\b0\par Caf\'e9 \ldblquote quoted\rdblquote  and {\i italic}\par"
           r" A\cell B\cell\row\pard done\par}").encode()
    text = rtf_text(rtf)
    assert text.splitlines()[:2] == ["ICD-X-004 — Rich text", "Café “quoted” and italic"]
    assert "A | B |" in text and "Arial" not in text and "generator" not in text
    (tmp_path / "x.rtf").write_bytes(rtf)
    res = convert(tmp_path / "x.rtf", BUILTIN)
    assert res.source_format == "rtf" and res.text.startswith("# ICD-X-004 — Rich text") and res.notes


# ── markdown, text, folders ──────────────────────────────────────────────────

def test_plain_markdown_is_returned_untouched():
    for f in settings.documents_dir.glob("*.md"):
        res = convert(f, BUILTIN)
        assert res.text == f.read_text() == read_document(f) and not res.changed and res.figures == []


def test_markdown_pictures_get_a_figure_block_but_only_from_the_documents_folder(tmp_path):
    docs = tmp_path / "docs"
    (docs / "img").mkdir(parents=True)
    (docs / "img" / "topo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>Brake bus</title>'
                                           '<text x="1" y="1">BRAKE ECU</text><text><tspan>EBC1</tspan> 100 ms</text></svg>')
    (tmp_path / "secret.png").write_bytes(wf.png())
    md = ("# ICD-X-005 — Bus\n\nSee ![the topology](img/topo.svg \"t\") here.\n\n"
          "![outside](../secret.png) ![remote](https://example.org/x.png) ![missing](img/none.png)\n\nEnd.\n")
    (docs / "ICD-X-005.md").write_text(md)
    res = convert(docs / "ICD-X-005.md", replace(BUILTIN, diagrams="local"))
    assert res.changed and [f["name"] for f in res.figures] == ["img/topo.svg"]           # nothing outside, nothing remote
    assert ("here.\n\n> **Picture 1** (`img/topo.svg`). Alt text: the topology.\n"
            "> Text in the picture: Brake bus; BRAKE ECU; EBC1 100 ms.\n\n![outside]") in res.text
    assert res.text.replace(res.text[res.text.index("\n\n> **Picture"):res.text.index("\n\n![outside]")], "") == md
    assert "Text in the picture" not in read_document(docs / "ICD-X-005.md")              # structure readers don't look


def test_a_picture_file_is_a_document_of_one_figure(tmp_path):
    (tmp_path / "topology.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>CLUSTER</text></svg>')
    res = convert(tmp_path / "topology.svg", replace(BUILTIN, diagrams="local"), title="ICD-X-006 — Cluster wiring")
    assert res.source_format == "picture" and read_header(res.text)["id"] == "ICD-X-006"
    assert res.text == ("# ICD-X-006 — Cluster wiring\n\n> **Picture 1** (`topology.svg`).\n"
                        "> Text in the picture: CLUSTER.\n")


def test_document_files_lists_supported_files_only(tmp_path):
    for name in ("b.docx", "a.md", "c.DOC", "d.png", "~$b.docx", ".hidden.md", "notes.xlsx", "e.txt"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "originals").mkdir()
    (tmp_path / "originals" / "z.docx").write_bytes(b"x")
    assert [p.name for p in document_files(tmp_path)] == ["a.md", "b.docx", "c.DOC", "d.png", "e.txt"]
    assert document_files(tmp_path / "nope") == [] and {".doc", ".docx", ".md", ".png", ".svg"} <= set(SUPPORTED)


def test_quoted_lines_are_not_test_case_fields():
    text = ("## TC-X-001 — t\n\nVerifies: SRS-X-010.\nMethod: Test.\nPass criteria: under 5 ms.\n\n"
            "> **Picture 1** (`p.png`).\n> Diagram description (m): a long story about SRS-X-999.\n")
    [tc] = read_test_cases(text, "STD-X-001").vertices
    assert tc.props["pass_criteria"] == "under 5 ms." and tc.props["verifies"] == ["SRS-X-010"]


def test_the_lesson_and_the_ontology_check_see_word_files(tmp_path):
    import yaml
    from esamkit.ontology import check_baseline, default_ontology
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(WORD / "STD-BRK-001.doc", docs / "STD-BRK-001.doc")
    data = yaml.safe_load(settings.baseline_yaml.read_text())
    msgs = [str(f) for f in check_baseline(default_ontology(), data, docs)]
    assert any("STD-BRK-001.doc" in m and "not listed in the baseline" in m for m in msgs)
