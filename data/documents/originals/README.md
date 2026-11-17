# Documents as delivered

The intake pipeline (Pipeline A) keeps here the Word or picture file a document was converted from, named by
the document id: `<ID>.<ext>`. The markdown it read from the file is `data/documents/<ID>.md`, and that is what
is chunked, embedded and searched.

| File | Document | Attached to | What makes it awkward |
|---|---|---|---|
| `SSDD-SUSP-001.doc` | Suspension/Steering System Design Description | `SYS-SUSP` | logo in the page header, a floating JPEG, a picture in a table cell, a three-box diagram drawn with Word shapes, a floating text box, one picture used twice |
| `SSDD-PWR-001.doc` | Power Package/Drive Train System Design Description | `SYS-PWR` | logo in header and footer, a two-by-two diagram drawn with Word shapes (stored bottom row first), a floating text box, a bitmap in a table cell |

Both are synthetic (programme SYNTH-LTV is fictional), Word 97-2003 files written by LibreOffice; how they
were made is in `tests/fixtures/word/README.md`.

    python -m esamkit.readers data/documents/originals/SSDD-SUSP-001.doc      # the markdown ESAM reads from it

The committed markdown was read with LibreOffice installed and `tesseract` for the text inside pictures
(`ESAM_DIAGRAMS=auto`, no vision model). With a vision model configured, each picture gets a description
instead of the OCR line.
