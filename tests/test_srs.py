"""Pipeline B's deterministic core: the brief, the SRS lint and the evidence
pack, without any agent. Uses the committed brief and a reference draft."""

import pytest

from esamkit.config import PROJECT_ROOT
from esamkit.ontology import default_ontology
from esamkit.srs import check_brief, check_srs, evidence_pack, load_brief
from esamkit.standards import default_library

BRIEF = PROJECT_ROOT / "data" / "briefs" / "SRS-ENG-001.yaml"
DRAFT = (PROJECT_ROOT / "tests" / "fixtures" / "SRS-ENG-001_reference.md").read_text()


class _Graph:
    """Just enough graph: the subsystem, its candidate EBC1 interface, one brake requirement."""
    def snapshot(self):
        return {"vertices": [{"eid": "SUB-ENG-ECM", "label": "Subsystem", "props": {}},
                             {"eid": "SW-SRS-BRK-011", "label": "Requirement", "props": {"source_artifact": "SRS-BRK-001"}}],
                "edges": [{"type": "INTERFACES_WITH", "from_eid": "SUB-J1939-BB", "to_eid": "SUB-ENG-ECM",
                           "props": {"message": "EBC1 (PGN 61441)"}}]}


@pytest.fixture(scope="module")
def ctx():
    brief = load_brief(BRIEF)
    lib, onto = default_library(), default_ontology()
    return brief, check_brief(brief, lib, onto), lib, onto


def _lint(ctx, text):
    brief, a, lib, onto = ctx
    return check_srs(text, brief, a, _Graph(), lib, onto)


def _rules(r):
    return {(f["rule"], f["where"]) for f in r["findings"] if f["level"] == "error"}


def test_brief_swci_from_table_v(ctx):
    a = ctx[1]
    assert a["ok"] and a["csci_swci"] == "SwCI 2"
    assert {x["id"]: x["swci"] for x in a["ssfs"]} == {"SSF-ENG-01": "SwCI 2", "SSF-ENG-02": "SwCI 3", "SSF-ENG-03": "SwCI 2"}
    assert any("3.2.31" in f["basis"] for f in a["findings"])      # baseline says not safety-critical


def test_reference_draft_passes(ctx):
    r = _lint(ctx, DRAFT)
    assert r["ok"], [f for f in r["findings"] if f["level"] == "error"]
    assert len(r["requirements"]) == 5 and r["levels"] == {"system": 1, "software": 4}
    assert {x["id"]: x["level"] for x in r["requirements"]} == {
        "SYS-SRS-ENG-040": "system", "SW-SRS-ENG-010": "software", "SW-SRS-ENG-030": "software",
        "SW-SRS-ENG-020": "software", "SW-SRS-ENG-021": "software"}
    assert not [f for f in r["findings"] if f["rule"] in ("requirement level", "flow-down")]      # ids, wording and traces agree


@pytest.mark.parametrize("change,expect", [
    (("Basis: MIL-STD-882E 4.4.2, DI-IPSC-81433A 3.7", "Basis: MIL-STD-882E 4.4.7"), ("standards trace", "SW-SRS-ENG-020")),
    (("Safety: SwCI 2 | Timing: 20 ms max", "Safety: SwCI 3 | Timing: 20 ms max"), ("safety", "SW-SRS-ENG-020")),
    (("Traces to: SSF-ENG-03, ICD-ENG-001 §2.1", "Traces to: ICD-ENG-001 §2.9"), ("traceability", "SW-SRS-ENG-021")),
    (("### 3.6 Adaptation requirements\n\nNo requirements.\n", ""), ("outline", "3.6")),
    (("### 3.8 Security and privacy requirements\n\nNo requirements.", "### 3.8 Security and privacy requirements\n"), ("outline", "3.8")),
    (("Safety: SwCI 3 | Verification: T |", "Safety: SwCI 3 | Verification: X |"), ("qualification", "SW-SRS-ENG-010")),
    (("**SW-SRS-ENG-030**", "**SW-SRS-BRK-011**"), ("requirement id", "SW-SRS-BRK-011")),
])
def test_lint_catches(ctx, change, expect):
    old, new = change
    assert old in DRAFT
    assert expect in _rules(_lint(ctx, DRAFT.replace(old, new)))


def test_unreadable_requirement_ids_are_named(ctx):
    # a four-part id isn't read as a requirement; say so instead of reporting the SSF as unimplemented only
    r = _lint(ctx, DRAFT.replace("**SW-SRS-ENG-020**", "**SRS-ENG-ECM-020**"))
    assert ("requirement id", "SRS-ENG-ECM-020") in _rules(r)


def test_unimplemented_safety_function_is_an_error(ctx):
    text = DRAFT.replace("Traces to: SSF-ENG-03, ICD-ENG-001 §2.1", "Traces to: ICD-ENG-001 §2.1")
    assert ("coverage", "SSF-ENG-03") in _rules(_lint(ctx, text))


def test_29148_heuristics_warn(ctx):
    text = DRAFT.replace("shall transmit EEC1 (PGN 61444) every 10 ms.",
                         "shall transmit EEC1 (PGN 61444) as appropriate and shall log TBD.")
    w = {(f["rule"], f["where"]) for f in _lint(ctx, text)["findings"] if f["level"] == "warning"}
    assert {("29148 singular (heuristic)", "SW-SRS-ENG-030"), ("29148 unambiguous (heuristic)", "SW-SRS-ENG-030"),
            ("29148 complete (heuristic)", "SW-SRS-ENG-030")} <= w


def test_a_requirement_implementing_an_ssf_needs_its_swci(ctx):
    text = DRAFT.replace("Safety: SwCI 3 | Verification: T | Traces to: SSF-ENG-02", "Verification: T | Traces to: SSF-ENG-02")
    assert ("safety", "SW-SRS-ENG-010") in _rules(_lint(ctx, text))


def test_tbd_timing_and_tool_names(ctx):
    text = DRAFT.replace("Timing: 20 ms max", "Timing: TBD").replace(
        "shall transmit EEC1 (PGN 61444) every 10 ms.", "shall transmit EEC1 (PGN 61444) every 10 ms [read_brief].")
    r = _lint(ctx, text)
    assert ("29148 complete (heuristic)", "SW-SRS-ENG-020") in {(f["rule"], f["where"]) for f in r["findings"]}
    assert ("form", "SW-SRS-ENG-030") in _rules(r)


def test_evidence_pack(ctx, tmp_path):
    brief, a, lib, onto = ctx
    lint = _lint(ctx, DRAFT)
    files = evidence_pack(DRAFT, brief, a, lint, lib, tmp_path,
                          {"run_id": "test", "environment": "local", "model": "none", "generated": "now"})
    index = (tmp_path / "evidence_pack.md").read_text()
    assert "not a certification" in index and "candidate" in index
    matrix = (tmp_path / "compliance_matrix.md").read_text()
    assert "| SSF-ENG-01 |" in matrix and "SwCI 2" in matrix and "4.4.2 Software Safety Criticality Matrix" in matrix
    rows = (tmp_path / "traceability_matrix.csv").read_text().splitlines()
    assert rows[0].startswith("requirement,level,section,") and len(rows) == 6
    assert rows[1].startswith("SYS-SRS-ENG-040,system,3.2,") and sum(",software," in r for r in rows) == 4
    assert "| Requirements | 5: 1 system, 4 software (3 safety) |" in index
    assert set(files) == {"draft", "traceability_matrix", "compliance_matrix", "lint", "evidence_pack"}


# ── system and software requirements in a draft ──────────────────────────────

def _level(r, level="warning"):
    return {f["where"]: f["message"] for f in r["findings"] if f["rule"] == "requirement level" and f["level"] == level}


def test_an_unmarked_id_is_accepted_as_a_system_requirement(ctx):
    # "The Engine Control Module shall": unmarked, read as system, which is what it says. Accepted; the marker is asked for.
    r = _lint(ctx, DRAFT.replace("**SYS-SRS-ENG-040**", "**SRS-ENG-040**").replace(", SYS-SRS-ENG-040", ", SRS-ENG-040"))
    assert r["ok"] and {x["id"]: x["level"] for x in r["requirements"]}["SRS-ENG-040"] == "system"
    assert _level(r) == {"SRS-ENG-040": "no SYS- or SW- marker: read as a system requirement. Number it SYS-SRS-ENG-040 to say so"}


def test_an_unmarked_id_worded_as_software_is_told_to_take_the_software_marker(ctx):
    r = _lint(ctx, DRAFT.replace("**SW-SRS-ENG-030**", "**SRS-ENG-030**"))
    assert r["ok"] and r["levels"] == {"system": 2, "software": 3}             # accepted, as a system requirement first
    assert _level(r)["SRS-ENG-030"] == (
        'the id has no SYS- or SW- marker, so it is read as a system requirement, but its wording reads as a software '
        'requirement ("the software shall"): if it is a software requirement, number it SW-SRS-ENG-030')


def test_an_id_that_disagrees_with_the_requirement_is_reported(ctx):
    r = _lint(ctx, DRAFT.replace("**SYS-SRS-ENG-040**", "**SW-SRS-ENG-040**").replace(", SYS-SRS-ENG-040", ", SW-SRS-ENG-040"))
    assert _level(r)["SW-SRS-ENG-040"].startswith(
        'the id says software requirement, but its wording reads as a system requirement ("the engine control module shall")')
    assert "number it SYS-SRS-ENG-040" in _level(r)["SW-SRS-ENG-040"]
    stated = _lint(ctx, DRAFT.replace("  Timing: 10 ms period | Verification: T |", "  Level: system | Timing: 10 ms period | Verification: T |"))
    assert not stated["ok"] and "its attribute line says 'Level: system'" in _level(stated, "error")["SW-SRS-ENG-030"]
    bad = _lint(ctx, DRAFT.replace("  Timing: 10 ms period | Verification: T |", "  Level: firmware | Timing: 10 ms period | Verification: T |"))
    assert _level(bad, "error")["SW-SRS-ENG-030"] == "Level: 'firmware' is not 'system' or 'software'"


def test_ids_must_be_this_documents_whatever_the_marker(ctx):
    for wrong in ("SYS-SRS-BRK-040", "SW-SRS-BRK-040", "SRS-BRK-040", "SYS-ENG-040", "SYS-SRS-ENG-004"):
        r = _lint(ctx, DRAFT.replace("**SYS-SRS-ENG-040**", f"**{wrong}**"))
        found = {f["where"]: f["message"] for f in r["findings"] if f["rule"] == "requirement id"}
        assert wrong in found and "SYS-SRS-ENG-010 to SYS-SRS-ENG-099 (system) or SW-SRS-ENG-010 to SW-SRS-ENG-099 (software)" in found[wrong], wrong
    r = _lint(ctx, DRAFT.replace("- **SYS-SRS-ENG-040**", "- **SYS_SRS-ENG-40**"))        # not readable as a requirement at all
    msg = next(f["message"] for f in r["findings"] if f["rule"] == "requirement id" and f["where"] == "SYS_SRS-ENG-40")
    assert "SYS-SRS-ENG-0NN for a system requirement or SW-SRS-ENG-0NN for a software requirement" in msg


def test_flow_down_within_the_draft(ctx):
    flow = lambda r: {(f["level"], f["where"]) for f in r["findings"] if f["rule"] == "flow-down"}      # noqa: E731
    # a software requirement may trace to a system requirement of the same draft (it is not in the graph yet)
    assert not any(f["rule"] == "traceability" for f in _lint(ctx, DRAFT)["findings"])
    # nothing traces to the system requirement
    assert flow(_lint(ctx, DRAFT.replace(", SYS-SRS-ENG-040", ""))) == {("warning", "SYS-SRS-ENG-040")}
    # the trace points the wrong way
    down = DRAFT.replace("  Verification: T | Traces to: ICD-ENG-001 §2.1\n", "  Verification: T | Traces to: ICD-ENG-001 §2.1, SW-SRS-ENG-010\n")
    assert ("error", "SYS-SRS-ENG-040") in flow(_lint(ctx, down))
    # a parent that exists nowhere is still an error
    r = _lint(ctx, DRAFT.replace(", SYS-SRS-ENG-040", ", SYS-SRS-ENG-099"))
    assert ("traceability", "SW-SRS-ENG-010") in _rules(r)
