"""Several documents of one type on one owner: a document delivered in parts
(each part its own id, with `part: "n of N"`), or simply two of a kind."""

import copy
import shutil
from dataclasses import replace

import pytest
import yaml

from esamkit import intake
from esamkit.baseline import load_baseline
from esamkit.config import settings
from esamkit.intake import baseline_line, ingest, ontology_check, read_header, receive, undo
from esamkit.ontology import check_baseline, default_ontology, part_sets


def ssdd(n, total=3, owner="V-001", **kw):
    return {"id": f"SSDD-LTV-00{n}", "name": f"SYNTH-LTV System Design, Part {n}: {'Overview Architecture Interfaces'.split()[n - 1]}",
            "type": "ssdd", "revision": "A", "attached_to": owner, "part": f"{n} of {total}", "file": f"SSDD-LTV-00{n}.md", **kw}


def doc_text(e):
    cols = "| Document | Type | Revision | System | Part |\n|---|---|---|---|---|\n"
    return (f"# {e['id']} — {e['name']}\n\n{cols}| {e['id']} | SSDD | {e['revision']} | {e['attached_to']} | {e['part']} |\n\n"
            f"## 1. Scope\n\nThis is part {e['part']} of the system design of the SYNTH-LTV vehicle.\n\n"
            f"## 2. Content\n\nDesign decisions recorded in part {e['part']}.\n")


@pytest.fixture
def onto():
    return default_ontology()


@pytest.fixture
def data():
    return yaml.safe_load(settings.baseline_yaml.read_text())


def findings(onto, data, *entries):
    d = copy.deepcopy(data)
    d["artifacts"] = d["artifacts"] + list(entries)
    return [f"{f.level}: {f.where}: {f.message}" for f in check_baseline(onto, d)]


def test_header_states_the_part_and_the_baseline_line_keeps_it():
    e = ssdd(2)
    assert read_header(doc_text(e)) == e
    assert yaml.safe_load(baseline_line(e))[0] == e                       # "2 of 3" survives as text
    assert read_header(doc_text(e).replace("| 2 of 3 |", "| 2/3 |"))["part"] == "2 of 3"
    assert read_header(doc_text(e).replace("| Part |", "| Volume |").replace("| 2 of 3 |", "| 2 OF 3 |"))["part"] == "2 of 3"
    assert "part" not in read_header("# SRS-BRK-001 — x\n\n| Document | Type | Revision | Subsystem |\n|---|---|---|---|\n| SRS-BRK-001 | SRS | A | SUB-BRK-ECU |\n")


def test_a_complete_set_passes_and_an_incomplete_one_is_a_warning_not_an_error(onto, data):
    assert findings(onto, data, ssdd(1), ssdd(2), ssdd(3)) == []
    got = findings(onto, data, ssdd(1), ssdd(3))
    assert got == ["warning: SSDD for V-001: part 2 of 3 is not in the baseline (have: SSDD-LTV-001, SSDD-LTV-003)"]
    only_first = findings(onto, data, ssdd(1))
    assert len(only_first) == 1 and "parts 2, 3 of 3 are not in the baseline" in only_first[0]


def test_what_is_wrong_with_a_set_is_reported(onto, data):
    twice = findings(onto, data, ssdd(1), ssdd(2), dict(ssdd(3), part="2 of 3"))
    assert any("part 2 is claimed by more than one document: SSDD-LTV-002, SSDD-LTV-003" in f and f.startswith("error") for f in twice)
    assert any("part 3 of 3 is not in the baseline" in f for f in twice)
    disagree = findings(onto, data, ssdd(1, total=2), ssdd(2, total=3))
    assert any("disagree on how many parts there are (2, 3)" in f for f in disagree)
    assert any("part = '4 of 3': the part number is larger than the number of parts" in f for f in findings(onto, data, dict(ssdd(1), part="4 of 3")))
    assert any("part = 'two': doesn't match the required form" in f for f in findings(onto, data, dict(ssdd(1), part="two")))


def test_sets_are_per_type_and_owner_and_documents_without_a_part_are_just_documents(onto, data):
    # two ICDs on one subsystem, no parts: nothing to report
    icd2 = {"id": "ICD-BRK-002", "name": "Brake ECU diagnostic interface", "type": "icd", "revision": "A",
            "attached_to": "SUB-BRK-ECU", "file": "ICD-BRK-002.md"}
    assert findings(onto, data, icd2) == []
    # a 2-part SSDD on the vehicle and a 1-part-of-2 on a system are different sets
    sys_part = dict(ssdd(3, total=2, owner="SYS-VETR"), part="1 of 2")
    got = findings(onto, data, ssdd(1, total=2), ssdd(2, total=2), sys_part)
    assert len(got) == 1 and "SSDD for SYS-VETR: part 2 of 2 is not in the baseline" in got[0]
    sets = part_sets([ssdd(1, total=2), ssdd(2, total=2), sys_part, icd2])
    assert [(s["type"], s["attached_to"], s["total"], s["missing"]) for s in sets] == [
        ("ssdd", "SYS-VETR", 2, [2]), ("ssdd", "V-001", 2, [])]


def test_the_graph_gets_every_part(tmp_path):
    path = tmp_path / "baseline.yaml"
    path.write_text(settings.baseline_yaml.read_text().rstrip("\n") + "\n" + "".join(baseline_line(ssdd(n)) + "\n" for n in (1, 2, 3)))
    base = load_baseline(path)
    owned = sorted(e.to_eid for e in base.edges if e.type == "HAS_ARTIFACT" and e.from_eid == "V-001")
    assert owned == ["SSDD-LTV-001", "SSDD-LTV-002", "SSDD-LTV-003"]
    assert base.vertex("SSDD-LTV-002").props["part"] == "2 of 3" and "part" not in base.vertex("SRS-BRK-001").props
    idx = base.artifact_index()
    assert idx["SSDD-LTV-003.md"]["part"] == "3 of 3" and "part" not in idx["SRS-BRK-001.md"]


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


def test_parts_are_ingested_one_by_one_and_none_replaces_another(tmp_path, monkeypatch, onto):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    s = replace(settings, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs, diagrams="local", vision_model="")
    monkeypatch.setattr(intake, "reload_graph", lambda graph, s: {})
    store, manifests = Store(), []

    for n, missing_after, others in ((1, [2, 3], []), (3, [2], ["SSDD-LTV-001"]), (2, [], ["SSDD-LTV-001", "SSDD-LTV-003"])):
        (tmp_path / f"part{n}.md").write_text(doc_text(ssdd(n)))
        got = receive(tmp_path / f"part{n}.md", tmp_path / f"in{n}", s)
        assert got["entry"] == ssdd(n)
        oc = ontology_check(onto, s.baseline_yaml, got["entry"])
        assert oc["ok"] and oc["replaces_revision"] is None
        assert [d["id"] for d in oc["same_type_documents"]] == others
        assert oc["part"]["this"] == f"{n} of 3" and oc["part"]["total"] == 3 and oc["part"]["still_missing"] == missing_after
        assert bool([f for f in oc["findings"] if "not in the baseline" in f["message"]]) == bool(missing_after)
        m = ingest(tmp_path / got["document"], got["entry"], Emb(), store, None, tmp_path / f"replaced{n}", s)
        assert m["previous_file"] is None and m["previous_line"] is None and m["chunks_replaced"] == 0
        manifests.append(m)

    arts = {a["id"]: a for a in yaml.safe_load(s.baseline_yaml.read_text())["artifacts"]}
    assert [arts[f"SSDD-LTV-00{n}"]["part"] for n in (1, 2, 3)] == ["1 of 3", "2 of 3", "3 of 3"]
    assert {c.artifact_id for c in store.rows.values()} == {"SSDD-LTV-001", "SSDD-LTV-002", "SSDD-LTV-003"}
    assert all(c.metadata["part"].endswith("of 3") for c in store.rows.values())
    assert [f for f in check_baseline(onto, yaml.safe_load(s.baseline_yaml.read_text()), docs)] == []

    # a new revision of ONE part replaces that part only
    rev_b = ssdd(2, revision="B")
    (tmp_path / "part2b.md").write_text(doc_text(rev_b))
    got = receive(tmp_path / "part2b.md", tmp_path / "in2b", s)
    oc = ontology_check(onto, s.baseline_yaml, got["entry"])
    assert oc["ok"] and oc["replaces_revision"] == "A" and oc["part"]["still_missing"] == []
    m = ingest(tmp_path / got["document"], got["entry"], Emb(), store, None, tmp_path / "replaced2b", s)
    assert m["previous_line"] and len(yaml.safe_load(s.baseline_yaml.read_text())["artifacts"]) == 5 + 3

    # undoing one part leaves the others, and the set is reported incomplete again
    undo(manifests[1], store, None, None, s)                     # part 3
    left = {a["id"] for a in yaml.safe_load(s.baseline_yaml.read_text())["artifacts"]}
    assert "SSDD-LTV-003" not in left and {"SSDD-LTV-001", "SSDD-LTV-002"} <= left
    assert any("part 3 of 3 is not in the baseline" in str(f) for f in check_baseline(onto, yaml.safe_load(s.baseline_yaml.read_text())))


def test_a_second_srs_for_a_subsystem_adds_its_requirements(tmp_path):
    # requirements split over two SRS parts land on the same subsystem
    from esamkit.traceability import extract
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    (docs / "SRS-BRK-002.md").write_text("# SRS-BRK-002 — Brake Control ECU Software Requirements, Part 2\n\n## 3. Diagnostics\n\n"
                                         "- **SRS-BRK-050** The software shall report a valve fault within 50 ms.\n")
    path = tmp_path / "baseline.yaml"
    text = settings.baseline_yaml.read_text()
    first = next(ln for ln in text.splitlines() if "id: SRS-BRK-001" in ln)
    text = text.replace(first, first.replace("file: SRS-BRK-001.md", 'part: "1 of 2", file: SRS-BRK-001.md'))
    second = {"id": "SRS-BRK-002", "name": "Brake Control ECU Software Requirements, Part 2", "type": "srs", "revision": "A",
              "attached_to": "SUB-BRK-ECU", "part": "2 of 2", "file": "SRS-BRK-002.md"}
    path.write_text(text.rstrip("\n") + "\n" + baseline_line(second) + "\n")
    assert [str(f) for f in check_baseline(default_ontology(), yaml.safe_load(path.read_text()), docs)] == []
    ex = extract(load_baseline(path), docs)
    by_doc = {}
    for v in ex.vertices:
        if v.label == "Requirement":
            by_doc.setdefault(v.props["source_artifact"], []).append(v)
    assert set(by_doc) >= {"SRS-BRK-001", "SRS-BRK-002"} and [v.eid for v in by_doc["SRS-BRK-002"]] == ["SRS-BRK-050"]
    assert by_doc["SRS-BRK-002"][0].props["subsystem"] == "SUB-BRK-ECU"
