"""
Chunking — cutting documents into retrieval-sized pieces.

Why chunk at all?
  An embedding is ONE vector for a whole input. Embed a 40-page ICD as one
  vector and every topic in it gets averaged into mush: the vector is "about"
  everything, so it's close to nothing in particular. Small chunks keep each
  vector about one thing. Embedding models also have an input limit
  (BGE: 512 tokens, roughly 2,000 characters) and silently cut off anything
  longer.

Why overlap?
  A sentence that straddles a cut would otherwise be split in half and
  belong properly to neither chunk. Overlapping by a little repeats the
  boundary text in both.

This chunker is deliberately simple but avoids two flaws of the earlier
character slicer:
  1. It breaks on paragraph / sentence / word boundaries, never mid-word.
  2. It keeps the nearest markdown heading with each chunk, so a chunk like
     "Transmission rate is 100 ms" still says WHICH section it came from.
"""

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _split_long(text: str, size: int) -> list[str]:
    """Split one oversized paragraph at sentence, then word, boundaries."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(current) + len(sentence) + 1 <= size:
            current = f"{current} {sentence}".strip()
            continue
        if current:
            pieces.append(current)
        # A single sentence longer than `size`: fall back to word boundaries.
        while len(sentence) > size:
            cut = sentence.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        current = sentence
    if current:
        pieces.append(current)
    return pieces


_TABLE_SEP = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")
_CAPTION = re.compile(r"^\W{0,3}(table|tab\.)\s*[\w.\-]+", re.I)


def _is_table(para: str) -> bool:
    lines = para.splitlines()
    return len(lines) >= 2 and all(ln.lstrip().startswith("|") for ln in lines) and bool(_TABLE_SEP.match(lines[1].strip()))


def _cells(row: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]   # "a\|b" is one cell


def _split_table(table: str, size: int, caption: str) -> list[str]:
    """A table longer than `size`, cut between rows only.

    The first piece is the table's head and as many rows as fit. Every later
    piece starts again with the caption (marked "continued") and the header
    rows, so a piece holding only "Seat width | Mismatch | 80 | 63 | 72" still
    says which table, grade and column each number belongs to. Header rows are
    the markdown header, its separator, and the sub-header rows under it (no
    digits, first cell empty or repeating the header's), such as
    "Female | Male | Total" below "Grade 4 | Grade 5". A single row longer
    than `size` is kept whole rather than cut.
    """
    lines = table.splitlines()
    head = lines[:2]
    first = _cells(lines[0])[0] if _cells(lines[0]) else ""
    for row in lines[2:5]:
        c = _cells(row)
        if any(ch.isdigit() for ch in row) or (c and c[0] not in ("", first)):
            break
        head.append(row)
    body = lines[len(head):]
    again = (f"{caption} (continued)\n\n" if caption else "") + "\n".join(head)
    pieces, current = [], "\n".join(head)
    for row in body:
        if current != "\n".join(head) and current != again and len(current) + 1 + len(row) > size:
            pieces.append(current)
            current = again
        current += "\n" + row
    pieces.append(current)
    return pieces


def chunk_markdown(text: str, size: int = 600, overlap: int = 80) -> list[str]:
    """Return chunks of at most ~`size` characters, each prefixed with its
    section heading, with ~`overlap` characters repeated between neighbours."""
    # 1. Walk the document, remembering the current heading for each paragraph.
    #    The label is "document title › section", so a chunk from section
    #    "2. Task set" still says WHICH document's task set it is. Without the
    #    title, that chunk's vector knows nothing about "brake ECU".
    blocks: list[tuple[str, str, str]] = []  # (heading, paragraph, kind: text | table | table+)
    title, heading = "", ""
    previous = ""                             # the paragraph before, for a table's caption
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        m = _HEADING.match(para.splitlines()[0])
        if m:
            if len(m.group(1)) == 1:          # "# Title" -> document title
                title = heading = m.group(2).strip()
            else:                             # "## Section" and deeper
                heading = f"{title} › {m.group(2).strip()}" if title else m.group(2).strip()
            rest = "\n".join(para.splitlines()[1:]).strip()
            if not rest:
                continue
            para = rest
        if _is_table(para):
            caption = previous if _CAPTION.match(previous) and len(previous) < 200 else ""
            if caption and blocks and blocks[-1][1] == caption and blocks[-1][0] == heading:
                blocks.pop()                  # the caption travels with its table, not alone before it
            pieces = _split_table(para, size, caption) if len(para) > size else [para]
            if caption:
                pieces[0] = f"{caption}\n\n{pieces[0]}"
            blocks += [(heading, piece, "table" if i == 0 else "table+") for i, piece in enumerate(pieces)]
        else:
            blocks += [(heading, piece, "text") for piece in (_split_long(para, size) if len(para) > size else [para])]
        previous = para

    # 2. Pack paragraphs into chunks, starting a new chunk when the section
    #    changes or the size limit would be exceeded.
    #    A table continued from the previous piece always starts a chunk of its
    #    own, and no overlap is carried into or out of table rows: half a row
    #    ("…| 15 | |") means nothing, and the continuation already repeats its
    #    caption and header.
    chunks: list[str] = []
    cur_heading, cur_body, cur_kind = None, "", "text"
    for h, para, kind in blocks:
        if cur_body and (kind == "table+" or h != cur_heading or len(cur_body) + len(para) + 2 > size):
            chunks.append(_render(cur_heading, cur_body))
            # Overlap: carry the tail of the previous chunk (whole words only)
            # into the next one — but only within the same section.
            tail = cur_body[-overlap:] if h == cur_heading and cur_kind == "text" and kind != "table+" else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            cur_body = f"…{tail}\n\n{para}" if tail else para
        else:
            cur_body = f"{cur_body}\n\n{para}" if cur_body else para
        cur_heading, cur_kind = h, kind
    if cur_body:
        chunks.append(_render(cur_heading, cur_body))
    return chunks


def _render(heading: str | None, body: str) -> str:
    return f"[{heading}]\n{body}" if heading else body
