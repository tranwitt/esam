"""A system-level document: the SSDD is attached to the vehicle system (V-001)
or to one of its systems, where every other document type is attached to a
subsystem. The ontology says so (document_types.ssdd.attaches_to); the
baseline loader, the ontology check and the intake follow it."""

import copy
import shutil
from dataclasses import replace

import pytest
import yaml

import wordfiles as wf
from esamkit import intake
from esamkit.baseline import load_baseline
from esamkit.config import PROJECT_ROOT, settings
from esamkit.intake import baseline_line, ingest, ontology_check, read_header, receive, review_document, undo
from esamkit.ontology import Ontology, check_baseline, default_ontology, find_node

STAGED = PROJECT_ROOT / "lessons" / "06_missing_document" / "staged" / "SSDD-LTV-001.md"
ENTRY = {"id": "SSDD-LTV-001", "name": "SYNTH-LTV Vehicle System/Subsystem Design Description", "type": "ssdd",
         "revision": "A", "attached_to": "V-001", "file": "SSDD-LTV-001.md"}


@pytest.fixture
def onto():
    return default_ontology()


def test_the_ontology_lets_an_ssdd_sit_on_the_vehicle_a_system_or_a_subsystem(onto):
    assert onto.attach_classes("ssdd") == ["Vehicle", "System", "Subsystem"]
    assert onto.attach_classes("SSDD") == onto.attach_classes("ssdd")
    for other in ("srs", "sdd", "icd", "std", "stp", "str", None, "nonsense"):
        assert onto.attach_classes(other) == ["Subsystem"]
    assert onto.relationships["HAS_ARTIFACT"]["from"] == ["Vehicle", "System", "Subsystem"]
    assert onto.check_self() == []


def test_the_ontology_checks_its_own_attachment_rules(onto):
    raw = copy.deepcopy(onto.data) if hasattr(onto, "data") else yaml.safe_load(settings.ontology_yaml.read_text())
    raw["document_types"]["ssdd"]["attaches_to"] = ["Vehicle", "Variant", "Planet"]
    msgs = [str(f) for f in Ontology(raw).check_self()]
    assert any("attaches_to unknown class 'Planet'" in m for m in msgs)
    assert any("attaches_to Variant, but HAS_ARTIFACT does not start from Variant" in m for m in msgs)


def test_the_staged_ssdd_states_what_it_is():
    assert read_header(STAGED.read_text()) == ENTRY
    assert yaml.safe_load(baseline_line(ENTRY))[0] == ENTRY
    # the owner column may also be headed Subsystem, Vehicle or Attached to
    for head in ("Subsystem", "Vehicle", "Attached to"):
        assert read_header(STAGED.read_text().replace("| Revision | System |", f"| Revision | {head} |")) == ENTRY


@pytest.mark.parametrize("owner,cls", [("V-001", "Vehicle"), ("SYS-VETR", "System"), ("SUB-BRK-ECU", "Subsystem")])
def test_ontology_check_accepts_an_ssdd_on_each_allowed_owner(onto, owner, cls):
    res = ontology_check(onto, settings.baseline_yaml, dict(ENTRY, attached_to=owner))
    assert res["ok"] and res["findings"] == [], res["findings"]
    assert res["subsystem"]["id"] == owner and res["subsystem"]["class"] == cls and res["subsystem"]["name"]
    assert res["may_attach_to"] == ["Vehicle", "System", "Subsystem"]
    assert res["document_type"]["name"] == "System/Subsystem Design Description"


def test_ontology_check_still_refuses_what_is_not_allowed(onto):
    msgs = lambda e: [f["message"] for f in ontology_check(onto, settings.baseline_yaml, e)["findings"]]   # noqa: E731
    # a variant or a family is not an owner the ontology allows; nor is an id that does not exist
    for owner in ("VAR-001", "FAM-001", "V-999", "FN-BRK-SLIP"):
        assert any(f"attached_to = {owner!r} is not a Vehicle, System or Subsystem in the baseline" in m
                   for m in msgs(dict(ENTRY, attached_to=owner))), owner
    # every other document type stays on a subsystem
    srs = {"id": "SRS-LTV-001", "name": "x", "type": "srs", "revision": "A", "attached_to": "V-001", "file": "SRS-LTV-001.md"}
    assert any("attached_to = 'V-001' is not a Subsystem in the baseline" in m for m in msgs(srs))
    assert any("required attribute 'attached_to' is missing" in m
               for m in msgs({k: v for k, v in ENTRY.items() if k != "attached_to"}))


def test_find_node():
    data = yaml.safe_load(settings.baseline_yaml.read_text())
    assert find_node(data, "V-001")[0] == "Vehicle" and find_node(data, "SYS-PWR")[0] == "System"
    assert find_node(data, "SUB-DCL")[0] == "Subsystem" and find_node(data, "FN-DCL-NVG")[0] == "Function"
    assert find_node(data, "SUB-NOPE") is None and find_node(data, None) is None


def _baseline_with(tmp_path, *entries):
    path = tmp_path / "baseline.yaml"
    path.write_text(settings.baseline_yaml.read_text().rstrip("\n") + "\n" + "".join(baseline_line(e) + "\n" for e in entries))
    return path


def test_the_baseline_loads_it_as_a_document_of_the_vehicle(tmp_path, onto):
    on_system = dict(ENTRY, id="SSDD-VETR-001", name="Vehicle Electronics design", attached_to="SYS-VETR", file="SSDD-VETR-001.md")
    path = _baseline_with(tmp_path, ENTRY, on_system)
    base = load_baseline(path)
    edges = {(e.from_eid, e.to_eid) for e in base.edges if e.type == "HAS_ARTIFACT"}
    assert ("V-001", "SSDD-LTV-001") in edges and ("SYS-VETR", "SSDD-VETR-001") in edges
    idx = base.artifact_index()
    assert idx["SSDD-LTV-001.md"]["wbs_code"] == "1.0" and idx["SSDD-LTV-001.md"]["owner_class"] == "Vehicle"
    assert idx["SSDD-LTV-001.md"]["subsystem_id"] == "V-001" and idx["SSDD-LTV-001.md"]["artifact_type"] == "ssdd"
    assert idx["SSDD-VETR-001.md"]["wbs_code"] == "1.1.1.6" and idx["SSDD-VETR-001.md"]["owner_class"] == "System"
    assert idx["SRS-BRK-001.md"]["wbs_code"] == "1.1.1.5" and idx["SRS-BRK-001.md"]["owner_class"] == "Subsystem"   # unchanged
    assert [f for f in check_baseline(onto, yaml.safe_load(path.read_text())) if f.level == "error"] == []


class Emb:
    model_id, dim = "fake", 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class Store:
    def __init__(self):
        self.rows = {}

    def upsert(self, chunks, vectors):
        self.rows.update({c.id: c for c in chunks})

    def delete_source(self, source):
        gone = [k for k, c in self.rows.items() if c.source_file == source]
        for k in gone:
            del self.rows[k]
        return len(gone)


class Graph:
    def snapshot(self):
        return {"vertices": [], "edges": []}


def test_ssdd_goes_through_receive_review_ingest_and_undo(tmp_path, monkeypatch, onto):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    s = replace(settings, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs, diagrams="local", vision_model="")
    monkeypatch.setattr(intake, "reload_graph", lambda graph, s: {})
    before = s.baseline_yaml.read_text()

    got = receive(STAGED, tmp_path / "incoming", s)
    assert got["entry"] == ENTRY
    assert ontology_check(onto, s.baseline_yaml, got["entry"])["ok"]
    review = review_document(STAGED.read_text(), got["entry"], Graph(), s)
    assert review["issues"] == [] and "4. Allocation of functions to subsystems" in review["sections"] and review["chunks"] > 3

    store = Store()
    manifest = ingest(tmp_path / got["document"], got["entry"], Emb(), store, None, tmp_path / "replaced", s)
    assert manifest["chunks"] == review["chunks"] == len(store.rows)
    assert "attached_to: V-001" in manifest["line"] and s.baseline_yaml.read_text() == before.rstrip("\n") + "\n" + manifest["line"] + "\n"
    chunk = next(iter(store.rows.values()))
    assert chunk.artifact_id == "SSDD-LTV-001" and chunk.metadata.get("wbs_code") == "1.0"
    base = load_baseline(s.baseline_yaml)
    assert any(e.type == "HAS_ARTIFACT" and (e.from_eid, e.to_eid) == ("V-001", "SSDD-LTV-001") for e in base.edges)
    assert [f for f in check_baseline(onto, yaml.safe_load(s.baseline_yaml.read_text()), docs) if f.level == "error"] == []

    undo(manifest, store, None, None, s)
    assert s.baseline_yaml.read_text() == before and not store.rows and not (docs / "SSDD-LTV-001.md").exists()


def test_an_ssdd_delivered_as_word_is_read_the_same_way(tmp_path):
    body = (wf.p("SSDD-LTV-001 — SYNTH-LTV Vehicle System/Subsystem Design Description", "Title")
            + wf.table([["Document", "Type", "Revision", "System"], ["SSDD-LTV-001", "SSDD", "A", "V-001"]])
            + wf.p("Scope", "Heading1") + wf.p("The system-wide design of the vehicle."))
    (tmp_path / "LTV system design rev A.docx").write_bytes(wf.make_docx(body))
    got = receive(tmp_path / "LTV system design rev A.docx", tmp_path / "incoming",
                  replace(settings, diagrams="local", vision_model=""))
    assert got["entry"] == ENTRY and got["conversion"]["from"] == "LTV system design rev A.docx"
