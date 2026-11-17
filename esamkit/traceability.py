"""
Traceability: requirements and test cases as vertices of the graph.

The baseline lists DOCUMENTS. Inside an SRS are the requirements; inside an
STD are the test cases, each with a `Verifies:` line (SE manual §9). This
module reads them out, deterministically (no AI), and turns them into:

    Requirement ──SPECIFIED_IN──► DesignArtifact (its SRS)
    Requirement ──ALLOCATED_TO──► Subsystem      (the SRS's subsystem)
    Requirement ──TRACES_TO─────► Requirement    (its parent, from `Traces to:`)
    TestCase    ──SPECIFIED_IN──► DesignArtifact (its STD)
    TestCase    ──VERIFIES──────► Requirement

Every requirement is a SYSTEM requirement (what the subsystem as a whole
must do, whatever mix of hardware and software does it) or a SOFTWARE
requirement (what the software itself must do). Both are written in the SRS
and the ID says which (the ontology's `requirement_classification`):

    SYS-SRS-BRK-050   system requirement
    SW-SRS-BRK-060    software requirement   (plain SRS-BRK-011, the earlier form, is software too)

Each carries `level` and `level_source` ("id"). The wording is only a
cross-check: a `Level:` on the attribute line, or the subject of the
"shall" ("the software shall" / "The Brake Control ECU shall"), that
disagrees with the id is recorded as `level_expected` and reported by the
review, so a requirement numbered as the wrong kind is noticed. An id with
is not an SRS- id is classified by that check instead (stated, wording, else
assumed), and reported.

A software requirement whose `Traces to:` names a system requirement gets a
TRACES_TO link to it, which answers "which system requirements has no
software requirement picked up?".

Every extracted vertex records `source_artifact` (the document it came
from), so reloading a document first removes what its previous revision
contributed: a deleted requirement doesn't linger.

The formats it reads are the ones the manual asks SEs to write:

    - **SYS-SRS-BRK-050** The Brake Control ECU shall prevent wheel lock-up ...
      Verification: T

    - **SW-SRS-BRK-060** The software shall release brake pressure ...
      (continuation lines indented)
      Verification: T | Traces to: SYS-SRS-BRK-050

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

# An id: letters, one or more middle parts, three digits. SRS-BRK-011, TC-BRK-011, SYS-SRS-BRK-050.
ID_FORM = r"[A-Z]+(?:-[A-Z0-9]+)+?-\d{3}"
_REQ_LINE = re.compile(rf"^- \*\*(?P<id>{ID_FORM})\*\*\s*(?P<text>.*)$")
_SECTION = re.compile(r"^#{2,6}\s+(?P<title>.+)$")
_TC_HEAD = re.compile(r"^#{2,6}\s+(?P<id>TC-[A-Z0-9]+-\d{3})\s*[—–-]\s*(?P<title>.+)$")
_FIELD = re.compile(r"^(?P<key>Verifies|Method|Steps|Pass criteria):\s*(?P<value>.*)$", re.I)
_ID = re.compile(rf"\b{ID_FORM}\b")
# The attribute line under a requirement (SE manual §9.4):
#   Safety: SwCI 2 | Timing: 20 ms max | Verification: T | Traces to: SSF-ENG-01 | Basis: MIL-STD-882E 4.4.2
_ATTR = re.compile(r"^(?P<key>Safety|Timing|Verification|Traces to|Basis|Level):\s*(?P<value>.*)$", re.I)
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


LEVELS = ("system", "software")
_LEVEL_WORDS = {"system": "system", "sys": "system", "software": "software", "sw": "software"}
DEFAULT_CLASSIFICATION = {"id_marker": {"system": "SYS", "software": "SW"}, "unmarked": "software", "default": "software", "subjects": {
    "software": ["software", "firmware", "csci", "application", "program", "code", "algorithm", "task"],
    "system": ["system", "subsystem", "vehicle", "ecu", "unit", "module", "controller", "hardware", "equipment"]}}


def classification_rules() -> dict:
    """The ontology's `requirement_classification` (the built-in one if the ontology cannot be read)."""
    try:
        from .ontology import default_ontology
        rules = default_ontology().requirement_classification()
        return rules if rules["subjects"] and rules["id_marker"] else {**DEFAULT_CLASSIFICATION, **{k: v for k, v in rules.items() if v}}
    except Exception:
        return DEFAULT_CLASSIFICATION


def stated_level(value: str | None) -> str | None:
    """`Level:` as written -> "system" | "software", or None when it is neither."""
    return _LEVEL_WORDS.get(re.sub(r"\s+requirement$", "", str(value or "").strip().lower()))


def shall_subject(text: str) -> str:
    """The words that say WHO shall: the clause just before the first "shall".
    "On detection of a fault, the software shall revert ..." -> "the software"."""
    m = re.search(r"^(?P<before>.*?)\bshall\b", text, re.I | re.S)
    if not m:
        return ""
    clause = re.split(r"[,;:]", m["before"])[-1]
    return " ".join(clause.lower().split())


def id_parts(requirement_id: str, rules: dict | None = None) -> tuple[str | None, str]:
    """(marker, the id without it): "SYS-SRS-BRK-050" -> ("SYS", "SRS-BRK-050");
    "SRS-BRK-011" -> (None, "SRS-BRK-011"). Only a known marker in front of "SRS-" in a four-part id counts."""
    parts = str(requirement_id).upper().split("-")
    markers = set((rules or classification_rules())["id_marker"].values())
    if len(parts) == 4 and parts[1] == "SRS" and parts[0] in markers:
        return parts[0], "-".join(parts[1:])
    return None, str(requirement_id).upper()


def level_of_id(requirement_id: str, rules: dict | None = None) -> str | None:
    """The level a requirement id announces: SYS-SRS-… system, SW-SRS-… software,
    plain SRS-… the ontology's `unmarked` (software). None when it is not an SRS id."""
    rules = rules or classification_rules()
    marker, plain = id_parts(requirement_id, rules)
    if marker:
        return next(level for level, m in rules["id_marker"].items() if m == marker)
    return rules["unmarked"] if re.fullmatch(r"SRS-[A-Z0-9]+-\d{3}", plain) else None


def id_for(requirement_id: str, level: str, rules: dict | None = None) -> str:
    """The same id marked for a level: id_for("SRS-BRK-050", "system") -> "SYS-SRS-BRK-050"."""
    rules = rules or classification_rules()
    marker, plain = rules["id_marker"].get(level), id_parts(requirement_id, rules)[1]
    return f"{marker}-{plain}" if marker and plain.startswith("SRS-") else str(requirement_id)


def classify(text: str, attrs: dict | None = None, rules: dict | None = None, requirement_id: str | None = None) -> tuple[str, str]:
    """(level, how it was decided) for one requirement.
    With `requirement_id`, an id that announces a level decides: how = "id".
    Otherwise, and for the cross-check (call it without the id): "stated"
    (its `Level:`), "wording" (the subject of its shall) or "assumed" (the
    default). A software word in the subject wins: "the ECU software shall"
    is a software requirement."""
    rules = rules or classification_rules()
    if requirement_id and (by_id := level_of_id(requirement_id, rules)):
        return by_id, "id"
    said = stated_level((attrs or {}).get("level"))
    if said:
        return said, "stated"
    subject = shall_subject(text)
    for level in ("software", *[k for k in rules["subjects"] if k != "software"]):
        if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", subject) for w in rules["subjects"].get(level, [])):
            return level, "wording"
    return rules["default"], "assumed"


def read_requirements(text: str, artifact_id: str, subsystem: str, rules: dict | None = None) -> Extracted:
    """The requirements stated in an SRS. Each is a system or a software
    requirement as its id says (`level`, `level_source`; see classify). When
    its `Level:` line or its wording says the other, `level_expected` and
    `level_expected_by` record that for the review."""
    out, section, cur = Extracted(), "", None
    rules = rules or classification_rules()

    def close():
        if cur:
            body = " ".join(cur["lines"]).strip()
            level, how = classify(body, cur["attrs"], rules, cur["id"])
            attrs = {k: v for k, v in cur["attrs"].items() if k != "level"}
            said, said_by = classify(body, cur["attrs"], rules)             # the cross-check: Level: line, then wording
            if cur["attrs"].get("level") and said_by != "stated":
                attrs["level_as_written"] = cur["attrs"]["level"]       # e.g. "Level: hardware": reported by the review
            if how == "id" and said_by in ("stated", "wording") and said != level:
                attrs.update(level_expected=said, level_expected_by=said_by)
            out.vertices.append(Vertex("Requirement", {
                "id": cur["id"], "name": _short(body), "text": body, "level": level, "level_source": how,
                "section": cur["section"], "source_artifact": artifact_id, "subsystem": subsystem, **attrs}))
            out.edges += [Edge(cur["id"], artifact_id, "SPECIFIED_IN"), Edge(cur["id"], subsystem, "ALLOCATED_TO")]
            # A parent that is written as a requirement id; kept only if that requirement exists (load_traceability).
            out.edges += [Edge(cur["id"], parent, "TRACES_TO") for parent in dict.fromkeys(
                p for ref in cur["attrs"].get("traces_to", []) for p in _ID.findall(ref)) if parent != cur["id"]]

    for line in text.splitlines():
        if m := _SECTION.match(line):
            close(); cur = None
            section = m["title"].strip()
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
    """Requirements from every SRS (each classified system or software) and
    test cases from every STD the baseline lists (or only the artifact ids in `only`)."""
    out, rules = Extracted(), classification_rules()
    for rel, facts in baseline.artifact_index().items():
        if only is not None and facts["artifact_id"] not in only:
            continue
        path = documents_dir / rel
        if not path.exists():
            continue
        t = facts["artifact_type"]
        if t == "srs":
            part = read_requirements(read_document(path), facts["artifact_id"], facts["subsystem_id"], rules)
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
