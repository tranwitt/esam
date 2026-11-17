"""
Document readers: every supported file becomes markdown text.

ESAM's chunker, header reader and traceability reader all work on markdown
(SE manual §9). This module is the one place that knows about file formats:

    .md .markdown .txt      read as they are
    .docx .docm             Word (Office Open XML): built-in reader (word_docx.py)
    .doc                    Word 97-2003: LibreOffice when installed, otherwise
                            the built-in reader (word_doc.py). ESAM_DOC_CONVERTER
                            = auto | libreoffice | builtin
    .rtf                    LibreOffice when installed, otherwise text only
    .png .jpg .jpeg .gif .webp .bmp .tif .tiff .svg
                            a picture is a document of one figure

and pictures INSIDE a Word or markdown document become figure blocks in the
text (esamkit/diagrams.py), so a diagram's content can be found by search.

The file's first bytes decide how it is read, not its name: a .docx saved
with a .doc extension, or an RTF file named .doc, is read as what it is.

    from esamkit.readers import convert, read_document
    result = convert(path)            # text + how it was read + every picture
    text = read_document(path)        # just the text, pictures not described

    python -m esamkit.readers path/to/file.doc      # print the markdown and the report
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import diagrams as _diagrams, office
from .config import Settings, settings as default_settings
from .docmodel import Para, Picture, to_markdown

TEXT = (".md", ".markdown", ".txt")
WORD = (".docx", ".docm", ".doc", ".rtf")
PICTURES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg")
SUPPORTED = TEXT + WORD + PICTURES

_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*<?(?P<src>[^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_ZIP, _OLE, _RTF = b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"{\\rtf"


class UnsupportedDocument(ValueError):
    pass


@dataclass
class Converted:
    text: str                               # markdown
    source_format: str                      # markdown | text | docx | doc | rtf | picture
    converter: str                          # how it was read, in words
    figures: list[dict] = field(default_factory=list)     # one record per picture (diagrams.figure_block)
    notes: list[str] = field(default_factory=list)        # what was lost or assumed
    changed: bool = True                    # False: `text` is exactly the file's own text
    pictures: list = field(default_factory=list, repr=False)      # the Picture objects, in order (not part of report())

    def report(self) -> dict:
        """What the intake review shows the SE about the conversion."""
        return {"source_format": self.source_format, "converter": self.converter, "notes": self.notes,
                "pictures": [{k: v for k, v in f.items() if k != "bytes"} for f in self.figures]}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED


def document_files(folder: Path) -> list[Path]:
    """The documents directly in a folder (not its subfolders, not hidden or
    lock files such as Word's ~$name.docx), sorted by name."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and is_supported(p) and not p.name.startswith((".", "~$")))


def kind_of(path: Path, head: bytes) -> str:
    """docx | doc | rtf | picture | text, from the content first."""
    ext = path.suffix.lower()
    if head.startswith(_OLE):
        return "doc"
    if head.startswith(_RTF):
        return "rtf"
    if head.startswith(_ZIP) and ext in WORD:
        return "docx"
    if ext in PICTURES:
        return "picture"
    if ext in TEXT:
        return "text"
    if ext in WORD:
        looks = "an HTML page" if head.lstrip()[:1] == b"<" else "not a Word file"
        raise UnsupportedDocument(f"{path.name} has a Word extension but is {looks}. Open it in Word or "
                                  "LibreOffice and save it as .docx.")
    raise UnsupportedDocument(f"{path.name}: unsupported document type {ext or '(none)'}. "
                              f"Supported: {', '.join(SUPPORTED)}")


class _Figures:
    """Numbers the pictures of one document and collects their records."""

    def __init__(self, s: Settings, describe: bool):
        self.s, self.mode, self.records, self.pictures = s, (None if describe else "off"), [], []

    def __call__(self, pic: Picture, n: int) -> str:
        text, info = _diagrams.figure_block(pic, n, self.s, self.mode)
        self.records.append(info)
        self.pictures.append(pic)
        return text


def _word(data: bytes, kind: str, s: Settings, figures: _Figures) -> tuple[str, str, list[str]]:
    from .word_doc import DocError, parse_doc
    from .word_docx import DocxError, parse_docx

    notes: list[str] = []
    try:
        if kind == "docx":
            blocks, more = parse_docx(data)
            how = "built-in .docx reader"
        elif kind == "doc":
            if s.doc_converter == "libreoffice" and office.soffice(s) is None:
                raise UnsupportedDocument("ESAM_DOC_CONVERTER=libreoffice but LibreOffice (soffice) is not installed; "
                                          "set ESAM_SOFFICE to its path, or use ESAM_DOC_CONVERTER=auto")
            blocks = None
            if office.soffice(s) is not None:
                try:
                    blocks, more = parse_docx(office.convert(data, ".doc", "docx", s))
                    how = "LibreOffice (.doc to .docx), then the built-in .docx reader"
                except (RuntimeError, DocxError) as e:
                    if s.doc_converter == "libreoffice":
                        raise UnsupportedDocument(f"LibreOffice could not convert the .doc: {e}") from None
                    notes.append(f"LibreOffice could not convert it ({e}); the built-in .doc reader was used")
            if blocks is None:
                blocks, more = parse_doc(data)
                how = "built-in .doc reader"
        else:                                                   # rtf
            if office.soffice(s) is not None:
                try:
                    blocks, more = parse_docx(office.convert(data, ".rtf", "docx", s))
                    return to_markdown(blocks, figures), "LibreOffice (.rtf to .docx), then the built-in .docx reader", more
                except (RuntimeError, DocxError) as e:
                    notes.append(f"LibreOffice could not convert it ({e})")
            blocks = [Para(runs=[(p, False)]) for p in rtf_text(data).split("\n") if p.strip()]
            more, how = ["RTF read as text only (no headings, tables or pictures): install LibreOffice for a full "
                         "conversion"], "built-in RTF text reader"
    except (DocError, DocxError) as e:
        raise UnsupportedDocument(str(e)) from None
    return to_markdown(blocks, figures), how, notes + more


def _markdown(text: str, base: Path, s: Settings, figures: _Figures) -> str:
    """Markdown whose pictures (`![alt](file.png)`) are followed by a figure
    block. Only files inside the document's own folder are read: a document
    cannot point ESAM at /etc/passwd or at a URL."""
    root = base.resolve()
    out, n = [], 0
    for para in re.split(r"(\n\s*\n)", text):
        out.append(para)
        for m in _MD_IMAGE.finditer(para):
            src = m["src"]
            if re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I) or src.startswith("//"):
                continue                                         # a URL or a data: link
            target = (root / src).resolve()
            if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in PICTURES:
                continue
            n += 1
            out.append("\n\n" + figures(Picture(src, target.read_bytes(), alt=m["alt"]), n))
    return "".join(out) if n else text


def convert(path: Path, s: Settings = default_settings, diagrams: bool = True, title: str | None = None) -> Converted:
    """Read any supported document as markdown.

    `diagrams=False` keeps each picture's name, caption and alt text but
    does not look at it (no model call, no OCR): for readers that only need
    the document's structure. `title` names a picture file that is a
    document by itself ("ICD-BRK-002 — Brake bus topology")."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no such document: {path}")
    data = path.read_bytes()
    kind = kind_of(path, data[:16])
    figures = _Figures(s, diagrams)
    if kind == "text":
        original = data.decode("utf-8-sig", "replace")
        text = _markdown(original, path.parent, s, figures) if path.suffix.lower() != ".txt" else original
        return Converted(text, "text" if path.suffix.lower() == ".txt" else "markdown", "read as it is",
                         figures.records, [], changed=text != original or original.encode() != data,
                         pictures=figures.pictures)
    if kind == "picture":
        block = figures(Picture(path.name, data), 1)
        return Converted(f"# {title or path.stem}\n\n{block}\n", "picture", "a picture read as a one-figure document",
                         figures.records, [], pictures=figures.pictures)
    text, how, notes = _word(data, kind, s, figures)
    if not text.strip():
        raise UnsupportedDocument(f"{path.name}: no text could be read from this {kind} file")
    return Converted(text, kind, how, figures.records, notes, pictures=figures.pictures)


def read_document(path: Path, s: Settings = default_settings, diagrams: bool = False) -> str:
    """The document's text as markdown. A plain .md file comes back exactly
    as `path.read_text()` would give it."""
    path = Path(path)
    if path.suffix.lower() in (".md", ".markdown") and not diagrams:
        text = path.read_text()
        if "![" not in text:
            return text
    return convert(path, s, diagrams).text


# ── RTF, text only ───────────────────────────────────────────────────────────

_RTF_TOKEN = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I | re.S)
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "headerl", "headerr",
             "footerl", "footerr", "footnote", "themedata", "colorschememapping", "latentstyles", "datastore",
             "generator", "listtable", "listoverridetable", "rsidtbl", "xmlnstbl", "fldinst", "filetbl", "revtbl",
             "mmathPr", "pgdsctbl", "wgrffmtfilter", "nonshppict", "shpinst", "panose", "falt", "bkmkstart", "bkmkend"}
_RTF_CHARS = {"par": "\n", "sect": "\n", "page": "\n", "line": "\n", "row": "\n", "cell": " | ", "tab": " ",
              "emdash": "\u2014", "endash": "\u2013", "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
              "rdblquote": "\u201d", "bullet": "\u2022", "emspace": " ", "enspace": " "}


def rtf_text(data: bytes) -> str:
    """The plain text of an RTF file: control words dropped, special
    characters and \\'hh / \\uN escapes decoded, tables as ' | ' separated lines."""
    src = data.decode("latin-1")
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending = False, 1, 0          # in a skipped group; \ucN; bytes to drop after a \u
    for m in _RTF_TOKEN.finditer(src):
        word, arg, hexchar, symbol, brace, char = m.groups()
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif symbol:
            if symbol == "*":
                skip = True                  # {\*\destination ...}: optional, ignore it
            elif not skip:
                if pending:
                    pending -= 1
                else:
                    out.append({"~": " ", "_": "-", "-": ""}.get(symbol, symbol if symbol in "\\{}" else ""))
        elif word:
            if word in _RTF_SKIP:
                skip = True
            elif skip:
                pass
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "u" and arg:
                code = int(arg)
                out.append(chr(code + 65536 if code < 0 else code))
                pending = uc
            elif word in _RTF_CHARS:
                out.append(_RTF_CHARS[word])
        elif hexchar:
            if pending:
                pending -= 1
            elif not skip:
                out.append(bytes([int(hexchar, 16)]).decode("cp1252", "replace"))
        elif char and not skip:
            if pending:
                pending -= 1
            else:
                out.append(char)
    text = "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Show a document as ESAM reads it (markdown), and how it was read.")
    ap.add_argument("file")
    ap.add_argument("--no-diagrams", action="store_true", help="do not describe pictures (no model call, no OCR)")
    args = ap.parse_args()
    try:
        res = convert(Path(args.file), diagrams=not args.no_diagrams)
    except (UnsupportedDocument, FileNotFoundError) as e:
        sys.exit(str(e))
    print(res.text)
    print("─" * 78, file=sys.stderr)
    print(json.dumps(res.report(), indent=1, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
