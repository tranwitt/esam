# Word fixtures

Real Word 97-2003 (`.doc`) and Office Open XML (`.docx`) files for `tests/test_readers.py`.
All content is the synthetic SYNTH-LTV programme; nothing here is real programme data.

| File | Made from | Shows |
|---|---|---|
| `STD-BRK-001.docx`, `STD-BRK-001.doc` | `lessons/06_missing_document/staged/STD-BRK-001.md` | a test description: header table, test cases with `Verifies:` lines |
| `SRS-BRK-001.doc` | `data/documents/SRS-BRK-001.md` | requirements as bulleted bold ids, with continuation lines and `Traces to:` attribute lines; system (`SRS-SYS-…`) and software requirements |
| `SRS-PWR-001.docx`, `SRS-PWR-001.doc` | written with python-docx | Title style, header table, bold-id bullets, an attribute line, an embedded PNG with a Caption, a numbered list |
| `ICD-PWR-001.doc` | an HTML page | a hyperlink field, a nested table, a cell with two paragraphs, an empty cell, nested lists, three heading levels |
| `ICD-PWR-002.doc` | written with python-docx | an embedded JPEG and PNG, text outside the Basic Multilingual Plane |

How they were made (LibreOffice 24.2.7, pandoc 3.1.3):

    pandoc -f markdown+hard_line_breaks-smart STD-BRK-001.md -o STD-BRK-001.docx
    soffice --headless --convert-to 'doc:MS Word 97' STD-BRK-001.docx

LibreOffice writes every text piece as UTF-16 and in file order. Word itself writes
8-bit (Windows-1252) pieces where it can and, after editing, pieces out of order;
those cases are built byte by byte in `tests/wordfiles.py`.
