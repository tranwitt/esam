"""Changing the ontology from the command line (esamkit/ontology_edit.py, crew/ontology_admin.py)."""

import argparse
import shutil
from dataclasses import replace

import pytest
import yaml

from crew import baseline_admin, ontology_admin
from esamkit import baseline_edit as be
from esamkit import ontology_edit as oe
from esamkit.baseline import load_baseline
from esamkit.config import settings
from esamkit.ontology import Ontology, check_baseline, load_ontology


@pytest.fixture
def s(tmp_path):
    shutil.copy2(settings.ontology_yaml, tmp_path / "ontology.yaml")
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    return replace(settings, ontology_yaml=tmp_path / "ontology.yaml", baseline_yaml=tmp_path / "baseline.yaml")


def edit(s, **kw):
    return oe.Edit(s.ontology_yaml, s.baseline_yaml, **kw)


def onto(s) -> Ontology:
    return load_ontology(s.ontology_yaml)


def diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """(lines only in before, lines only in after), the version line left out."""
    a, b = before.split("\n"), after.split("\n")
    skip = lambda ln: ln.strip().startswith("version:")                  # noqa: E731
    return [x for x in a if x not in b and not skip(x)], [x for x in b if x not in a and not skip(x)]


def test_the_sample_ontology_has_its_sections_where_they_belong():
    data = load_ontology().data
    assert list(data["placement"]) == ["baseline", "document_header", "requirement_line", "test_case_fields"]
    assert list(data["requirement_classification"]) == ["id_marker", "unmarked", "default", "subjects"]


def test_values_and_value_sets(s):
    before = s.ontology_yaml.read_text()
    res = edit(s).add_value("os_class", "hypervisor", "Separation  kernel hosting several partitions")
    assert res["version"] == "0.14" and res["written"] and res["baseline_errors"] == []
    assert diff(before, s.ontology_yaml.read_text()) == ([], ['    hypervisor: "Separation kernel hosting several partitions"'])
    assert list(onto(s).value_sets["os_class"])[-1] == "hypervisor"
    # the baseline editor takes the new value straight away
    be.add("subsystem", "Partition Manager", s.baseline_yaml, onto(s), parent="SYS-VETR",
           attributes={"safety_critical": "yes", "os_class": "hypervisor"})

    res = edit(s).add_value("supplier_tier", "tier1", "Delivers to the prime", new_set=True)
    assert res["text"] == '  supplier_tier:\n    tier1: "Delivers to the prime"' and "no attribute uses" in res["notes"][0]
    text = s.ontology_yaml.read_text()
    assert text.index("  supplier_tier:") < text.index("# ── The WBS numbers the standard fixes")      # inside value_sets, before the next section's comment

    for call, why in [(lambda e: e.add_value("os_class", "rtos", "again"), "already has the value 'rtos'"),
                      (lambda e: e.add_value("nope", "x", "y"), "there is no value set 'nope'.*--new-set"),
                      (lambda e: e.add_value("os_class", "x", "y", new_set=True), "already there: leave --new-set out"),
                      (lambda e: e.add_value("os_class", "x", " "), "what it means"),
                      (lambda e: e.add_value("document_types", "sss", "x"), "ontology add document-type"),
                      (lambda e: e.remove_value("os_class", "vxworks"), "has no value 'vxworks'"),
                      (lambda e: e.remove_value("supplier_tier", "tier1"), "cannot be left empty"),
                      (lambda e: e.remove_value("os_class", "hypervisor"), "SUB-PM: os_class = 'hypervisor': not an allowed value")]:
        kept = s.ontology_yaml.read_bytes()
        with pytest.raises(oe.OntologyEditError, match=why):
            call(edit(s))
        assert s.ontology_yaml.read_bytes() == kept
    edit(s).add_value("os_class", "bare_metal", "No operating system")
    assert edit(s).remove_value("os_class", "bare_metal")["text"] == '    bare_metal: "No operating system"'
    assert "bare_metal" not in onto(s).value_sets["os_class"] and "hypervisor" in onto(s).value_sets["os_class"]


def test_attributes(s):
    before = s.ontology_yaml.read_text()
    edit(s).add_attribute("Subsystem", "supplier", description="Who builds it")
    edit(s).add_attribute("Subsystem", "tier", type="enum", values="link_status")
    edit(s).add_attribute("Function", "asil", pattern=r"^(QM|[A-D])$")
    edit(s).add_attribute("System", "mass_kg", type="integer", min="0", max="90000", description="Design mass")
    gone, new = diff(before, s.ontology_yaml.read_text())
    assert gone == [] and new == [
        '      mass_kg: { type: integer, min: 0, max: 90000, description: "Design mass" }',
        '      supplier: { type: text, description: "Who builds it" }',
        "      tier: { type: enum, values: link_status }",
        "      asil: { type: text, pattern: '^(QM|[A-D])$' }"]
    attrs = onto(s).classes["Subsystem"]["attributes"]
    assert list(attrs)[-2:] == ["supplier", "tier"] and onto(s).classes["Function"]["attributes"]["asil"]["pattern"] == "^(QM|[A-D])$"

    # ... and the baseline command, the check and the graph loader follow
    o = onto(s)
    be.add("subsystem", "Winch Controller", s.baseline_yaml, o, parent="SYS-PWR",
           attributes={"safety_critical": "no", "supplier": "Acme Hydraulics", "tier": "candidate"})
    be.add("function", "Spool out", s.baseline_yaml, o, parent="SUB-WC", attributes={"asil": "B"})
    with pytest.raises(be.BaselineEditError, match="asil"):
        be.add("function", "Spool in", s.baseline_yaml, o, parent="SUB-WC", attributes={"asil": "E"})
    base = load_baseline(s.baseline_yaml)
    assert base.vertex("SUB-WC").props["supplier"] == "Acme Hydraulics" and base.vertex("FN-WC-SO").props["asil"] == "B"
    assert check_baseline(o, yaml.safe_load(s.baseline_yaml.read_text())) == []

    for call, why in [(lambda e: e.add_attribute("Subsystem", "supplier"), "already has 'supplier'.*ontology set attribute"),
                      (lambda e: e.add_attribute("Subsystem", "name"), "already has 'name'"),
                      (lambda e: e.add_attribute("Wheel", "size"), "there is no class 'Wheel'. The classes: Vehicle"),
                      (lambda e: e.add_attribute("Subsystem", "Bad Name"), "lower-case words joined by _"),
                      (lambda e: e.add_attribute("Subsystem", "x", type="colour"), "type is one of: text, integer"),
                      (lambda e: e.add_attribute("Subsystem", "x", type="enum"), "add --values SET"),
                      (lambda e: e.add_attribute("Subsystem", "x", type="enum", values="nope"), "there is no value set 'nope'"),
                      (lambda e: e.add_attribute("Subsystem", "x", values="os_class"), "--values goes with --type enum"),
                      (lambda e: e.add_attribute("Subsystem", "x", pattern="(["), "not a regular expression"),
                      (lambda e: e.add_attribute("Subsystem", "x", type="integer", min="5", max="2"), "--min 5 is above --max 2"),
                      (lambda e: e.add_attribute("Subsystem", "x", min="5"), "--min goes with --type integer"),
                      (lambda e: e.add_attribute("Subsystem", "x", required=True), "SUB-BRK-ECU: required attribute 'x' is missing"),
                      (lambda e: e.set_attribute("Subsystem", "os_class", required=True), "SUB-J1939-BB: required attribute 'os_class'"),
                      (lambda e: e.set_attribute("Subsystem", "nope", required=True), "Subsystem has no attribute 'nope'. It has: safety_critical"),
                      (lambda e: e.set_attribute("Subsystem", "supplier", type="text"), "already like that"),
                      (lambda e: e.remove_attribute("Subsystem", "supplier"), "is set on SUB-WC in the baseline"),
                      (lambda e: e.remove_attribute("Subsystem", "nope"), "has no attribute 'nope'")]:
        kept = s.ontology_yaml.read_bytes()
        with pytest.raises(oe.OntologyEditError, match=why):
            call(edit(s))
        assert s.ontology_yaml.read_bytes() == kept and not list(s.ontology_yaml.parent.glob("*.tmp"))

    # set: only what is given changes; the comment on the line stays; "" takes a key off
    line = "      j1939_source_address: { type: integer, min: 0, max: 253 }"
    s.ontology_yaml.write_text(s.ontology_yaml.read_text().replace(line, line + "   # 254 and 255 are reserved"))
    res = edit(s).set_attribute("Subsystem", "j1939_source_address", max="255", description="J1939 source address")
    assert res["text"] == ('      j1939_source_address: { type: integer, min: 0, max: 255, description: "J1939 source address" }'
                           "  # 254 and 255 are reserved")
    edit(s).set_attribute("Subsystem", "safety_classification", pattern="")
    assert "pattern" not in onto(s).classes["Subsystem"]["attributes"]["safety_classification"]
    edit(s).set_attribute("Subsystem", "safety_critical", required=False)
    assert "required" not in onto(s).classes["Subsystem"]["attributes"]["safety_critical"]
    assert edit(s).remove_attribute("Subsystem", "tsn_profile")["text"] == "      tsn_profile: { type: text }"

    # --force writes a change the baseline does not meet yet, and says what now fails
    res = edit(s, force=True).set_attribute("Subsystem", "supplier", required=True)
    assert res["written"] and "SUB-DCL: required attribute 'supplier' is missing" in res["baseline_errors"]
    assert onto(s).classes["Subsystem"]["attributes"]["supplier"]["required"] is True


def test_document_types_rules_and_what_they_bring(s):
    before = s.ontology_yaml.read_text()
    res = edit(s).add_document_type("SSS", "System/Subsystem Specification", standard="MIL-STD-498",
                                    answers=["system requirement", "capability"], synonyms=["system spec"],
                                    attaches_to=["Vehicle", "System"])
    assert res["text"].split("\n") == ["  sss:", '    name: "System/Subsystem Specification"', '    standard: "MIL-STD-498"',
                                       "    attaches_to: [Vehicle, System]", "    answers: [system requirement, capability]",
                                       '    synonyms: ["system spec"]']
    assert "document ids may now start with SSS-" in res["notes"][0]
    o = onto(s)
    assert o.attach_classes("sss") == ["Vehicle", "System"] and o.doc_type_cues()["sss"] == ["system requirement", "capability", "system spec"]
    assert o.classes["DesignArtifact"]["id_pattern"] == r"^(ICD|SRS|SDD|SSDD|STD|STP|STR|SSS)-[A-Z0-9]+-00[1-9]$"
    assert o.check_self() == [] and "sss" in o.allowed("document_types")

    res = edit(s).add_rule(["software", "safety_critical"], ["sss", "icd"], "Safety-critical software has a system spec.")
    assert res["change"] == "rule 4 added: when software, safety_critical require sss, icd"
    assert onto(s).required_documents({"os_class": "rtos", "safety_critical": True}, False).keys() >= {"srs", "sdd", "std", "sss", "icd"}

    for call, why in [(lambda e: e.add_document_type("sss", "Again"), "sss is already there"),
                      (lambda e: e.add_document_type("x", "Too short"), "2 to 8 letters or digits"),
                      (lambda e: e.add_document_type("ocd", " "), "full name in quotes"),
                      (lambda e: e.add_document_type("ocd", "Operational Concept", attaches_to=["Function"]),
                       "cannot be attached to a Function: the choices are Vehicle, System, Subsystem"),
                      (lambda e: e.add_rule(["software"], ["srs"], "again"), "rule 1 already says that"),
                      (lambda e: e.add_rule(["armoured"], ["srs"], "x"), "'armoured' is not a condition. The conditions: software"),
                      (lambda e: e.add_rule(["software"], ["nope"], "x"), "there is no document type 'nope'"),
                      (lambda e: e.add_rule(["software"], ["std"], " "), "a rule needs --when"),
                      (lambda e: e.remove_rule(9), "there are 4 rules"),
                      (lambda e: e.remove_document_type("sss"), "was not removed: rule 4 requires it"),
                      (lambda e: e.remove_document_type("icd"), "document ICD-BRK-001 is one; rule 3 requires it; rule 4")]:
        kept = s.ontology_yaml.read_bytes()
        with pytest.raises(oe.OntologyEditError, match=why):
            call(edit(s))
        assert s.ontology_yaml.read_bytes() == kept

    assert edit(s).remove_rule(4)["text"].split("\n")[0] == "    - when: [software, safety_critical]"
    assert len(onto(s).rules["required_documents"]) == 3
    assert edit(s).remove_rule(2)["change"] == "rule 2 removed (when software, safety_critical require std)"
    assert [r["require"] for r in onto(s).rules["required_documents"]] == [["srs", "sdd"], ["icd"]]
    edit(s, keep_version=True).add_rule(["software", "safety_critical"], ["std"],
                                        "Safety-critical software must have a documented test description.")
    res = edit(s).remove_document_type("sss")
    assert "may no longer start with SSS-" in res["notes"][0]
    # back to where it started, but for the version and the order of the rules
    assert diff(before, s.ontology_yaml.read_text()) == ([], [])
    assert onto(s).data["metadata"]["version"] == "0.18"


def test_word_lists(s):
    before = s.ontology_yaml.read_text()
    res = edit(s).word("software", ["Bootloader", "boot  loader", "task"])
    assert res["change"] == "words that mark a software requirement: added bootloader, boot loader"
    assert res["text"].split("\n") == [
        "    software: [software, firmware, csci, csc, application, program, code, algorithm, task, routine, function block,",
        "               bootloader, boot loader]"]
    res = edit(s).word("system", ["gateway", "software"])
    assert "software also marks another level" in res["notes"][0]
    rc = onto(s).requirement_classification()["subjects"]
    assert rc["software"][-2:] == ["bootloader", "boot loader"] and rc["system"][-2:] == ["gateway", "software"]
    edit(s).word("system", ["software", "gateway"], add=False)
    edit(s).word("software", ["boot loader", "bootloader"], add=False)
    assert diff(before, s.ontology_yaml.read_text()) == ([], [])          # the lists are back as they were written

    edit(s).answer("icd", ["CAN bus", "baud rate"])
    edit(s).synonym("icd", ["Interface Sheet"])
    edit(s).synonym("stp", ["V&V plan"])
    text = s.ontology_yaml.read_text()
    assert "    answers: [interface, message, pgn, signal, j1939, transmit, receive, consume, send, can bus, baud rate]\n" in text
    assert '    synonyms: ["IRS", "IDD", "interface spec", "interface document", "Interface Sheet"]\n' in text
    assert onto(s).doc_type_cues()["stp"][-1] == "v&v plan"
    for call, why in [(lambda e: e.word("hardware", ["x"]), "the levels are: software, system"),
                      (lambda e: e.word("software", ["software"]), "already has: software"),
                      (lambda e: e.word("software", ["zzz"], add=False), "does not have: zzz"),
                      (lambda e: e.word("software", [" "]), "give at least one word"),
                      (lambda e: e.answer("nope", ["x"]), "there is no document type 'nope'")]:
        with pytest.raises(oe.OntologyEditError, match=why):
            call(edit(s))


def test_dry_run_and_what_stays(s):
    before = s.ontology_yaml.read_text()
    res = edit(s, dry_run=True).add_attribute("Subsystem", "supplier")
    assert res["written"] is False and res["text"] == "      supplier: { type: text }" and s.ontology_yaml.read_text() == before
    edit(s).add_attribute("Subsystem", "supplier")
    after = s.ontology_yaml.read_text()
    comments = [ln for ln in before.split("\n") if ln.lstrip().startswith("#")]
    assert [ln for ln in after.split("\n") if ln.lstrip().startswith("#")] == comments and len(comments) > 80
    assert len(after.split("\n")) == len(before.split("\n")) + 1


def test_show(s):
    o = onto(s)
    everything = "\n".join(oe.show(o))
    assert everything.startswith("ESAM ontology v0.13") and "── Required-document rules ──" in everything
    assert oe.show(o, "rules")[1] == "2. when software, safety_critical: require std  — Safety-critical software must have a documented test description."
    assert oe.show(o, "os_class")[0] == "os_class:" and any(ln.split()[0] == "rtos" for ln in oe.show(o, "os_class"))
    sub = oe.show(o, "Subsystem")
    assert sub[0] == "Subsystem  (id ^SUB-[A-Z0-9-]+$; part of System)"
    assert "  safety_critical          boolean, REQUIRED  — Can its failure contribute to a hazard?" in sub
    assert any("one of os_class" in ln for ln in sub) and any("0..253" in ln for ln in sub)
    assert oe.show(o, "words")[0] == "ids: SYS-SRS-... is system, SW-SRS-... is software; an unmarked SRS-... is system"
    with pytest.raises(oe.OntologyEditError, match="show takes a section"):
        oe.show(o, "nonsense")


# ── the command line ─────────────────────────────────────────────────────────

def cli(module, *argv):
    ap = argparse.ArgumentParser()
    module.add_arguments(ap)
    ns = ap.parse_args(list(argv))
    ns.env = None
    return ns


def test_command_line(s, capsys):
    run = lambda *argv: ontology_admin.run(cli(ontology_admin, *argv), s=s)        # noqa: E731
    assert run("show", "documents") == 0 and "icd    Interface Control Document  (attached to Subsystem)" in capsys.readouterr().out
    assert run("show", "nonsense") == 1
    capsys.readouterr()
    assert run("check") == 0 and "The ontology is consistent and the baseline passes it" in capsys.readouterr().out

    assert run("add", "value", "os_class", "hypervisor", "Separation kernel", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Would change: value 'hypervisor' added to os_class" in out and "Nothing was written" in out
    assert run("add", "value", "os_class", "hypervisor", "Separation kernel") == 0
    assert "(now version 0.14)" in capsys.readouterr().out
    assert run("add", "attribute", "Subsystem", "supplier", "--description", "Who builds it") == 0
    assert '      supplier: { type: text, description: "Who builds it" }' in capsys.readouterr().out
    assert run("add", "attribute", "Subsystem", "tier", "--type", "enum") == 1
    assert "NOT CHANGED: an enum attribute takes its values from a value set" in capsys.readouterr().out
    assert run("set", "attribute", "Subsystem", "supplier", "--required", "yes") == 1
    assert "NOT CHANGED: not written: the baseline would no longer pass the ontology check.\n  SUB-BRK-ECU" in capsys.readouterr().out
    assert run("set", "attribute", "Subsystem", "supplier", "--required", "yes", "--force", "--keep-version") == 0
    out = capsys.readouterr().out
    assert "The baseline now FAILS the ontology check (--force):" in out and "(now version" not in out
    assert run("check") == 1
    assert "required attribute 'supplier' is missing" in capsys.readouterr().out
    assert run("set", "attribute", "Subsystem", "supplier", "--required", "no") == 0

    assert run("add", "document-type", "sss", "System/Subsystem Specification", "--attaches-to", "Vehicle", "System",
               "--answers", "system requirement", "--synonyms", "system spec") == 0
    assert run("add", "rule", "--when", "software", "has_interfaces", "--require", "sss", "--why", "Interfaced software has one.") == 0
    assert run("add", "word", "software", "bootloader") == 0
    assert run("add", "answer", "sss", "capability") == 0
    assert run("add", "synonym", "sss", "A-spec") == 0
    capsys.readouterr()
    o = onto(s)
    assert o.document_types["sss"] == {"name": "System/Subsystem Specification", "attaches_to": ["Vehicle", "System"],
                                       "answers": ["system requirement", "capability"], "synonyms": ["system spec", "A-spec"]}
    assert o.check_self() == [] and "bootloader" in o.requirement_classification()["subjects"]["software"]

    # the baseline command reads the same ontology: the new attribute is an option there
    assert baseline_admin.run(cli(baseline_admin, "add", "subsystem", "Winch Controller", "--in", "SYS-PWR", "--safety-critical", "no",
                                  "--os", "hypervisor", "--set", "supplier=Acme"), s=s) == 0
    capsys.readouterr()
    assert run("remove", "attribute", "Subsystem", "supplier") == 1
    assert "is set on SUB-WC in the baseline" in capsys.readouterr().out
    for argv in (["remove", "rule", "4"], ["remove", "document-type", "sss"], ["remove", "word", "software", "bootloader"],
                 ["add", "value", "os_class", "bare_metal", "No operating system"], ["remove", "value", "os_class", "bare_metal"]):
        assert run(*argv) == 0, argv
    assert "Changed: value 'bare_metal' removed from os_class" in capsys.readouterr().out
    assert run("remove", "value", "os_class", "gpos") == 1
    assert "SUB-DCL: os_class = 'gpos': not an allowed value" in capsys.readouterr().out
    assert run("check") == 0


def test_the_crew_command_line_offers_it():
    text = (ontology_admin.ROOT / "crew" / "main.py").read_text()
    assert 'add("ontology", cmd_ontology' in text and "ontology_admin.add_arguments(on)" in text
