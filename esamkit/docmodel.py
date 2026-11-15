"""
What a Word document is reduced to before it becomes markdown.

The .docx reader (word_docx.py) and the .doc reader (word_doc.py) both
produce a list of blocks: paragraphs (with their role: title, heading, list
item, caption, plain text), tables and pictures. `to_markdown` writes them
in the markdown conventions the rest of ESAM reads (SE manual §9):

    # <ID> — <name>                          the document title
    | Document | Type | Revision | Subsystem |   the header table
    ## 1. Section                             numbered sections
    - **SRS-BRK-011** The software shall ...  one requirement per list item
    ## TC-BRK-011 — title  /  Verifies: ...   test cases

so a Word document written the way the manual asks goes through the same
deterministic checks as a markdown one.
"""

import re
from dataclasses import dataclass, field

DOC_ID = r"[A-Z]+-[A-Z0-9]+-\d{3}"
_TITLE = re.compile(rf"^{DOC_ID}\s*[—–-]\s*\S")
# A paragraph that opens with a bold id is a requirement line, bulleted or not.
_LEADING_ID = re.compile(rf"^\*\*(?P<id>{DOC_ID})[:.]?\*\*[\s:.]*(?P<rest>\S.*)$", re.S)
_ATTRIBUTE = re.compile(r"^(Safety|Timing|Verification|Traces to|Basis):\s", re.I)
_CAPTION = re.compile(r"^(Figure|Fig\.|Diagram|Drawing)\s*\d", re.I)
_ORDERED = re.compile(r"^\d+[.)]$")


@dataclass
class Picture:
    name: str                       # file name inside the source, e.g. image1.png
    data: bytes = b""               # empty for a drawing made of Word shapes
    mime: str = ""
    alt: str = ""                   # the author's alt text
    caption: str = ""               # the caption paragraph next to it
    labels: list[str] = field(default_factory=list)   # text inside Word shapes / SmartArt / charts


@dataclass
class Para:
    runs: list[tuple[str, bool]] = field(default_factory=list)   # (text, bold)
    kind: str = "text"              # title | heading | list | caption | text
    level: int = 0                  # heading level 1..9; list depth 0..
    number: str = ""                # automatic number as Word shows it: "1.2", "a)"; "" for a bullet
    pictures: list[Picture] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.runs).strip()


@dataclass
class Table:
    rows: list[list[str]] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)


def _inline(runs: list[tuple[str, bool]]) -> str:
    """Runs as markdown: adjacent runs merged, bold text wrapped in **."""
    merged: list[list] = []
    for text, bold in runs:
        if not text:
            continue
        if merged and merged[-1][1] == bold:
            merged[-1][0] += text
        else:
            merged.append([text, bold])
    out = []
    for text, bold in merged:
        core = text.strip()
        if bold and core:
            lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
            out.append(f"{lead}**{core}**{trail}")
        else:
            out.append(text)
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def _plain(runs: list[tuple[str, bool]]) -> str:
    return re.sub(r"\s+", " ", "".join(t for t, _ in runs)).strip()


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().replace("|", "\\|")


def settle(blocks: list) -> list:
    """Decide what the reader could not know paragraph by paragraph: which
    paragraph is the document title, how heading levels map to markdown, and
    which caption belongs to which picture."""
    paras = [b for b in blocks if isinstance(b, Para) and b.text]
    has_title = any(p.kind == "title" for p in paras)
    top = [p for p in paras if p.kind == "heading" and p.level == 1]
    first = paras[0] if paras else None
    if not has_title and first is not None and _TITLE.match(first.text):
        # No Title style, but the first paragraph reads "<ID> — <name>".
        if not (first.kind == "heading" and first.level == 1 and len(top) == 1):
            first.kind, first.number, has_title = "title", "", True
    if has_title:
        seen = False
        for p in paras:
            if p.kind == "title":
                if seen:                      # a second Title paragraph: a subtitle, not another document
                    p.kind = "text"
                seen = True
            elif p.kind == "heading":
                p.level += 1                  # Title is '#', Heading 1 is '##'
    claimed: set[int] = set()            # a caption belongs to one picture
    for i, b in enumerate(blocks):
        pics = getattr(b, "pictures", None)
        if not pics or pics[-1].caption:
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(blocks) and j not in claimed and isinstance(blocks[j], Para) and not blocks[j].pictures:
                near = blocks[j]
                if near.kind == "caption" or (near.kind == "text" and _CAPTION.match(near.text)):
                    pics[-1].caption = near.text
                    claimed.add(j)
                    break
    return blocks


def to_markdown(blocks: list, figure=None) -> str:
    """Markdown for the blocks. `figure(picture, n)` returns the text that
    stands for the n-th picture (see esamkit/diagrams.py); None leaves
    pictures out."""
    out: list[str] = []              # markdown blocks, joined by a blank line
    last = ""                        # what the previous block was: "req", "list" or ""
    n = 0

    def add_pictures(pictures):
        nonlocal n, last
        for pic in pictures:
            n += 1
            if figure is not None:
                text = figure(pic, n)
                if text:
                    out.append(text)
                    last = ""

    for b in settle(blocks):
        if isinstance(b, Table):
            rows = [[_cell(c) for c in row] for row in b.rows if any(c.strip() for c in row)]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                out.append("\n".join(lines))
                last = ""
            add_pictures(b.pictures)
            continue
        text = _plain(b.runs) if b.kind in ("title", "heading") else _inline(b.runs)
        if not text:
            add_pictures(b.pictures)
            continue
        if b.kind == "title":
            out.append(f"# {text}")
            last = ""
        elif b.kind == "heading":
            number = f"{b.number} " if b.number and not text.startswith(b.number) else ""
            out.append(f"{'#' * min(max(b.level, 1), 6)} {number}{text}")
            last = ""
        else:
            lines = [ln.strip() for ln in "".join(t for t, _ in b.runs).split("\n")]
            multi = len([ln for ln in lines if ln]) > 1
            body = "\n".join(ln for ln in (_inline_lines(b.runs) if multi else [text]) if ln)
            m = _LEADING_ID.match(body)
            if b.kind == "list" or m:
                if m:
                    item, kind = f"- **{m['id']}** {m['rest'].strip()}", "req"
                elif b.number and _ORDERED.match(b.number):
                    item, kind = f"{b.number[:-1]}. {body}", "list"
                else:
                    item, kind = f"- {b.number + ' ' if b.number else ''}{body}", "list"
                item = item.replace("\n", "\n  ")            # continuation lines stay inside the item
                indent = "  " * b.level if kind == "list" else ""
                item = indent + item.replace("\n", "\n" + indent)
                if last == kind and out:
                    out[-1] += "\n" + item                   # a tight list, as in the markdown corpus
                else:
                    out.append(item)
                last = kind
            elif last == "req" and _ATTRIBUTE.match(body) and "\n" not in body:
                out[-1] += "\n  " + body                     # the attribute line under a requirement (§9.4)
            else:
                out.append(body)
                last = ""
        add_pictures(b.pictures)
    return "\n\n".join(out).strip() + "\n"


def _inline_lines(runs: list[tuple[str, bool]]) -> list[str]:
    """A paragraph with line breaks, one markdown line per Word line."""
    lines: list[list[tuple[str, bool]]] = [[]]
    for text, bold in runs:
        parts = text.split("\n")
        lines[-1].append((parts[0], bold))
        for part in parts[1:]:
            lines.append([(part, bold)])
    return [_inline(line) for line in lines]
