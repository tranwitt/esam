"""Every requirement read from an SRS is classified as a system requirement
or a software requirement: by `Level:` on its attribute line, else by the
subject of its "shall", else the ontology's default. A software requirement
traces up to a system requirement."""

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
from esamkit.traceability import classify, extract, load_traceability, read_requirements, shall_subject, stated_level
from test_graph_admin import FakeGraph

SRS2 = """# SRS-BRK-002 — Brake Control ECU Requirements, Part 2

| Document | Type | Revision | Subsystem |
|---|---|---|---|
| SRS-BRK-002 | SRS | A | SUB-BRK-ECU |

## 3. Requirements

### 3.1 What the unit must do

- **SRS-BRK-050** The Brake Control ECU shall prevent sustained lock-up of any braked wheel above 8 km/h.
  Safety: SwCI 1 | Verification: T, A

- **SRS-BRK-051** Service braking shall remain available when the vehicle data bus has failed.
  Level: system | Safety: SwCI 1 | Verification: T

- **SRS-BRK-052** The Brake Control ECU shall inform the driver of a brake fault within 500 ms.
  Timing: 500 ms max | Verification: T

### 3.2 What the software must do

- **SRS-BRK-060** The software shall command pressure release within 20 ms of detecting incipient lock-up.
  Safety: SwCI 1 | Timing: 20 ms max | Verification: T | Traces to: SRS-BRK-050

- **SRS-BRK-061** On a valve fault, the ECU software shall set the EBC1 fault bit within 100 ms.
  Verification: T | Traces to: SRS-BRK-052, ICD-BRK-001

- **SRS-BRK-062** Fault codes shall be logged to non-volatile memory.
  Level: Software | Verification: I | Traces to: SSF-BRK-02, SRS-BRK-099

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
    rc = onto.requirement_classification()
    assert rc["default"] == "software" and "software" in rc["subjects"]["software"] and "ecu" in rc["subjects"]["system"]
    assert set(onto.value_sets["requirement_level"]) == {"system", "software"} and onto.check_self() == []
    assert "sss" not in onto.document_types                                   # requirements come from the SRS only
    assert not ontology_check(onto, settings.baseline_yaml, {"id": "SSS-LTV-001", "name": "x", "type": "sss", "revision": "A",
                                                              "attached_to": "V-001", "file": "SSS-LTV-001.md"})["ok"]
    raw = copy.deepcopy(onto.data)
    raw["requirement_classification"]["subjects"]["hardware"] = ["bracket"]
    assert any("'hardware' is not in value_sets.requirement_level" in f.message for f in Ontology(raw).check_self())
    # an SE's own vocabulary changes the answer without touching code
    raw = copy.deepcopy(onto.data)
    raw["requirement_classification"]["subjects"]["system"].append("gearbox")
    assert classify("The gearbox shall shift within 300 ms.", rules=Ontology(raw).requirement_classification()) == ("system", "wording")
    assert classify("The gearbox shall shift within 300 ms.") == ("software", "assumed")


@pytest.mark.parametrize("text,attrs,expected", [
    ("The software shall execute the wheel-slip control loop every 5 ms.", {}, ("software", "wording")),
    ("On detection of an internal fault, the software shall revert the hydraulic unit.", {}, ("software", "wording")),
    ("In blackout mode the software shall suppress all graphics.", {}, ("software", "wording")),
    ("The Brake Control ECU shall prevent wheel lock-up.", {}, ("system", "wording")),
    ("The system shall report faults to the driver.", {}, ("system", "wording")),
    ("The vehicle shall keep service braking available.", {}, ("system", "wording")),
    ("The ECU software shall log faults.", {}, ("software", "wording")),            # a software word wins
    ("The ECUs shall share one bus.", {}, ("software", "assumed")),                 # whole words only: "ecus" is not "ecu"
    ("Wheel speed shall be sampled at 1 kHz.", {}, ("software", "assumed")),
    ("The software is assigned Software Criticality Index 1.", {}, ("software", "assumed")),      # no shall: no subject
    ("The software shall report what the system shall do.", {}, ("software", "wording")),         # the FIRST shall's subject
    ("The software shall do it.", {"level": "System"}, ("system", "stated")),       # stated beats wording
    ("The ECU shall do it.", {"level": "software requirement"}, ("software", "stated")),
    ("The ECU shall do it.", {"level": "SW"}, ("software", "stated")),
    ("The ECU shall do it.", {"level": "hardware"}, ("system", "wording")),         # not a level: falls back to the wording
])
def test_classify(text, attrs, expected):
    assert classify(text, attrs) == expected


def test_the_subject_is_the_clause_before_the_first_shall():
    assert shall_subject("On detection of an internal fault, the software shall revert.") == "the software"
    assert shall_subject("The Brake  Control\nECU shall prevent lock-up.") == "the brake control ecu"
    assert shall_subject("No such word here.") == ""
    assert (stated_level("system"), stated_level(" SYS "), stated_level("Software"), stated_level("both"), stated_level(None)) == (
        "system", "system", "software", None, None)


def test_every_requirement_of_an_srs_gets_a_level():
    reqs = {v.eid: v.props for v in read_requirements(SRS2, "SRS-BRK-002", "SUB-BRK-ECU").vertices}
    assert {k: (p["level"], p["level_source"]) for k, p in reqs.items()} == {
        "SRS-BRK-050": ("system", "wording"), "SRS-BRK-051": ("system", "stated"), "SRS-BRK-052": ("system", "wording"),
        "SRS-BRK-060": ("software", "wording"), "SRS-BRK-061": ("software", "wording"),
        "SRS-BRK-062": ("software", "stated"), "SRS-BRK-063": ("software", "assumed")}
    assert reqs["SRS-BRK-051"]["safety"] == "SwCI 1" and reqs["SRS-BRK-051"]["verification"] == ["T"]    # the other attributes still read
    assert all("level_as_written" not in p for p in reqs.values())


def test_the_shipped_documents_are_all_software_requirements(onto):
    ex = extract(load_baseline(settings.baseline_yaml), settings.documents_dir)
    reqs = [v.props for v in ex.vertices if v.label == "Requirement"]
    assert len(reqs) == 16 and {p["level"] for p in reqs} == {"software"}
    assert {p["level_source"] for p in reqs} == {"wording"}                    # each says "the software ... shall"
    assert [f for f in check_baseline(onto, yaml.safe_load(settings.baseline_yaml.read_text()), settings.documents_dir)
            if f.level == "error"] == []


def test_software_requirements_trace_up_to_system_requirements(s):
    g, _, res = loaded(s)
    assert (res["requirements"], res["system_requirements"], res["software_requirements"]) == (23, 3, 20)
    traces = sorted((e["from_eid"], e["to_eid"]) for e in g.e.values() if e["type"] == "TRACES_TO")
    # only parents that ARE requirements: not the ICD, not the safety function, not the one nobody states
    assert traces == [("SRS-BRK-060", "SRS-BRK-050"), ("SRS-BRK-061", "SRS-BRK-052")] and res["traces"] == 2
    assert g.v["SRS-BRK-062"]["props"]["traces_to"] == ["SSF-BRK-02", "SRS-BRK-099"]              # kept as written
    assert g.v["SRS-BRK-050"]["props"]["level"] == "system" and g.v["SRS-BRK-060"]["props"]["level"] == "software"
    # both levels are the subsystem's: the SRS is attached to it
    assert {e["to_eid"] for e in g.e.values() if e["type"] == "ALLOCATED_TO" and e["from_eid"].startswith("SRS-BRK-05")} == {"SUB-BRK-ECU"}
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
    assert (reqs["SRS-BRK-051"]["level"], reqs["SRS-BRK-051"]["level_source"]) == ("system", "stated")
    # a software requirement and the system requirement it derives from, in the same document
    assert [(p["id"], p["exists"], p["level"]) for p in reqs["SRS-BRK-060"]["traces_to"]] == [("SRS-BRK-050", True, "system")]
    assert reqs["SRS-BRK-060"]["traces_to"][0]["text"].startswith("The Brake Control ECU shall prevent sustained lock-up")
    # which system requirements the software has picked up
    assert reqs["SRS-BRK-050"]["traced_by"] == ["SRS-BRK-060"] and reqs["SRS-BRK-052"]["traced_by"] == ["SRS-BRK-061"]
    assert reqs["SRS-BRK-051"]["traced_by"] == [] and "traced_by" not in reqs["SRS-BRK-060"]
    assert sorted(rev["issues"]) == [
        "SRS-BRK-062: traces to SRS-BRK-099, which is not a requirement in this document or the graph",
        "SRS-BRK-063: neither a Level: nor the wording says whether this is a system or a software requirement "
        "(who 'shall'?); taken as software. Add 'Level: system' or 'Level: software' to its attribute line"]


def test_the_review_reports_a_level_it_cannot_use_and_a_trace_the_wrong_way(s):
    g, _, _ = loaded(s)
    text = SRS2.replace("  Level: system | Safety: SwCI 1 | Verification: T", "  Level: hardware | Verification: T | Traces to: SRS-BRK-060")
    text = text.replace("- **SRS-BRK-051** Service braking shall", "- **SRS-BRK-051** The unit shall keep service braking; it shall")
    issues = review_document(text, SRS2_ENTRY, g, s)["issues"]
    assert "SRS-BRK-051: Level: 'hardware' is not 'system' or 'software'; classified as system from its wording" in issues
    assert any(i.startswith("SRS-BRK-051: a system requirement traces to the software requirement SRS-BRK-060") for i in issues)


def test_the_health_report_names_the_level_and_a_parent_nobody_states(s, onto):
    from esamkit.health import health_report
    g, _, _ = loaded(s)
    data = yaml.safe_load(s.baseline_yaml.read_text())
    rep = health_report(g, onto, data, s.documents_dir, {a["id"]: 1 for a in data["artifacts"]})
    found = {f["where"]: f["message"] for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]
             if "no test case" in f["message"]}
    assert found["SRS-BRK-050"].startswith("system requirement: no test case verifies it (SUB-BRK-ECU, SRS-BRK-002)")
    assert found["SRS-BRK-060"].startswith("software requirement: no test case verifies it (SUB-BRK-ECU, SRS-BRK-002)")
    msgs = [f"{f['where']}: {f['message']}" for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]]
    assert "SRS-BRK-062: traces to SRS-BRK-099, which no SRS defines" in msgs
    assert not any("SSF-BRK-02" in m or "ICD-BRK-001" in m for m in msgs)      # not requirement ids: not this check's business
    assert not rep["blocked"]


def test_a_test_case_can_verify_either_level(s):
    std = ("# STD-BRK-002 — Brake test description, part 2\n\n## TC-BRK-050 — Wheel lock-up on low friction\n"
           "Verifies: SRS-BRK-050, SRS-BRK-060.\nMethod: Test.\nPass criteria: no wheel locks for more than 150 ms.\n")
    (s.documents_dir / "STD-BRK-002.md").write_text(std)
    entry = {"id": "STD-BRK-002", "name": "Brake test description, part 2", "type": "std", "revision": "A",
             "attached_to": "SUB-BRK-ECU", "file": "STD-BRK-002.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text() + baseline_line(entry) + "\n")
    g, _, res = loaded(s)
    assert res["unresolved"] == [] and res["verifies"] == 2
    cases = review_document(std, entry, g, s)["test_cases"]
    assert [(c["id"], c["level"]) for c in cases[0]["verifies"]] == [("SRS-BRK-050", "system"), ("SRS-BRK-060", "software")]


def test_a_document_held_by_the_vehicle_covers_the_subsystems_under_it(s):
    ssdd = {"id": "SSDD-LTV-001", "name": "Vehicle design", "type": "ssdd", "revision": "A", "attached_to": "V-001", "file": "SSDD-LTV-001.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text() + baseline_line(ssdd) + "\n")
    g, _, _ = loaded(s)
    assert missing_documents(g, ["SUB-BRK-ECU", "SUB-DCL"], ["ssdd"]) == []            # the SSDD is on V-001, above both
    assert {"eid": "SUB-ENG-ECM", "doc_type": "std"} in missing_documents(g, ["SUB-ENG-ECM"], ["std"])
