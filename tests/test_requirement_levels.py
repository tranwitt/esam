"""System requirements and software requirements are kept apart. Which one a
requirement is comes from the document it is read from, as the ontology says:
an SSS states system requirements (SSS-…), an SRS software requirements
(SRS-…). A software requirement traces up to a system requirement."""

import copy
import shutil
from dataclasses import replace

import pytest
import yaml

from esamkit.baseline import load_baseline
from esamkit.config import PROJECT_ROOT, settings
from esamkit.coverage import missing_documents
from esamkit.intake import baseline_line, ontology_check, read_header, receive, review_document
from esamkit.ontology import Ontology, check_baseline, default_ontology
from esamkit.traceability import (extract, level_of_id, load_traceability, misplaced_requirements, read_requirements,
                                  requirement_levels)
from test_graph_admin import FakeGraph

SSS = PROJECT_ROOT / "lessons" / "06_missing_document" / "staged" / "SSS-LTV-001.md"
SSS_ENTRY = {"id": "SSS-LTV-001", "name": "SYNTH-LTV Vehicle System/Subsystem Specification", "type": "sss",
             "revision": "A", "attached_to": "V-001", "file": "SSS-LTV-001.md"}
SRS2 = """# SRS-BRK-002 — Brake Control ECU Software Requirements, Part 2

| Document | Type | Revision | Subsystem |
|---|---|---|---|
| SRS-BRK-002 | SRS | A | SUB-BRK-ECU |

## 3. Requirements

- **SRS-BRK-060** The software shall command pressure release within 20 ms of detecting incipient lock-up.
  Safety: SwCI 1 | Timing: 20 ms max | Verification: T | Traces to: SSS-LTV-010

- **SRS-BRK-061** The software shall set the EBC1 fault bit within 100 ms of a valve fault.
  Verification: T | Traces to: SSS-LTV-011, SSS-LTV-021, ICD-BRK-001

- **SRS-BRK-062** The software shall log the fault code to non-volatile memory.
  Verification: I | Traces to: SSF-BRK-02, SSS-LTV-099

- **SRS-BRK-063** The software shall run its self-test at power-up.
  Verification: T
"""
SRS2_ENTRY = {"id": "SRS-BRK-002", "name": "Brake Control ECU Software Requirements, Part 2", "type": "srs",
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


@pytest.fixture
def onto():
    return default_ontology()


@pytest.fixture
def s(tmp_path):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(SSS, docs / "SSS-LTV-001.md")
    (docs / "SRS-BRK-002.md").write_text(SRS2)
    path = tmp_path / "baseline.yaml"
    path.write_text(settings.baseline_yaml.read_text().rstrip("\n") + "\n" + baseline_line(SSS_ENTRY) + "\n"
                    + baseline_line(SRS2_ENTRY) + "\n")
    return replace(settings, baseline_yaml=path, documents_dir=docs)


def loaded(s):
    g, base = Graph(), load_baseline(s.baseline_yaml)
    g.load(base)
    return g, base, load_traceability(g, base, s.documents_dir)


def test_the_ontology_says_which_documents_state_which_requirements(onto):
    assert onto.requirement_levels() == {"sss": "system", "srs": "software"} == requirement_levels()
    assert set(onto.value_sets["requirement_level"]) == {"system", "software"}
    assert onto.attach_classes("sss") == ["Vehicle", "System", "Subsystem"] and onto.check_self() == []
    assert (level_of_id("SSS-LTV-010"), level_of_id("SRS-BRK-011"), level_of_id("ICD-BRK-001")) == ("system", "software", None)
    raw = copy.deepcopy(onto.data)
    raw["document_types"]["sss"]["requirements"] = "hardware"
    assert any("requirements = 'hardware' is not in value_sets.requirement_level" in f.message for f in Ontology(raw).check_self())


def test_an_sss_is_a_document_like_any_other(onto):
    assert read_header(SSS.read_text()) == SSS_ENTRY
    for owner in ("V-001", "SYS-SUSP", "SUB-BRK-ECU"):
        assert ontology_check(onto, settings.baseline_yaml, dict(SSS_ENTRY, attached_to=owner))["ok"]


def test_the_level_comes_from_the_document(s, onto):
    ex = extract(load_baseline(s.baseline_yaml), s.documents_dir)
    reqs = {v.eid: v.props for v in ex.vertices if v.label == "Requirement"}
    by_level = lambda lv: sorted(r for r, p in reqs.items() if p["level"] == lv)                   # noqa: E731
    assert by_level("system") == ["SSS-LTV-010", "SSS-LTV-011", "SSS-LTV-012", "SSS-LTV-020", "SSS-LTV-021", "SSS-LTV-030"]
    assert len(by_level("software")) == 16 + 4 and all(r.startswith("SRS-") for r in by_level("software"))
    assert all(p["source_artifact"].startswith("SSS-") == (p["level"] == "system") for p in reqs.values())
    # a system requirement is allocated to what its SSS is attached to; a software one to its SRS's subsystem
    alloc = {e.from_eid: e.to_eid for e in ex.edges if e.type == "ALLOCATED_TO"}
    assert alloc["SSS-LTV-010"] == "V-001" and alloc["SRS-BRK-060"] == "SUB-BRK-ECU" and reqs["SSS-LTV-010"]["subsystem"] == "V-001"
    assert reqs["SSS-LTV-011"]["timing"] == "500 ms max" and reqs["SSS-LTV-010"]["verification"] == ["T", "A"]
    assert [f for f in check_baseline(onto, yaml.safe_load(s.baseline_yaml.read_text()), s.documents_dir) if f.level == "error"] == []


def test_software_requirements_trace_up_to_system_requirements(s):
    g, _, res = loaded(s)
    assert (res["requirements"], res["system_requirements"], res["software_requirements"]) == (26, 6, 20)
    traces = sorted((e["from_eid"], e["to_eid"]) for e in g.e.values() if e["type"] == "TRACES_TO")
    # only parents that ARE requirements: not the ICD, not the safety function, not the one nobody states
    assert traces == [("SRS-BRK-060", "SSS-LTV-010"), ("SRS-BRK-061", "SSS-LTV-011"), ("SRS-BRK-061", "SSS-LTV-021")]
    assert res["traces"] == 3
    assert g.v["SRS-BRK-062"]["props"]["traces_to"] == ["SSF-BRK-02", "SSS-LTV-099"]              # kept as written
    assert g.v["SSS-LTV-010"]["props"]["level"] == "system" and g.v["SRS-BRK-060"]["props"]["level"] == "software"
    again = load_traceability(g, load_baseline(s.baseline_yaml), s.documents_dir, only={"SRS-BRK-002"})     # one document reloaded
    assert again["traces"] == 3 and again["system_requirements"] == 0 and again["software_requirements"] == 4
    assert sum(e["type"] == "TRACES_TO" for e in g.e.values()) == 3


def test_a_requirement_of_the_other_level_is_not_taken_in():
    srs = SRS2 + "\n- **SSS-LTV-010** The vehicle shall prevent sustained lock-up.\n\n- **SRS-BRK-064** The software shall do one more thing.\n"
    assert misplaced_requirements(srs, "srs") == [{"id": "SSS-LTV-010", "level": "system", "belongs_in": "SSS"}]
    ids = [v.eid for v in read_requirements(srs, "SRS-BRK-002", "SUB-BRK-ECU", "software", "srs").vertices]
    assert ids == ["SRS-BRK-060", "SRS-BRK-061", "SRS-BRK-062", "SRS-BRK-063", "SRS-BRK-064"]
    sss = SSS.read_text() + "\n- **SRS-BRK-011** The software shall release pressure.\n"
    assert misplaced_requirements(sss, "sss") == [{"id": "SRS-BRK-011", "level": "software", "belongs_in": "SRS"}]
    assert all(v.props["level"] == "system" and v.eid.startswith("SSS-")
               for v in read_requirements(sss, "SSS-LTV-001", "V-001", "system", "sss").vertices)
    # read without a document type (older callers): everything, as software
    assert all(v.props["level"] == "software" for v in read_requirements(SRS2, "SRS-BRK-002", "SUB-BRK-ECU").vertices)


def test_reviewing_a_system_specification(s):
    g, _, _ = loaded(s)
    rev = review_document(SSS.read_text(), SSS_ENTRY, g, s)
    assert rev["requirement_level"] == "system" and rev["issues"] == []
    reqs = {r["id"]: r for r in rev["requirements"]}
    assert len(reqs) == 6 and all(r["level"] == "system" for r in reqs.values())
    assert reqs["SSS-LTV-010"]["traced_by"] == ["SRS-BRK-060"] and reqs["SSS-LTV-012"]["traced_by"] == []
    bad = SSS.read_text() + "\n- **SSS-LTV-040** Braking is important.\n\n- **SRS-BRK-011** The software shall release pressure.\n"
    issues = review_document(bad, SSS_ENTRY, g, s)["issues"]
    assert "SSS-LTV-040: no 'shall'" in issues
    assert any(i.startswith("SRS-BRK-011: a software requirement id in an SSS, which states system requirements.") for i in issues)
    empty = review_document("# SSS-LTV-001 — x\n\n## 1. Scope\n\nNothing.\n", SSS_ENTRY, g, s)["issues"]
    assert empty == ["no requirement lines ('- **SSS-…** The system shall …') found"]


def test_reviewing_a_software_specification_shows_its_parents(s):
    g, _, _ = loaded(s)
    rev = review_document(SRS2, SRS2_ENTRY, g, s)
    assert rev["requirement_level"] == "software"
    reqs = {r["id"]: r for r in rev["requirements"]}
    assert [(p["id"], p["exists"], p["level"]) for p in reqs["SRS-BRK-061"]["traces_to"]] == [
        ("SSS-LTV-011", True, "system"), ("SSS-LTV-021", True, "system")]
    assert reqs["SRS-BRK-060"]["traces_to"][0]["text"].startswith("The vehicle shall prevent sustained lock-up")
    assert reqs["SRS-BRK-063"]["traces_to"] == []
    assert rev["issues"] == ["SRS-BRK-062: traces to SSS-LTV-099, which is not a requirement in the graph"]
    mixed = review_document(SRS2 + "\n- **SSS-LTV-010** The vehicle shall prevent lock-up.\n", SRS2_ENTRY, g, s)["issues"]
    assert any("SSS-LTV-010: a system requirement id in an SRS, which states software requirements." in i
               and "Traces to: SSS-LTV-010" in i for i in mixed)


def test_a_test_case_can_verify_either_level(s):
    std = ("# STD-LTV-001 — Vehicle test description\n\n## TC-LTV-010 — Wheel lock-up on low friction\n"
           "Verifies: SSS-LTV-010, SRS-BRK-060.\nMethod: Test.\nPass criteria: no wheel locks for more than 150 ms.\n")
    (s.documents_dir / "STD-LTV-001.md").write_text(std)
    entry = {"id": "STD-LTV-001", "name": "Vehicle test description", "type": "std", "revision": "A",
             "attached_to": "SUB-BRK-ECU", "file": "STD-LTV-001.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text() + baseline_line(entry) + "\n")
    g, _, res = loaded(s)
    assert res["unresolved"] == [] and res["verifies"] == 2
    assert {e["to_eid"] for e in g.e.values() if e["type"] == "VERIFIES"} == {"SSS-LTV-010", "SRS-BRK-060"}


def test_a_document_held_by_the_vehicle_covers_the_subsystems_under_it(s):
    g, _, _ = loaded(s)
    assert missing_documents(g, ["SUB-BRK-ECU", "SUB-DCL"], ["sss"]) == []            # the SSS is on V-001, above both
    assert missing_documents(g, ["SUB-BRK-ECU"], ["ssdd"]) == [{"eid": "SUB-BRK-ECU", "doc_type": "ssdd"}]
    assert {"eid": "SUB-ENG-ECM", "doc_type": "std"} in missing_documents(g, ["SUB-ENG-ECM"], ["std"])


def test_the_sss_arrives_through_the_front_door(tmp_path):
    got = receive(SSS, tmp_path / "incoming", replace(settings, diagrams="local", vision_model=""))
    assert got["entry"] == SSS_ENTRY


def test_the_health_report_names_the_level_and_a_parent_nobody_states(s, onto):
    from esamkit.health import health_report

    class G(Graph):
        def message_origins(self, message):
            return []

        def untested_requirements(self):
            tested = {e["to_eid"] for e in self.e.values() if e["type"] == "VERIFIES"}
            return [{"eid": k, "name": v["props"]["name"], "level": v["props"]["level"], "subsystem": v["props"]["subsystem"],
                     "document": v["props"]["source_artifact"]}
                    for k, v in sorted(self.v.items()) if v["label"] == "Requirement" and k not in tested]

    g, base = G(), load_baseline(s.baseline_yaml)
    g.load(base)
    load_traceability(g, base, s.documents_dir)
    data = yaml.safe_load(s.baseline_yaml.read_text())
    rep = health_report(g, onto, data, s.documents_dir, {a["id"]: 1 for a in data["artifacts"]})
    trace = {f["where"]: f["message"] for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]
             if "no test case" in f["message"]}
    assert trace["SSS-LTV-010"].startswith("system requirement: no test case verifies it (V-001, SSS-LTV-001)")
    assert trace["SRS-BRK-060"].startswith("software requirement: no test case verifies it (SUB-BRK-ECU, SRS-BRK-002)")
    msgs = [f"{f['where']}: {f['message']}" for f in next(c for c in rep["checks"] if c["id"] == 4)["findings"]]
    assert "SRS-BRK-062: traces to SSS-LTV-099, which no SSS defines" in msgs
    assert not any("SSF-BRK-02" in m or "ICD-BRK-001" in m for m in msgs)      # not requirement ids: not this check's business
    assert not rep["blocked"]
