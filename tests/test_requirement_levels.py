"""Every requirement read from an SRS is a system requirement or a software
requirement, and its id says which: SRS-SYS-… system, SRS-SW-… (or the
unmarked SRS-…) software. A `Level:`
line or wording that disagrees with the id is reported. A software
requirement traces up to a system requirement."""

import copy
import shutil
from dataclasses import replace

import pytest
import yaml

from esamkit.baseline import load_baseline
from esamkit.config import settings
from esamkit.coverage import missing_documents
from esamkit.intake import baseline_line, ontology_check, review_document
from esamkit.ontology import Ontology, check_baseline, default_ontology
from esamkit.traceability import (_ID, classify, extract, id_for, id_parts, level_of_id, load_traceability, read_requirements, shall_subject,
                                  stated_level)
from test_graph_admin import FakeGraph

SRS2 = """# SRS-BRK-002 — Brake Control ECU Requirements, Part 2

| Document | Type | Revision | Subsystem |
|---|---|---|---|
| SRS-BRK-002 | SRS | A | SUB-BRK-ECU |

## 3. Requirements

### 3.1 What the unit must do

- **SRS-SYS-BRK-050** The Brake Control ECU shall prevent sustained lock-up of any braked wheel above 8 km/h.
  Safety: SwCI 1 | Verification: T, A

- **SRS-SYS-BRK-051** Service braking shall remain available when the vehicle data bus has failed.
  Level: system | Safety: SwCI 1 | Verification: T

- **SRS-SYS-BRK-052** The Brake Control ECU shall inform the driver of a brake fault within 500 ms.
  Timing: 500 ms max | Verification: T

### 3.2 What the software must do

- **SRS-SW-BRK-060** The software shall command pressure release within 20 ms of detecting incipient lock-up.
  Safety: SwCI 1 | Timing: 20 ms max | Verification: T | Traces to: SRS-SYS-BRK-050

- **SRS-BRK-061** On a valve fault, the ECU software shall set the EBC1 fault bit within 100 ms.
  Verification: T | Traces to: SRS-SYS-BRK-052, ICD-BRK-001

- **SRS-BRK-062** Fault codes shall be logged to non-volatile memory.
  Level: Software | Verification: I | Traces to: SSF-BRK-02, SRS-SYS-BRK-099

- **SRS-BRK-063** Wheel speed shall be sampled at 1 kHz.
  Verification: T
"""
SRS2_ENTRY = {"id": "SRS-BRK-002", "name": "Brake Control ECU Requirements, Part 2", "type": "srs",
              "revision": "A", "attached_to": "SUB-BRK-ECU", "file": "SRS-BRK-002.md"}


class Graph(FakeGraph):
    def ancestors(self, eid):                      # like the store: the node itself first (depth 0), then upwards
        row = lambda e, d: {"eid": e, "label": self.v[e]["label"], "depth": d, "name": self.v[e]["props"].get("name"),   # noqa: E731
                            "wbs_code": self.v[e]["props"].get("wbs_code")}
        out, depth = [row(eid, 0)], 0
        while True:
            up = next((e for e in self.e.values() if e["type"] == "PART_OF" and e["from_eid"] == eid), None)
            if not up:
                return out
            eid, depth = up["to_eid"], depth + 1
            out.append(row(eid, depth))

    def artifact_gaps(self, t):
        have = {e["from_eid"] for e in self.e.values() if e["type"] == "HAS_ARTIFACT" and e["props"].get("artifact_type") == t}
        return [{"eid": k} for k, v in self.v.items() if v["label"] == "Subsystem" and k not in have]

    def message_origins(self, message):
        return []

    def verified_by(self, requirement_id):
        return [{"eid": e["from_eid"], "document": self.v[e["from_eid"]]["props"]["source_artifact"]}
                for e in self.e.values() if e["type"] == "VERIFIES" and e["to_eid"] == requirement_id]

    def untested_requirements(self):
        tested = {e["to_eid"] for e in self.e.values() if e["type"] == "VERIFIES"}
        return [{"eid": k, "name": v["props"]["name"], "level": v["props"]["level"], "subsystem": v["props"]["subsystem"],
                 "document": v["props"]["source_artifact"]}
                for k, v in sorted(self.v.items()) if v["label"] == "Requirement" and k not in tested]


@pytest.fixture
def onto():
    return default_ontology()


@pytest.fixture
def s(tmp_path):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    (docs / "SRS-BRK-002.md").write_text(SRS2)
    path = tmp_path / "baseline.yaml"
    path.write_text(settings.baseline_yaml.read_text().rstrip("\n") + "\n" + baseline_line(SRS2_ENTRY) + "\n")
    return replace(settings, baseline_yaml=path, documents_dir=docs)


def loaded(s):
    g, base = Graph(), load_baseline(s.baseline_yaml)
    g.load(base)
    return g, base, load_traceability(g, base, s.documents_dir)


def test_the_rule_is_the_ontologys(onto):
    import re
    rc = onto.requirement_classification()
    assert rc["id_marker"] == {"system": "SYS", "software": "SW"} and rc["unmarked"] == rc["default"] == "software"
    assert "software" in rc["subjects"]["software"] and "ecu" in rc["subjects"]["system"]
    assert set(onto.value_sets["requirement_level"]) == {"system", "software"} and onto.check_self() == []
    assert "sss" not in onto.document_types                                   # requirements come from the SRS only
    pattern = re.compile(onto.classes["Requirement"]["id_pattern"])
    assert all(pattern.match(i) for i in ("SRS-SYS-BRK-050", "SRS-SW-BRK-060", "SRS-BRK-011"))
    assert not any(pattern.match(i) for i in ("SYS-BRK-050", "REQ-BRK-050", "SRS-HW-BRK-050", "SRS-SYS-BRK-005"))
    for change, message in ((lambda rc: rc["id_marker"].update(system="SYSTEM"), "id_marker.system = 'SYSTEM' does not fit"),
                            (lambda rc: rc["id_marker"].update(system="SW"), "two levels share one id marker"),
                            (lambda rc: rc["id_marker"].pop("software"), "id_marker has no marker for software requirements"),
                            (lambda rc: rc["subjects"].update(hardware=["bracket"]), "'hardware' is not in value_sets.requirement_level")):
        raw = copy.deepcopy(onto.data)
        change(raw["requirement_classification"])
        assert any(message in f.message for f in Ontology(raw).check_self()), message


@pytest.mark.parametrize("rid,expected", [("SRS-SYS-BRK-050", "system"), ("SRS-SW-BRK-060", "software"), ("srs-sys-brk-050", "system"),
                                          ("SRS-BRK-011", "software"),                       # unmarked: the earlier form
                                          ("SYS-BRK-050", None), ("REQ-BRK-050", None), ("ICD-BRK-001", None),
                                          ("SRS-HW-BRK-050", None), ("TC-BRK-011", None)])
def test_the_id_says_the_level(rid, expected):
    assert level_of_id(rid) == expected


def test_the_id_decides_whatever_the_wording():
    assert classify("The software shall do it.", {}, requirement_id="SRS-SYS-BRK-050") == ("system", "id")
    assert classify("The Brake Control ECU shall do it.", {"level": "system"}, requirement_id="SRS-SW-BRK-060") == ("software", "id")
    assert classify("The Brake Control ECU shall do it.", {}, requirement_id="REQ-BRK-050") == ("system", "wording")   # no level in the id
    assert id_for("SRS-BRK-050", "system") == "SRS-SYS-BRK-050" and id_for("SRS-SYS-BRK-060", "software") == "SRS-SW-BRK-060"
    assert id_parts("SRS-SYS-BRK-050") == ("SYS", "SRS-BRK-050") and id_parts("SRS-BRK-011") == (None, "SRS-BRK-011")


@pytest.mark.parametrize("text,attrs,expected", [
    ("The software shall execute the wheel-slip control loop every 5 ms.", {}, ("software", "wording")),
    ("On detection of an internal fault, the software shall revert the hydraulic unit.", {}, ("software", "wording")),
    ("In blackout mode the software shall suppress all graphics.", {}, ("software", "wording")),
    ("The Brake Control ECU shall prevent wheel lock-up.", {}, ("system", "wording")),
    ("The system shall report faults to the driver.", {}, ("system", "wording")),
    ("The ECU software shall log faults.", {}, ("software", "wording")),            # a software word wins
    ("The ECUs shall share one bus.", {}, ("software", "assumed")),                 # whole words only: "ecus" is not "ecu"
    ("Wheel speed shall be sampled at 1 kHz.", {}, ("software", "assumed")),
    ("The software shall report what the system shall do.", {}, ("software", "wording")),         # the FIRST shall's subject
    ("The software shall do it.", {"level": "System"}, ("system", "stated")),       # stated beats wording
    ("The ECU shall do it.", {"level": "SW"}, ("software", "stated")),
    ("The ECU shall do it.", {"level": "hardware"}, ("system", "wording")),         # not a level: falls back to the wording
])
def test_the_cross_check_reads_the_level_line_then_the_wording(text, attrs, expected):
    assert classify(text, attrs) == expected


def test_the_subject_is_the_clause_before_the_first_shall():
    assert shall_subject("On detection of an internal fault, the software shall revert.") == "the software"
    assert shall_subject("The Brake  Control\nECU shall prevent lock-up.") == "the brake control ecu"
    assert shall_subject("No such word here.") == ""
    assert (stated_level("system"), stated_level(" SYS "), stated_level("Software"), stated_level("both"), stated_level(None)) == (
        "system", "system", "software", None, None)


def test_every_requirement_of_an_srs_gets_its_level_from_its_id():
    reqs = {v.eid: v.props for v in read_requirements(SRS2, "SRS-BRK-002", "SUB-BRK-ECU").vertices}
    assert {k: p["level"] for k, p in reqs.items()} == {
        "SRS-SYS-BRK-050": "system", "SRS-SYS-BRK-051": "system", "SRS-SYS-BRK-052": "system",
        "SRS-SW-BRK-060": "software", "SRS-BRK-061": "software", "SRS-BRK-062": "software", "SRS-BRK-063": "software"}
    assert {p["level_source"] for p in reqs.values()} == {"id"}
    assert all("level_expected" not in p and "level_as_written" not in p for p in reqs.values())      # ids and wording agree
    assert reqs["SRS-SYS-BRK-051"]["safety"] == "SwCI 1" and reqs["SRS-SYS-BRK-051"]["verification"] == ["T"]    # the other attributes still read


def test_the_shipped_documents_are_all_software_requirements(onto):
    ex = extract(load_baseline(settings.baseline_yaml), settings.documents_dir)
    reqs = [v.props for v in ex.vertices if v.label == "Requirement"]
    assert len(reqs) == 16 and {p["level"] for p in reqs} == {"software"} and {p["level_source"] for p in reqs} == {"id"}
    assert all(p["id"].startswith("SRS-") and "level_expected" not in p for p in reqs)        # each says "the software ... shall"
    assert [f for f in check_baseline(onto, yaml.safe_load(settings.baseline_yaml.read_text()), settings.documents_dir)
            if f.level == "error"] == []


def test_software_requirements_trace_up_to_system_requirements(s):
    g, _, res = loaded(s)
    assert (res["requirements"], res["system_requirements"], res["software_requirements"]) == (23, 3, 20)
    traces = sorted((e["from_eid"], e["to_eid"]) for e in g.e.values() if e["type"] == "TRACES_TO")
    # only parents that ARE requirements: not the ICD, not the safety function, not the one nobody states
    assert traces == [("SRS-BRK-061", "SRS-SYS-BRK-052"), ("SRS-SW-BRK-060", "SRS-SYS-BRK-050")] and res["traces"] == 2
    assert g.v["SRS-BRK-062"]["props"]["traces_to"] == ["SSF-BRK-02", "SRS-SYS-BRK-099"]              # kept as written
    assert g.v["SRS-SYS-BRK-050"]["props"]["level"] == "system" and g.v["SRS-SW-BRK-060"]["props"]["level"] == "software"
    # both levels are the subsystem's: the SRS is attached to it
    assert {e["to_eid"] for e in g.e.values() if e["type"] == "ALLOCATED_TO" and e["from_eid"].startswith("SRS-SYS-BRK-05")} == {"SUB-BRK-ECU"}
    again = load_traceability(g, load_baseline(s.baseline_yaml), s.documents_dir, only={"SRS-BRK-002"})     # one document reloaded
    assert (again["traces"], again["system_requirements"], again["software_requirements"]) == (2, 3, 4)
    assert sum(e["type"] == "TRACES_TO" for e in g.e.values()) == 2


def test_the_review_lists_the_two_levels_apart(s):
    g = Graph()
    g.load(load_baseline(settings.baseline_yaml))
    load_traceability(g, load_baseline(settings.baseline_yaml), settings.documents_dir)        # the graph BEFORE this document
    rev = review_document(SRS2, SRS2_ENTRY, g, s)
    assert rev["levels"] == {"system": 3, "software": 4}
    reqs = {r["id"]: r for r in rev["requirements"]}
    assert (reqs["SRS-SYS-BRK-051"]["level"], reqs["SRS-SYS-BRK-051"]["level_source"]) == ("system", "id")
    # a software requirement and the system requirement it derives from, in the same document
    assert [(p["id"], p["exists"], p["level"]) for p in reqs["SRS-SW-BRK-060"]["traces_to"]] == [("SRS-SYS-BRK-050", True, "system")]
    assert reqs["SRS-SW-BRK-060"]["traces_to"][0]["text"].startswith("The Brake Control ECU shall prevent sustained lock-up")
    # which system requirements the software has picked up
    assert reqs["SRS-SYS-BRK-050"]["traced_by"] == ["SRS-SW-BRK-060"] and reqs["SRS-SYS-BRK-052"]["traced_by"] == ["SRS-BRK-061"]
    assert reqs["SRS-SYS-BRK-051"]["traced_by"] == [] and "traced_by" not in reqs["SRS-SW-BRK-060"]
    assert rev["issues"] == ["SRS-BRK-062: traces to SRS-SYS-BRK-099, which is not a requirement in this document or the graph"]


def test_the_review_reports_an_id_that_disagrees_with_the_requirement(s):
    g, _, _ = loaded(s)
    text = (SRS2.replace("- **SRS-SYS-BRK-050** The Brake Control ECU shall", "- **SRS-SW-BRK-050** The Brake Control ECU shall")   # worded as system
                .replace("- **SRS-SYS-BRK-051**", "- **SRS-BRK-051**")                                                     # Level: system, unmarked id
                .replace("- **SRS-SW-BRK-060** The software shall", "- **SRS-SYS-BRK-060** The software shall")            # worded as software
                .replace("- **SRS-BRK-063**", "- **REQ-BRK-063**")                                                         # not an SRS- id
                .replace("  Level: Software | Verification: I", "  Level: firmware | Verification: I"))
    rev = review_document(text, SRS2_ENTRY, g, s)
    reqs = {r["id"]: (r["level"], r["level_source"]) for r in rev["requirements"]}
    assert reqs["SRS-SW-BRK-050"] == ("software", "id") and reqs["SRS-SYS-BRK-060"] == ("system", "id")      # the id decides
    assert reqs["SRS-BRK-051"] == ("software", "id") and reqs["REQ-BRK-063"] == ("software", "assumed")
    issues = rev["issues"]
    assert ('SRS-SW-BRK-050: the id says software requirement, but its wording reads as a system requirement '
            '("the brake control ecu shall"). If it is a system requirement, number it SRS-SYS-BRK-050') in issues
    assert ("SRS-BRK-051: the id says software requirement, but its attribute line says 'Level: system'. "
            "If it is a system requirement, number it SRS-SYS-BRK-051") in issues
    assert ('SRS-SYS-BRK-060: the id says system requirement, but its wording reads as a software requirement '
            '("the software shall"). If it is a software requirement, number it SRS-SW-BRK-060') in issues
    assert ("REQ-BRK-063: the id does not say whether this is a system or a software requirement: use SRS-SYS-<code>-0NN "
            "(system) or SRS-SW-<code>-0NN (software). Taken as software (assumed)") in issues
    assert "SRS-BRK-062: Level: 'firmware' is not 'system' or 'software'" in issues
    # a system requirement that traces DOWN to a software requirement
    down = SRS2.replace("  Timing: 500 ms max | Verification: T\n", "  Timing: 500 ms max | Verification: T | Traces to: SRS-BRK-061\n")
    assert any(i.startswith("SRS-SYS-BRK-052: a system requirement traces to the software requirement SRS-BRK-061")
               for i in review_document(down, SRS2_ENTRY, g, s)["issues"])


def test_four_part_ids_are_read_whole_everywhere():
    assert _ID.findall("Verifies: SRS-SYS-BRK-050, SRS-BRK-011; TC-BRK-011, ICD-BRK-001 §2.1 and SRS-SW-BRK-060.") == [
        "SRS-SYS-BRK-050", "SRS-BRK-011", "TC-BRK-011", "ICD-BRK-001", "SRS-SW-BRK-060"]
    from esamkit.hybrid import _REQ_ID
    assert _REQ_ID.findall("What verifies SRS-SYS-BRK-050 and SRS-BRK-011, and SRS-SW-BRK-060?") == [
        "SRS-SYS-BRK-050", "SRS-BRK-011", "SRS-SW-BRK-060"]
    from esamkit.traceability import read_test_cases
    tc = read_test_cases("## TC-BRK-050 — Lock-up\nVerifies: SRS-SYS-BRK-050, SRS-SW-BRK-060.\nMethod: Test.\n", "STD-BRK-002").vertices[0]
    assert tc.props["verifies"] == ["SRS-SYS-BRK-050", "SRS-SW-BRK-060"]


def test_the_health_report_names_the_level_and_a_parent_nobody_states(s, onto):
    from esamkit.health import health_report
    g, _, _ = loaded(s)
    data = yaml.safe_load(s.baseline_yaml.read_text())
    rep = health_report(g, onto, data, s.documents_dir, {a["id"]: 1 for a in data["artifacts"]})
    found = {f["where"]: f["message"] for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]
             if "no test case" in f["message"]}
    assert found["SRS-SYS-BRK-050"].startswith("system requirement: no test case verifies it (SUB-BRK-ECU, SRS-BRK-002)")
    assert found["SRS-SW-BRK-060"].startswith("software requirement: no test case verifies it (SUB-BRK-ECU, SRS-BRK-002)")
    msgs = [f"{f['where']}: {f['message']}" for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]]
    assert "SRS-BRK-062: traces to SRS-SYS-BRK-099, which no SRS defines" in msgs
    assert not any("SSF-BRK-02" in m or "ICD-BRK-001" in m for m in msgs)      # not requirement ids: not this check's business
    assert not rep["blocked"]


def test_a_test_case_can_verify_either_level(s):
    std = ("# STD-BRK-002 — Brake test description, part 2\n\n## TC-BRK-050 — Wheel lock-up on low friction\n"
           "Verifies: SRS-SYS-BRK-050, SRS-SW-BRK-060.\nMethod: Test.\nPass criteria: no wheel locks for more than 150 ms.\n")
    (s.documents_dir / "STD-BRK-002.md").write_text(std)
    entry = {"id": "STD-BRK-002", "name": "Brake test description, part 2", "type": "std", "revision": "A",
             "attached_to": "SUB-BRK-ECU", "file": "STD-BRK-002.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text() + baseline_line(entry) + "\n")
    g, _, res = loaded(s)
    assert res["unresolved"] == [] and res["verifies"] == 2
    cases = review_document(std, entry, g, s)["test_cases"]
    assert [(c["id"], c["level"]) for c in cases[0]["verifies"]] == [("SRS-SYS-BRK-050", "system"), ("SRS-SW-BRK-060", "software")]


def test_a_document_held_by_the_vehicle_covers_the_subsystems_under_it(s):
    ssdd = {"id": "SSDD-LTV-001", "name": "Vehicle design", "type": "ssdd", "revision": "A", "attached_to": "V-001", "file": "SSDD-LTV-001.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text() + baseline_line(ssdd) + "\n")
    g, _, _ = loaded(s)
    assert missing_documents(g, ["SUB-BRK-ECU", "SUB-DCL"], ["ssdd"]) == []            # the SSDD is on V-001, above both
    assert {"eid": "SUB-ENG-ECM", "doc_type": "std"} in missing_documents(g, ["SUB-ENG-ECM"], ["std"])


def test_ids_keep_their_level_through_a_word_file(tmp_path):
    import wordfiles as wf
    from esamkit.readers import convert
    body = (wf.p("SRS-BRK-002 — Brake Control ECU Requirements, Part 2", "Title") + wf.p("Requirements", "Heading1")
            + wf.p(" The Brake Control ECU shall prevent sustained wheel lock-up.", bold_prefix="SRS-SYS-BRK-050")
            + wf.p(" The software shall command pressure release within 20 ms.", bold_prefix="SRS-SW-BRK-060"))
    (tmp_path / "SRS-BRK-002.docx").write_bytes(wf.make_docx(body))
    text = convert(tmp_path / "SRS-BRK-002.docx", replace(settings, diagrams="local", vision_model="")).text
    reqs = {v.eid: v.props["level"] for v in read_requirements(text, "SRS-BRK-002", "SUB-BRK-ECU").vertices}
    assert reqs == {"SRS-SYS-BRK-050": "system", "SRS-SW-BRK-060": "software"}
