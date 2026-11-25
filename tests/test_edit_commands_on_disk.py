"""The baseline and ontology commands, run the way a person runs them: as
`python -m crew.baseline_admin ...` / `python -m crew.ontology_admin ...` in
a process of their own, with the files they work on found through the
settings (ESAM_BASELINE_YAML, ESAM_ONTOLOGY_YAML). After each command the
file ON DISK is read again and must hold the change; the other tests call
the functions directly with settings handed to them.

(`python -m crew.main baseline|ontology ...` goes to the same two `run`
functions; crew.main itself needs CrewAI's dependencies to import.)"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

from esamkit.config import PROJECT_ROOT, settings


@pytest.fixture
def files(tmp_path):
    base, onto = tmp_path / "baseline.yaml", tmp_path / "ontology.yaml"
    shutil.copy2(settings.baseline_yaml, base)
    shutil.copy2(settings.ontology_yaml, onto)
    return base, onto


def command(files, module, *argv):
    env = {**os.environ, "ESAM_BASELINE_YAML": str(files[0]), "ESAM_ONTOLOGY_YAML": str(files[1]), "ESAM_ENV": "local"}
    return subprocess.run([sys.executable, "-m", module, *argv], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_repository_files_are_the_ones_the_commands_find():
    """Without the two variables the commands work on the repository's own files."""
    env = {k: v for k, v in os.environ.items() if k not in ("ESAM_BASELINE_YAML", "ESAM_ONTOLOGY_YAML")}
    got = subprocess.run([sys.executable, "-m", "crew.baseline_admin", "add", "system", "Armament", "--dry-run"],
                         cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert got.returncode == 0, got.stdout + got.stderr
    assert f"Nothing was written (--dry-run): {PROJECT_ROOT / 'data' / 'baseline' / 'vehicle_decomposition.yaml'}" in got.stdout
    got = subprocess.run([sys.executable, "-m", "crew.ontology_admin", "add", "word", "software", "bootloader", "--dry-run"],
                         cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert f"Nothing was written (--dry-run): {PROJECT_ROOT / 'data' / 'ontology' / 'esam_ontology.yaml'}" in got.stdout


def test_baseline_commands_change_the_file_on_disk(files):
    base, _ = files
    original = base.read_text()
    steps = [
        (("add", "system", "Fire Control"), lambda d: d["vehicle"]["families"][0]["variants"][0]["systems"][-1]
         == {"id": "SYS-FC", "name": "Fire Control", "wbs_code": "1.1.1.9", "universality": "universal"}),
        (("add", "subsystem", "Turret Drive Controller (TDC)", "--in", "SYS-FC", "--safety-critical", "yes", "--os", "rtos"),
         lambda d: d["vehicle"]["families"][0]["variants"][0]["systems"][-1]["subsystems"]
         == [{"id": "SUB-TDC", "name": "Turret Drive Controller (TDC)", "safety_critical": True, "os_class": "rtos"}]),
        (("add", "function", "Turret slew control", "--in", "SUB-TDC", "--description", "Drives the turret."),
         lambda d: d["vehicle"]["families"][0]["variants"][0]["systems"][-1]["subsystems"][0]["functions"]
         == [{"id": "FN-TDC-TSC", "name": "Turret slew control", "description": "Drives the turret."}]),
        (("add", "variant", "Ambulance"), lambda d: d["vehicle"]["families"][0]["variants"][-1]
         == {"id": "VAR-002", "name": "Ambulance", "wbs_code": "1.1.2", "role": "derivative"}),
        (("add", "family", "Recovery Vehicles"), lambda d: d["vehicle"]["families"][-1]
         == {"id": "FAM-002", "name": "Recovery Vehicles", "wbs_code": "1.2"}),
        (("add", "vehicle", "SYNTH-MTV"), lambda d: [v["id"] for v in d["vehicles"]] == ["V-002"]
         and d["vehicles"][0]["families"][0]["variants"][0]["id"] == "VAR-003"),
    ]
    for argv, holds in steps:
        before = digest(base)
        got = command(files, "crew.baseline_admin", *argv)
        assert got.returncode == 0, got.stdout + got.stderr
        assert f"Written to {base}" in got.stdout
        assert digest(base) != before, f"{argv}: the file on disk did not change"
        assert holds(yaml.safe_load(base.read_text())), f"{argv}: the file on disk does not hold the entry"
    assert command(files, "crew.baseline_admin", "check").returncode == 0
    shown = command(files, "crew.baseline_admin", "show").stdout
    assert "FN-TDC-TSC  Turret slew control" in shown and "V-002  SYNTH-MTV  (WBS 1.0)" in shown

    # a refused command and a dry run leave the bytes alone
    before = digest(base)
    assert command(files, "crew.baseline_admin", "add", "subsystem", "Pump", "--in", "SYS-FC").returncode == 1
    assert command(files, "crew.baseline_admin", "add", "system", "Armament", "--in", "VAR-001", "--dry-run").returncode == 0
    assert digest(base) == before

    # every removal is on disk too, and taking it all out again gives the file back to the byte
    for eid in ("VAR-003", "FAM-003", "V-002", "FAM-002", "VAR-002", "FN-TDC-TSC", "SUB-TDC", "SYS-FC"):
        before = digest(base)
        got = command(files, "crew.baseline_admin", "remove", eid)
        assert got.returncode == 0, got.stdout + got.stderr
        assert digest(base) != before and f"id: {eid}\n" not in base.read_text()
    assert base.read_text() == original


def test_ontology_commands_change_the_file_on_disk(files):
    base, onto = files
    steps = [
        (("add", "value", "os_class", "hypervisor", "Separation kernel"), lambda d: d["value_sets"]["os_class"]["hypervisor"] == "Separation kernel"),
        (("add", "value", "supplier_tier", "tier1", "Delivers to the prime", "--new-set"), lambda d: d["value_sets"]["supplier_tier"] == {"tier1": "Delivers to the prime"}),
        (("add", "attribute", "Subsystem", "supplier", "--description", "Who builds it"),
         lambda d: d["classes"]["Subsystem"]["attributes"]["supplier"] == {"type": "text", "description": "Who builds it"}),
        (("add", "attribute", "Subsystem", "tier", "--type", "enum", "--values", "supplier_tier"),
         lambda d: d["classes"]["Subsystem"]["attributes"]["tier"] == {"type": "enum", "values": "supplier_tier"}),
        (("set", "attribute", "Subsystem", "j1939_source_address", "--max", "255"),
         lambda d: d["classes"]["Subsystem"]["attributes"]["j1939_source_address"]["max"] == 255),
        (("add", "document-type", "sss", "System/Subsystem Specification", "--attaches-to", "Vehicle", "System"),
         lambda d: d["document_types"]["sss"]["attaches_to"] == ["Vehicle", "System"] and "|SSS)" in d["classes"]["DesignArtifact"]["id_pattern"]),
        (("add", "rule", "--when", "software", "has_interfaces", "--require", "sss", "--why", "Interfaced software has one."),
         lambda d: d["rules"]["required_documents"][-1] == {"when": ["software", "has_interfaces"], "require": ["sss"], "why": "Interfaced software has one."}),
        (("add", "word", "software", "bootloader"), lambda d: d["requirement_classification"]["subjects"]["software"][-1] == "bootloader"),
        (("add", "answer", "icd", "can bus"), lambda d: d["document_types"]["icd"]["answers"][-1] == "can bus"),
        (("add", "synonym", "icd", "Interface Sheet"), lambda d: d["document_types"]["icd"]["synonyms"][-1] == "Interface Sheet"),
    ]
    version = 13
    for argv, holds in steps:
        before = digest(onto)
        got = command(files, "crew.ontology_admin", *argv)
        version += 1
        assert got.returncode == 0, got.stdout + got.stderr
        assert f"Written to {onto} (now version 0.{version})" in got.stdout
        assert digest(onto) != before, f"{argv}: the file on disk did not change"
        data = yaml.safe_load(onto.read_text())
        assert holds(data) and data["metadata"]["version"] == f"0.{version}", f"{argv}: the file on disk does not hold the change"

    # the next process reads what the last one wrote: the baseline command accepts the new attribute and value
    got = command(files, "crew.baseline_admin", "add", "subsystem", "Winch Controller", "--in", "SYS-PWR", "--safety-critical", "no",
                  "--os", "hypervisor", "--set", "supplier=Acme", "--set", "tier=tier1")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "                  supplier: Acme\n                  tier: tier1\n" in base.read_text()
    assert command(files, "crew.ontology_admin", "check").returncode == 0
    assert "supplier" in command(files, "crew.ontology_admin", "show", "Subsystem").stdout

    before = digest(onto)
    assert command(files, "crew.ontology_admin", "remove", "attribute", "Subsystem", "supplier").returncode == 1     # SUB-WC has it
    assert command(files, "crew.ontology_admin", "add", "word", "software", "kernel", "--dry-run").returncode == 0
    assert digest(onto) == before
    for argv, gone in [(("remove", "rule", "4"), lambda d: len(d["rules"]["required_documents"]) == 3),
                       (("remove", "document-type", "sss"), lambda d: "sss" not in d["document_types"]),
                       (("remove", "word", "software", "bootloader"), lambda d: "bootloader" not in d["requirement_classification"]["subjects"]["software"]),
                       (("remove", "answer", "icd", "can bus"), lambda d: "can bus" not in d["document_types"]["icd"]["answers"]),
                       (("remove", "attribute", "Subsystem", "tsn_profile"), lambda d: "tsn_profile" not in d["classes"]["Subsystem"]["attributes"])]:
        before = digest(onto)
        got = command(files, "crew.ontology_admin", *argv)
        assert got.returncode == 0, got.stdout + got.stderr
        assert digest(onto) != before and gone(yaml.safe_load(onto.read_text())), argv


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a folder this user cannot write to")
def test_a_file_that_cannot_be_written_is_reported_not_passed_over(files):
    base, onto = files
    folder = base.parent
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        got = command(files, "crew.baseline_admin", "add", "system", "Fire Control")
        assert got.returncode == 1 and "NOT CHANGED: NOT written" in got.stdout and "Written to" not in got.stdout
        got = command(files, "crew.ontology_admin", "add", "word", "software", "bootloader")
        assert got.returncode == 1 and "NOT CHANGED: NOT written" in got.stdout and "Written to" not in got.stdout
    finally:
        folder.chmod(stat.S_IRWXU)
    assert "SYS-FC" not in base.read_text() and "bootloader" not in onto.read_text()


def test_a_write_that_fails_is_an_error_and_leaves_the_file(files, monkeypatch):
    """The same, where the test runs as a user who can write anywhere: the replace itself is made to fail."""
    from esamkit import baseline_edit as be
    from esamkit import ontology_edit as oe
    base, onto = files
    kept = digest(base), digest(onto)

    def denied(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(be.BaselineEditError, match="NOT written: .*baseline.yaml could not be changed .Permission denied."):
        be.add("system", "Fire Control", base)
    with pytest.raises(oe.OntologyEditError, match="NOT written: .*ontology.yaml could not be changed .Permission denied."):
        oe.Edit(onto, base).word("software", ["bootloader"])
    assert (digest(base), digest(onto)) == kept and sorted(x.name for x in base.parent.iterdir()) == ["baseline.yaml", "ontology.yaml"]
