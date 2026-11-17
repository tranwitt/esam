"""
Traceability: requirements and test cases as vertices of the graph.

The baseline lists DOCUMENTS. Inside a specification are the requirements;
inside an STD are the test cases, each with a `Verifies:` line (SE manual
§9). This module reads them out, deterministically (no AI), and turns them
into:

    Requirement ──SPECIFIED_IN──► DesignArtifact (its SSS or SRS)
    Requirement ──ALLOCATED_TO──► the document's owner
    Requirement ──TRACES_TO─────► Requirement    (its parent, from `Traces to:`)
    TestCase    ──SPECIFIED_IN──► DesignArtifact (its STD)
    TestCase    ──VERIFIES──────► Requirement

SYSTEM and SOFTWARE requirements are kept apart. Which one a requirement is
comes from the kind of document it is stated in, as the ontology says
(document_types.<type>.requirements), never from its wording:

    SSS  System/Subsystem Specification        level: system     ids SSS-…
    SRS  Software Requirements Specification   level: software   ids SRS-…

Every Requirement carries `level`. A system requirement is allocated to
what its SSS is attached to (the vehicle, a system or a subsystem); a
software requirement to its SRS's subsystem. A software requirement whose
`Traces to:` names a system requirement gets a TRACES_TO link to it, which
is how "which system requirements has the software not picked up?" is
answered.

which answers "which requirements have no test?" and "what verifies
SRS-BRK-011?" with a graph query instead of a guess.

Every extracted vertex records `source_artifact` (the document it came
from), so reloading a document first removes what its previous revision
contributed: a deleted requirement doesn't linger.

The formats it reads are the ones the manual asks SEs to write:

    - **SSS-LTV-010** The vehicle shall prevent wheel lock-up ...        (in an SSS)

    - **SRS-BRK-011** The software shall release brake pressure ...      (in an SRS)
      (continuation lines indented)
      Verification: T | Traces to: SSS-LTV-010

    ## TC-BRK-011 — Pressure release time on incipient wheel lock-up
    Verifies: SRS-BRK-011.
    Method: Test.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .baseline import Baseline, Edge, Vertex
from .graphstores.base import GraphStore
from .readers import read_document

_REQ_LINE = re.compile(r"^- \*\*(?P<id>[A-Z]+-[A-Z0-9]+-\d{3})\*\*\s*(?P<text>.*)$")
_SECTION = re.compile(r"^#{2,6}\s+(?P<title>.+)$")
_TC_HEAD = re.compile(r"^#{2,6}\s+(?P<id>TC-[A-Z0-9]+-\d{3})\s*[—–-]\s*(?P<title>.+)$")
_FIELD = re.compile(r"^(?P<key>Verifies|Method|Steps|Pass criteria):\s*(?P<value>.*)$", re.I)
_ID = re.compile(r"\b[A-Z]+-[A-Z0-9]+-\d{3}\b")
# The attribute line under a requirement (SE manual §9.4):
#   Safety: SwCI 2 | Timing: 20 ms max | Verification: T | Traces to: SSF-ENG-01 | Basis: MIL-STD-882E 4.4.2
_ATTR = re.compile(r"^(?P<key>Safety|Timing|Verification|Traces to|Basis):\s*(?P<value>.*)$", re.I)
_LISTS = {"traces_to", "basis", "verification"}


def read_attributes(line: str) -> dict | None:
    """The attributes on one attribute line, or None if it isn't one."""
    parts = [p.strip() for p in line.strip().split("|")]
    found = [_ATTR.match(p) for p in parts]
    if not parts or not all(found):
        return None
    out = {}
    for m in found:
        key, value = m["key"].lower().replace(" ", "_"), m["value"].strip().rstrip(".")
        out[key] = [v.strip() for v in re.split(r"[,;]", value) if v.strip()] if key in _LISTS else value
    return out


@dataclass
class Extracted:
    vertices: list[Vertex] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _short(text: str, n: int = 90) -> str:
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return first if len(first) <= n else first[: n - 1].rstrip() + "…"


DEFAULT_LEVELS = {"sss": "system", "srs": "software"}       # used when the ontology does not say


def requirement_levels() -> dict[str, str]:
    """{document type: level} from the ontology: which documents state
    requirements, and whether they are system or software requirements."""
    try:
        from .ontology import default_ontology
        return default_ontology().requirement_levels() or dict(DEFAULT_LEVELS)
    except Exception:
        return dict(DEFAULT_LEVELS)


def id_prefix(requirement_id: str) -> str:
    return requirement_id.split("-", 1)[0].lower()


def level_of_id(requirement_id: str, levels: dict[str, str] | None = None) -> str | None:
    """The level a requirement id announces (SSS-… system, SRS-… software), or None."""
    return (levels or requirement_levels()).get(id_prefix(requirement_id))


def misplaced_requirements(text: str, doc_type: str, levels: dict[str, str] | None = None) -> list[dict]:
    """Requirement lines whose id belongs to the OTHER level's document: a
    system requirement id (SSS-…) written as a requirement in an SRS, or the
    reverse. They are not read as this document's requirements.
    [{id, level, belongs_in}]"""
    levels = levels or requirement_levels()
    out = []
    for line in text.splitlines():
        m = _REQ_LINE.match(line)
        if m and id_prefix(m["id"]) != doc_type and id_prefix(m["id"]) in levels:
            out.append({"id": m["id"], "level": levels[id_prefix(m["id"])], "belongs_in": id_prefix(m["id"]).upper()})
    return out


def read_requirements(text: str, artifact_id: str, subsystem: str, level: str = "software",
                      doc_type: str | None = None, levels: dict[str, str] | None = None) -> Extracted:
    """The requirements stated in one document, each marked with `level`
    ("system" for an SSS, "software" for an SRS). `subsystem` is what the
    document is attached to. With `doc_type` given, a line carrying the other
    level's id (see misplaced_requirements) is left out: one requirement has
    one level and one home."""
    out, section, cur = Extracted(), "", None
    foreign = {p for p in (levels or requirement_levels()) if p != doc_type} if doc_type else set()

    def close():
        if cur:
            body = " ".join(cur["lines"]).strip()
            out.vertices.append(Vertex("Requirement", {
                "id": cur["id"], "name": _short(body), "text": body, "level": level, "section": cur["section"],
                "source_artifact": artifact_id, "subsystem": subsystem, **cur["attrs"]}))
            out.edges += [Edge(cur["id"], artifact_id, "SPECIFIED_IN"), Edge(cur["id"], subsystem, "ALLOCATED_TO")]
            # A parent that is written as a requirement id; kept only if that requirement exists (load_traceability).
            out.edges += [Edge(cur["id"], parent, "TRACES_TO") for parent in dict.fromkeys(
                p for ref in cur["attrs"].get("traces_to", []) for p in _ID.findall(ref)) if parent != cur["id"]]

    for line in text.splitlines():
        if m := _SECTION.match(line):
            close(); cur = None
            section = m["title"].strip()
        elif (m := _REQ_LINE.match(line)) and id_prefix(m["id"]) in foreign:
            close(); cur = None                           # the other level's requirement: not this document's
        elif m := _REQ_LINE.match(line):
            close()
            cur = {"id": m["id"], "lines": [m["text"]], "section": section, "attrs": {}}
        elif cur and line.startswith("  ") and line.strip():
            attrs = read_attributes(line)
            if attrs is not None:
                cur["attrs"].update(attrs)
            else:
                cur["lines"].append(line.strip())
        elif cur and not line.strip():
            close(); cur = None
    close()
    return out


def read_test_cases(text: str, artifact_id: str) -> Extracted:
    out, cur = Extracted(), None

    def close():
        if cur:
            refs = _ID.findall(cur["fields"].get("verifies", ""))
            out.vertices.append(Vertex("TestCase", {
                "id": cur["id"], "name": cur["title"], "method": cur["fields"].get("method", "").rstrip("."),
                "verifies": refs, "pass_criteria": cur["fields"].get("pass criteria", ""),
                "source_artifact": artifact_id}))
            out.edges.append(Edge(cur["id"], artifact_id, "SPECIFIED_IN"))
            out.edges += [Edge(cur["id"], r, "VERIFIES") for r in refs]

    key = None
    for line in text.splitlines():
        if m := _TC_HEAD.match(line):
            close()
            cur, key = {"id": m["id"], "title": m["title"].strip(), "fields": {}}, None
        elif _SECTION.match(line):
            close(); cur = None
        elif line.lstrip().startswith(">"):
            continue                                      # a quoted note or a figure block: not part of a field
        elif cur and (m := _FIELD.match(line.strip())):
            key = m["key"].lower()
            cur["fields"][key] = m["value"].strip()
        elif cur and key and line.strip():
            cur["fields"][key] += " " + line.strip()      # a field continued on the next line
    close()
    return out


def extract(baseline: Baseline, documents_dir: Path, only: set[str] | None = None) -> Extracted:
    """Requirements from every specification (system requirements from an
    SSS, software requirements from an SRS) and test cases from every STD the
    baseline lists (or only the artifact ids in `only`)."""
    out, levels = Extracted(), requirement_levels()
    for rel, facts in baseline.artifact_index().items():
        if only is not None and facts["artifact_id"] not in only:
            continue
        path = documents_dir / rel
        if not path.exists():
            continue
        t = facts["artifact_type"]
        if t in levels:
            part = read_requirements(read_document(path), facts["artifact_id"], facts["subsystem_id"],
                                     levels[t], t, levels)
        elif t == "std":
            part = read_test_cases(read_document(path), facts["artifact_id"])
        else:
            continue
        out.vertices += part.vertices
        out.edges += part.edges
    return out


def load_traceability(graph: GraphStore, baseline: Baseline, documents_dir: Path,
                      only: set[str] | None = None) -> dict:
    """Replace what the given documents contribute to the graph. Edges to a
    requirement that doesn't exist (a test that cites a typo) are not stored:
    the TestCase keeps the reference in `verifies`, and `unresolved` reports it.
    Returns counts plus the unresolved references."""
    ex = extract(baseline, documents_dir, only)
    sources = {v.props["source_artifact"] for v in ex.vertices} | (only or set())
    for src in sources:
        graph.remove_derived(src)
    known = ({v.eid for v in baseline.vertices} | {v.eid for v in ex.vertices}
             | graph.existing_ids("Requirement") | graph.existing_ids("DesignArtifact"))
    requirement_ids = {v.eid for v in ex.vertices if v.label == "Requirement"} | graph.existing_ids("Requirement")
    # TRACES_TO only between requirements: a parent that is a document, a safety function or a typo is not a link.
    edges = [e for e in ex.edges if e.to_eid in known and (e.type != "TRACES_TO" or e.to_eid in requirement_ids)]
    unresolved = [{"test_case": e.from_eid, "cites": e.to_eid} for e in ex.edges
                  if e.type == "VERIFIES" and e.to_eid not in known]
    graph.load(Baseline(metadata={}, vertices=ex.vertices, edges=edges, artifacts=[]))
    by_level = lambda level: sum(v.label == "Requirement" and v.props.get("level") == level for v in ex.vertices)  # noqa: E731
    return {"requirements": sum(v.label == "Requirement" for v in ex.vertices),
            "system_requirements": by_level("system"), "software_requirements": by_level("software"),
            "traces": sum(e.type == "TRACES_TO" for e in edges),
            "test_cases": sum(v.label == "TestCase" for v in ex.vertices),
            "verifies": sum(e.type == "VERIFIES" for e in edges), "unresolved": unresolved}
