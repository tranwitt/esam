"""Adding to the vehicle baseline with simple inputs (esamkit/baseline_edit.py,
crew/baseline_admin.py): vehicles, families, variants, systems, subsystems, functions."""

import argparse
import shutil
from dataclasses import replace

import pytest
import yaml

from crew import baseline_admin
from esamkit import baseline_edit as be
from esamkit.baseline import load_baseline
from esamkit.config import settings
from esamkit.diagram_baseline import existing_structure
from esamkit.ontology import check_baseline, find_node, load_ontology, standard_element, wbs_findings
from test_graph_admin import FakeGraph

ONTO = load_ontology()


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "baseline.yaml"
    shutil.copy2(settings.baseline_yaml, p)
    return p


def added_lines(before: str, after: str) -> list[str]:
    """The lines of `after` that are not in `before`; fails if any line of `before` moved or changed."""
    old, new, extra, i = before.split("\n"), after.split("\n"), [], 0
    for line in new:
        if i < len(old) and line == old[i]:
            i += 1
        else:
            extra.append(line)
    assert i == len(old), "a line that was in the baseline changed or moved"
    return extra


def test_a_system_a_subsystem_and_a_function_from_their_names(path):
    before = path.read_text()
    sysm = be.add("system", "Fire  Control", path, ONTO)
    assert (sysm["id"], sysm["parent"], sysm["wbs_code"], sysm["written"]) == ("SYS-FC", "VAR-001", "1.1.1.9", True)
    sub = be.add("subsystem", "Turret Drive Controller (TDC)", path, ONTO, parent="SYS-FC",
                 attributes={"safety_critical": "yes", "os_class": "rtos", "aliases": ["turret ECU", "TDC"],
                             "j1939_source_address": "42", "safety_classification": "SwCI 2"})
    assert sub["id"] == "SUB-TDC" and sub["wbs_code"] is None
    fn = be.add("function", "Turret slew control", path, ONTO, parent="SUB-TDC",
                attributes={"description": 'Drives the turret to the "commanded" bearing: fast.'})
    assert fn["id"] == "FN-TDC-TSC"

    assert added_lines(before, path.read_text()) == [
        "            - id: SYS-FC",
        '              name: "Fire Control"',
        '              wbs_code: "1.1.1.9"',
        "              universality: universal",
        "              subsystems:",
        "                - id: SUB-TDC",
        '                  name: "Turret Drive Controller (TDC)"',
        "                  safety_critical: true",
        '                  aliases: ["turret ECU", "TDC"]',
        '                  safety_classification: "SwCI 2"',
        "                  os_class: rtos",
        "                  j1939_source_address: 42",
        "                  functions:",
        "                    - id: FN-TDC-TSC",
        '                      name: "Turret slew control"',
        '                      description: "Drives the turret to the \\"commanded\\" bearing: fast."',
    ]
    data = yaml.safe_load(path.read_text())
    assert check_baseline(ONTO, data, settings.documents_dir) == []
    assert find_node(data, "FN-TDC-TSC")[1]["description"] == 'Drives the turret to the "commanded" bearing: fast.'
    base = load_baseline(path)
    assert base.vertex("SUB-TDC").props["j1939_source_address"] == 42
    assert {(e.from_eid, e.to_eid) for e in base.edges if e.type == "PART_OF"} >= {
        ("SYS-FC", "VAR-001"), ("SUB-TDC", "SYS-FC"), ("FN-TDC-TSC", "SUB-TDC")}


def test_an_entry_joins_the_end_of_a_list_that_is_there_and_starts_one_that_is_not(path):
    before = path.read_text()
    # SUB-ENG-ECM has no functions yet and is the last thing in the vehicle tree
    assert be.add("function", "Engine speed governing", path, ONTO, parent="SUB-ENG-ECM")["id"] == "FN-ENG-ESG"
    # SUB-BRK-ECU has two, and sits in the middle of the file
    be.add("function", "Self test", path, ONTO, parent="SUB-BRK-ECU", id="FN-BRK-BIT")
    # a subsystem in the middle system, after its last subsystem's functions
    be.add("subsystem", "Body Controller", path, ONTO, parent="SYS-VETR", attributes={"safety_critical": "no"})
    data = yaml.safe_load(path.read_text())
    assert [f["id"] for f in find_node(data, "SUB-BRK-ECU")[1]["functions"]] == ["FN-BRK-SLIP", "FN-BRK-STATUS", "FN-BRK-BIT"]
    assert [f["id"] for f in find_node(data, "SUB-ENG-ECM")[1]["functions"]] == ["FN-ENG-ESG"]
    assert [x["id"] for x in find_node(data, "SYS-VETR")[1]["subsystems"]] == ["SUB-J1939-BB", "SUB-DCL", "SUB-BC"]
    assert find_node(data, "SUB-BC")[1]["safety_critical"] is False
    assert len(added_lines(before, path.read_text())) == 2 + 3 + 3          # every comment and line of the SE's file is still there
    assert data["interfaces"] == yaml.safe_load(before)["interfaces"] and data["artifacts"] == yaml.safe_load(before)["artifacts"]


def test_systems_get_the_number_the_standard_gives_their_name(path):
    """MIL-STD-881F Appendix G, G.3: 1.1.1.2 Hull/Frame/Body/Cab ... 1.1.1.14 Communications,
    1.1.1.15 Software Release, 1.1.1.16 onwards for what the table does not name."""
    got = {}
    for name in ("Fire Control", "Hull", "Turret", "Vehicle Communications", "Navigation", "Auxiliary Automotive",
                 "Mission Software Release 1", "Recovery Crane", "Winch"):
        res = be.add("system", name, path, ONTO)
        got[name] = (res["id"], res["wbs_code"])
    assert got == {"Fire Control": ("SYS-FC", "1.1.1.9"), "Hull": ("SYS-HULL", "1.1.1.2"), "Turret": ("SYS-TURRET", "1.1.1.4"),
                   "Vehicle Communications": ("SYS-VC", "1.1.1.14"), "Navigation": ("SYS-NAVIGA", "1.1.1.12"),
                   "Auxiliary Automotive": ("SYS-AA", "1.1.1.8"), "Mission Software Release 1": ("SYS-MSR1", "1.1.1.15"),
                   "Recovery Crane": ("SYS-RC", "1.1.1.16"), "Winch": ("SYS-WINCH", "1.1.1.17")}
    note = be.add("system", "Trailer Hitch", path, ONTO, dry_run=True)["notes"][0]
    assert "is not one of the elements MIL-STD-881F Appendix G, G.3 (13 May 2022) names" in note and "WBS 1.1.1.18" in note
    assert check_baseline(ONTO, yaml.safe_load(path.read_text()), settings.documents_dir) == []

    # the standard's slot is taken: say whose it is, and what to do instead
    for name, why in [("Suspension", r"already has WBS 1.1.1.5 \(SYS-SUSP, Suspension/Steering\).*as a subsystem of SYS-SUSP"),
                      ("Drive Train", r"already has WBS 1.1.1.7 \(SYS-PWR"),
                      ("Software Release 2", r"gives further releases no number: give one with --wbs \(16 or above\)")]:
        with pytest.raises(be.BaselineEditError, match=why):
            be.add("system", name, path, ONTO)
    assert be.add("system", "Software Release 2", path, ONTO, wbs="19")["wbs_code"] == "1.1.1.19"

    # a number the standard names can only carry that name, and a named system cannot sit among the others
    for name, wbs, why in [("Body Network", "3", "WBS 1.1.1.3 is 'System Survivability' in MIL-STD-881F .*, not 'Body Network'"),
                           ("Armament", "20", "gives 'Armament' the number 1.1.1.10"),
                           ("Winch Two", "1.1.1.17", "WBS 1.1.1.17 is also SYS-WINCH's"),
                           ("Deep", "1.1.1.16.1", "is not one level below VAR-001's 1.1.1"),
                           ("Elsewhere", "2.1.1.4", "doesn't sit under its parent's 1.1.1")]:
        with pytest.raises(be.BaselineEditError, match=why):
            be.add("system", name, path, ONTO, wbs=wbs)
    assert be.add("system", "System Survivability", path, ONTO, wbs="3")["wbs_code"] == "1.1.1.3"

    s1 = be.add("subsystem", "Pump Unit", path, ONTO, parent="SYS-FC", attributes={"safety_critical": False})
    s2 = be.add("subsystem", "Pump Unit", path, ONTO, parent="SYS-AA", attributes={"safety_critical": True})
    assert (s1["id"], s2["id"]) == ("SUB-PUMP", "SUB-PUMP2") and "SUB-PUMP is also called 'Pump Unit'" in s2["notes"][0]


def test_families_and_variants_follow_the_standard(path):
    var = be.add("variant", "Ambulance", path, ONTO)                        # 1.1.1 is the lead variant; the others follow
    assert (var["id"], var["wbs_code"], var["attributes"]) == ("VAR-002", "1.1.2", {"role": "derivative"})
    assert be.add("variant", "Cargo", path, ONTO, parent="FAM-001")["wbs_code"] == "1.1.3"
    with pytest.raises(be.BaselineEditError, match="the lead variant is 1.1.1 and the other variants follow it"):
        be.add("variant", "Command Post", path, ONTO, parent="FAM-001", attributes={"role": "lead"})
    # a variant has the same numbered elements as the lead
    assert be.add("system", "Suspension/Steering", path, ONTO, parent="VAR-002")["wbs_code"] == "1.1.2.5"

    fam = be.add("family", "Recovery Vehicles", path, ONTO)                 # 1.2 Secondary Vehicle
    assert (fam["id"], fam["wbs_code"]) == ("FAM-002", "1.2") and "'Secondary Vehicle' element" in fam["notes"][0]
    sec = be.add("variant", "Wrecker", path, ONTO, parent="FAM-002")
    assert (sec["wbs_code"], sec["attributes"]) == ("1.2.1", {"role": "lead"})
    with pytest.raises(be.BaselineEditError, match=r"already has 1.1 \(FAM-001\) and 1.2 \(FAM-002\).*1.3 onwards are the common"):
        be.add("family", "Third", path, ONTO)
    with pytest.raises(be.BaselineEditError, match="WBS 1.3 is not a vehicle element"):
        be.add("family", "Third", path, ONTO, wbs="3")
    assert check_baseline(ONTO, yaml.safe_load(path.read_text()), settings.documents_dir) == []


def test_a_new_vehicle_is_a_ground_vehicle_system_of_its_own(path):
    before = path.read_text()
    veh = be.add("vehicle", "SYNTH-MTV Medium Tactical Vehicle", path, ONTO, variant_name="Cargo")
    assert [(c["id"], c["class"], c["name"], c["wbs_code"]) for c in veh["created"]] == [
        ("V-002", "Vehicle", "SYNTH-MTV Medium Tactical Vehicle", "1.0"),
        ("FAM-002", "VehicleFamily", "SYNTH-MTV Medium Tactical Vehicle Family of Vehicles", "1.1"),
        ("VAR-002", "Variant", "Cargo", "1.1.1")]
    assert veh["text"].split("\n")[:5] == [
        "", "# Further ground vehicle systems, written like `vehicle:` above. Each has its own WBS, from 1.0.", "vehicles:",
        "  - id: V-002", '    name: "SYNTH-MTV Medium Tactical Vehicle"']
    added_lines(before, path.read_text())                       # nothing that was there moved
    text = path.read_text()
    assert text.index("\nvehicles:") < text.index("\n# Interfaces = edges of the graph") < text.index("\ninterfaces:")
    for wbs in ("2", "2.0", "1.1"):
        with pytest.raises(be.BaselineEditError, match="a ground vehicle system is WBS 1.0"):
            be.add("vehicle", "Other", path, ONTO, wbs=wbs)

    # with two variants, a system has to say which
    with pytest.raises(be.BaselineEditError, match=r"which Variant does it go in\? Add --in ID. The choices: VAR-001 .*; VAR-002 \(Cargo\)"):
        be.add("system", "Hull/Frame", path, ONTO)
    hull = be.add("system", "Hull/Frame", path, ONTO, parent="VAR-002")
    assert (hull["id"], hull["wbs_code"]) == ("SYS-HF", "1.1.1.2")
    # the second vehicle's WBS is its own: the first vehicle's 1.1.1.5 does not take its 1.1.1.5
    assert be.add("system", "Suspension/Steering", path, ONTO, parent="VAR-002")["wbs_code"] == "1.1.1.5"
    be.add("subsystem", "Body Controller", path, ONTO, parent="SYS-HF", attributes={"safety_critical": "no"})
    third = be.add("vehicle", "Recovery Vehicle", path, ONTO, bare=True)
    assert [(c["id"], c["wbs_code"]) for c in third["created"]] == [("V-003", "1.0")]
    fam = be.add("family", "Recovery Family", path, ONTO, parent="V-003")
    var = be.add("variant", "Wrecker", path, ONTO, parent="FAM-003")
    var2 = be.add("variant", "Crane", path, ONTO, parent="FAM-003")
    assert (fam["id"], fam["wbs_code"], var["wbs_code"], var["attributes"], var2["attributes"], var2["wbs_code"]) == \
        ("FAM-003", "1.1", "1.1.1", {"role": "lead"}, {"role": "derivative"}, "1.1.2")

    data = yaml.safe_load(path.read_text())
    assert check_baseline(ONTO, data, settings.documents_dir) == []
    base = load_baseline(path)
    assert [(v.eid, v.props["wbs_code"]) for v in base.vertices if v.label == "Vehicle"] == \
        [("V-001", "1.0"), ("V-002", "1.0"), ("V-003", "1.0")]
    assert base.vertex("SYS-HF").props["wbs_code"] == "1.1.1.2"
    assert ("SUB-BC", "SYS-HF") in {(e.from_eid, e.to_eid) for e in base.edges}
    assert find_node(data, "VAR-003")[0] == "Variant"
    assert existing_structure(data)["systems"]["SYS-HF"]["variant"] == "VAR-002"       # the diagram reader sees it too
    # the first vehicle's own tree gained nothing
    assert yaml.safe_load(before)["vehicle"] == data["vehicle"]


def test_a_baseline_that_departs_from_the_standard_is_told_so(path):
    data = yaml.safe_load(path.read_text())
    assert wbs_findings(ONTO, data) == []
    var = data["vehicle"]["families"][0]["variants"][0]
    data["vehicle"]["wbs_code"] = "2.0"
    data["vehicle"]["families"][0]["wbs_code"] = "2.4"
    var["wbs_code"], var["role"] = "2.4.2", "lead"
    var["systems"][0]["wbs_code"] = "2.4.2.6"                      # Suspension/Steering at Vehicle Electronics' number
    var["systems"][1]["wbs_code"] = "2.4.2.6"
    var["systems"][2]["wbs_code"] = "2.4.2.16"                     # Power Package/Drive Train among the others
    found = wbs_findings(ONTO, data)
    text = "\n".join(str(f) for f in found)
    assert all(f.level == "warning" for f in found) and len(found) == 5, text
    for part in ("WBS 2.0: a ground vehicle system is 1.0", "WBS 2.4 is not a vehicle element",
                 "1.1 Family of Vehicles and 1.2 Secondary Vehicle", "WBS 2.4.2.6 is 'Vehicle Electronics'",
                 "WBS 2.4.2.6 is also SYS-SUSP's", "gives 'Power Package/Drive Train' the number 2.4.2.7"):
        assert part in text, part
    # they are part of the ontology check, as warnings: the baseline still loads
    assert {str(f) for f in found} <= {str(f) for f in check_baseline(ONTO, data)}
    # a candidate system without a code yet is passed over
    var["systems"].append({"id": "SYS-NEW", "name": "Fire Control", "status": "candidate"})
    assert len(wbs_findings(ONTO, data)) == 5


def test_names_the_standard_elements_go_by():
    for name, number in [("Suspension/Steering", 5), ("Steering", 5), ("Hull", 2), ("Hull/Frame", 2), ("Body and Cab", 2),
                         ("Vehicle Electronics (Vetronics)", 6), ("Power Package", 7), ("Drive Train", 7), ("Turret", 4),
                         ("Navigation", 12), ("Remote Piloting Systems", 12), ("FIRE CONTROL", 9), ("Communications", 14),
                         ("Integration, Assembly, Test, and Checkout", 1), ("Software Release 3", 15),
                         ("Control", None), ("Vehicle", None), ("System", None), ("Checkout", None), ("Recovery Crane", None),
                         ("Fire", None), ("", None)]:
        assert standard_element(ONTO, name) == number, name
    assert be.standard(ONTO)[0] == "MIL-STD-881F Appendix G, G.3 (13 May 2022)"
    assert any(ln.startswith("1.1.n.9") and "Fire Control" in ln for ln in be.standard(ONTO))


@pytest.mark.parametrize("kind, name, kw, message", [
    ("subsystem", "Pump", {"parent": "SYS-PWR"}, "say whether the subsystem is safety-critical"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "attributes": {"safety_critical": "perhaps"}}, "safety_critical: give yes or no"),
    ("subsystem", "Pump", {"attributes": {"safety_critical": "no"}}, r"which System does it go in\? Add --in ID"),
    ("subsystem", "Pump", {"parent": "VAR-001", "attributes": {"safety_critical": "no"}}, "VAR-001 is a Variant; a subsystem goes in a System"),
    ("subsystem", "Pump", {"parent": "SYS-NOPE", "attributes": {"safety_critical": "no"}}, "SYS-NOPE is not in the baseline"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "wbs": "3", "attributes": {"safety_critical": "no"}}, "has no WBS code of its own"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "attributes": {"safety_critical": "no", "os_class": "linux"}}, "os_class = 'linux': not an allowed value"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "attributes": {"safety_critical": "no", "colour": "red"}}, "'colour' is not an attribute of a Subsystem"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "attributes": {"safety_critical": "no", "status": "candidate"}}, "'status' is not an attribute"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "attributes": {"safety_critical": "no", "j1939_source_address": "x"}}, "must be a whole number"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "id": "SUB-DCL", "attributes": {"safety_critical": "no"}}, "the id SUB-DCL is already used"),
    ("subsystem", "Pump", {"parent": "SYS-PWR", "id": "PUMP-1", "attributes": {"safety_critical": "no"}}, "not in the form of a Subsystem id"),
    ("subsystem", "Engine Control Module", {"parent": "SYS-PWR", "attributes": {"safety_critical": "no"}}, "SUB-ENG-ECM is already a subsystem called"),
    ("system", "Turret", {"wbs": "2.1.1.4"}, "WBS 2.1.1.4 doesn't sit under its parent's 1.1.1"),
    ("system", "Turret", {"wbs": "9"}, "WBS 1.1.1.9 is 'Fire Control'"),
    ("vehicle", "Other", {"wbs": "2.0"}, "a ground vehicle system is WBS 1.0"),
    ("system", "Turret", {"id": "ICD-BRK-001"}, "already used"),
    ("system", "   ", {}, "give the system's name"),
    ("function", "Idle", {"parent": "SUB-ENG-ECM", "attributes": {"safety_classification": "high"}}, "safety_classification"),
    ("vehicle", "Other", {"parent": "V-001"}, "a vehicle is at the top"),
    ("system", "Turret", {"bare": True}, "go with `add vehicle` only"),
    ("wheel", "Front left", {}, "can add: vehicle, family, variant, system, subsystem, function"),
])
def test_what_is_refused_leaves_the_file_as_it_was(path, kind, name, kw, message):
    before = path.read_bytes()
    with pytest.raises(be.BaselineEditError, match=message):
        be.add(kind, name, path, ONTO, **kw)
    assert path.read_bytes() == before and list(path.parent.iterdir()) == [path]


def test_a_layout_the_command_cannot_follow_is_refused_not_guessed(path):
    text = path.read_text()
    one_line = text.replace('''                  j1939_source_address: 0       # "Engine #1"''',
                            '''                  j1939_source_address: 0       # "Engine #1"
                  functions: [{id: FN-ENG-IDLE, name: "Idle control"}]''')
    assert one_line != text
    path.write_text(one_line)
    with pytest.raises(be.BaselineEditError, match="`functions:` list at line .* is written on one line"):
        be.add("function", "Speed governing", path, ONTO, parent="SUB-ENG-ECM")
    assert path.read_text() == one_line
    # an empty list written `[]` is fine: it becomes a list with one entry
    path.write_text(text.replace('''                  j1939_source_address: 0       # "Engine #1"''',
                                 '''                  j1939_source_address: 0       # "Engine #1"
                  functions: []        # none yet'''))
    be.add("function", "Speed governing", path, ONTO, parent="SUB-ENG-ECM")
    assert "                  functions:  # none yet\n                    - id: FN-ENG-SG\n" in path.read_text()


def test_dry_run_shows_the_entry_and_writes_nothing(path):
    before = path.read_bytes()
    res = be.add("system", "Fire Control", path, ONTO, dry_run=True)
    assert res["written"] is False and res["text"].splitlines()[0] == "            - id: SYS-FC"
    assert path.read_bytes() == before
    with pytest.raises(be.BaselineEditError, match="doesn't sit under"):            # checked all the same
        be.add("system", "Fire Control", path, ONTO, wbs="7.7", dry_run=True)
    with pytest.raises(be.BaselineEditError, match="would not follow the standard"):
        be.add("system", "Fire Control", path, ONTO, wbs="4", dry_run=True)


def test_remove_takes_out_only_what_nothing_depends_on(path):
    before = path.read_text()
    for eid, why in [("SYS-SUSP", "it has SUB-BRK-ECU under it; document SSDD-SUSP-001 is attached to it"),
                     ("SUB-ENG-ECM", r"an interface \(SUB-J1939-BB → SUB-ENG-ECM\) uses it"),
                     ("V-001", "first vehicle"), ("ICD-BRK-001", "it is a document"), ("SUB-NOPE", "not an entry")]:
        with pytest.raises(be.BaselineEditError, match=why):
            be.remove(eid, path, ONTO)
    assert path.read_text() == before

    be.add("system", "Fire Control", path, ONTO)
    be.add("subsystem", "Turret Drive", path, ONTO, parent="SYS-FC", attributes={"safety_critical": "yes"})
    be.add("function", "Slew", path, ONTO, parent="SUB-TD")
    be.add("vehicle", "Second", path, ONTO, bare=True)
    assert be.remove("FN-TD-SLEW", path, ONTO, dry_run=True)["written"] is False
    for eid in ("FN-TD-SLEW", "SUB-TD", "SYS-FC", "V-002"):
        assert be.remove(eid, path, ONTO)["id"] == eid
        assert check_baseline(ONTO, yaml.safe_load(path.read_text())) == []
        load_baseline(path)
    assert path.read_text() == before                              # everything added is out again, to the byte
    data = yaml.safe_load(path.read_text())
    assert be.add("vehicle", "Second again", path, ONTO, bare=True)["id"] == "V-002"     # and the empty list takes an entry again
    assert yaml.safe_load(path.read_text())["vehicles"][0]["name"] == "Second again"

    # a function removed from the middle of a list leaves its neighbours
    be.remove("FN-BRK-SLIP", path, ONTO)
    assert [f["id"] for f in find_node(yaml.safe_load(path.read_text()), "SUB-BRK-ECU")[1]["functions"]] == ["FN-BRK-STATUS"]


def test_tree_lists_every_entry_with_what_is_attached(path):
    lines = be.tree(yaml.safe_load(path.read_text()))
    assert lines[0] == "V-001  SYNTH-LTV Ground Vehicle System  (WBS 1.0)"
    assert "      SYS-SUSP  Suspension/Steering  (WBS 1.1.1.5; documents: SSDD-SUSP-001)" in lines
    assert "        SUB-DCL  Driver Digital Cluster  (not safety-critical; documents: SDD-DCL-001, SRS-DCL-001)" in lines
    assert "          FN-BRK-SLIP  Wheel-slip regulation" in lines and len(lines) == 15


# ── the command line ─────────────────────────────────────────────────────────

def cli(*argv):
    ap = argparse.ArgumentParser()
    baseline_admin.add_arguments(ap)
    ns = ap.parse_args(list(argv))
    ns.env = None
    return ns


def test_command_line(path, capsys):
    s = replace(settings, baseline_yaml=path)
    assert baseline_admin.run(cli("show"), s=s) == 0
    assert "SYS-PWR  Power Package/Drive Train" in capsys.readouterr().out
    assert baseline_admin.run(cli("check"), s=s) == 0
    assert "passes the ontology check" in capsys.readouterr().out

    assert baseline_admin.run(cli("standard"), s=s) == 0
    assert "1.1.n.5    Suspension/Steering" in capsys.readouterr().out

    assert baseline_admin.run(cli("add", "system", "Fire Control", "--dry-run"), s=s) == 0
    out = capsys.readouterr().out
    assert "Would add System SYS-FC (Fire Control), WBS 1.1.1.9, in VAR-001" in out and "Nothing was written" in out
    assert "SYS-FC" not in path.read_text()

    assert baseline_admin.run(cli("add", "system", "Body Network", "--wbs", "6"), s=s) == 1
    assert "NOT CHANGED: not written: the WBS code would not follow the standard.\n  SYS-BN: WBS 1.1.1.6 is 'Vehicle Electronics'" \
        in capsys.readouterr().out
    assert baseline_admin.run(cli("add", "system", "Fire Control"), s=s) == 0
    out = capsys.readouterr().out
    assert "Added System SYS-FC" in out and 'Next: python -m crew.main baseline add subsystem "..." --in SYS-FC' in out
    assert "The graph was not changed. To bring it in step: python -m crew.main graph reload" in out

    assert baseline_admin.run(cli("add", "subsystem", "Turret Drive Controller (TDC)", "--in", "SYS-FC"), s=s) == 1
    assert "NOT CHANGED: say whether the subsystem is safety-critical" in capsys.readouterr().out
    assert baseline_admin.run(cli("add", "subsystem", "Turret Drive Controller (TDC)", "--in", "SYS-FC", "--safety-critical", "yes",
                                  "--os", "rtos", "--alias", "turret ECU", "--alias", "TDC", "--j1939-address", "42",
                                  "--set", "repo_url=https://git.example/tdc"), s=s) == 0
    capsys.readouterr()
    node = find_node(yaml.safe_load(path.read_text()), "SUB-TDC")[1]
    assert node == {"id": "SUB-TDC", "name": "Turret Drive Controller (TDC)", "safety_critical": True, "os_class": "rtos",
                    "aliases": ["turret ECU", "TDC"], "j1939_source_address": 42, "repo_url": "https://git.example/tdc"}
    assert baseline_admin.run(cli("add", "function", "Slew control", "--in", "SUB-TDC", "--set", "nonsense"), s=s) == 1
    assert "--set takes KEY=VALUE" in capsys.readouterr().out

    graph = FakeGraph()
    assert baseline_admin.run(cli("add", "function", "Slew control", "--in", "SUB-TDC", "--description", "Turns the turret.",
                                  "--reload-graph"), s=s, graph=graph) == 0
    out = capsys.readouterr().out
    assert "Added Function FN-TDC-SC (Slew control), in SUB-TDC" in out and "Graph reloaded from the baseline." in out
    assert {"SYS-FC", "SUB-TDC", "FN-TDC-SC"} <= set(graph.v)

    assert baseline_admin.run(cli("add", "vehicle", "SYNTH-MTV"), s=s) == 0
    out = capsys.readouterr().out
    assert ("Added Vehicle V-002 (SYNTH-MTV), WBS 1.0\n  with VehicleFamily FAM-002 (SYNTH-MTV Family of Vehicles), WBS 1.1\n"
            "  with Variant VAR-002 (Lead Variant), WBS 1.1.1") in out
    assert 'Next: python -m crew.main baseline add system "..." --in VAR-002' in out

    assert baseline_admin.run(cli("remove", "SYS-FC"), s=s) == 1
    assert "NOT CHANGED: SYS-FC was not removed: it has SUB-TDC under it" in capsys.readouterr().out
    assert baseline_admin.run(cli("remove", "FN-TDC-SC"), s=s) == 0
    assert "Removed Function FN-TDC-SC (Slew control) from SUB-TDC" in capsys.readouterr().out
    assert baseline_admin.run(cli("check"), s=s) == 0


def test_the_crew_command_line_offers_it():
    text = (baseline_admin.ROOT / "crew" / "main.py").read_text()
    assert 'add("baseline", cmd_baseline' in text and "baseline_admin.add_arguments(bl)" in text
