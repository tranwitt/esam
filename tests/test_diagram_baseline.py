"""Systems and subsystems read from a picture diagram into the vehicle
baseline (esamkit/diagram_baseline.py). The vision model is a fake endpoint
that answers with the JSON a model would give."""

import json
import shutil
from dataclasses import replace

import pytest
import yaml

import wordfiles as wf
from conftest import chat_answer
from esamkit import diagram_baseline as db, intake, llm
from esamkit.baseline import load_baseline
from esamkit.config import settings
from esamkit.docmodel import Picture
from esamkit.intake import ingest, receive, undo
from esamkit.ontology import check_baseline, default_ontology

SHOWN = {"is_structure_diagram": True, "systems": [
    {"name": "Vehicle Electronics", "existing_id": "SYS-VETR", "wbs_code": None, "subsystems": [
        {"name": "Driver Digital Cluster", "existing_id": "SUB-DCL"},
        {"name": "Tire Pressure Monitor", "existing_id": None, "aliases": ["TPMS"], "safety_critical": None,
         "evidence": "box right of the cluster"},
        {"name": "Body Control Module (BCM)", "safety_critical": False}]},
    {"name": "Fire Suppression", "existing_id": None, "wbs_code": "1.1.1.16",
     "subsystems": [{"name": "Fire Control Unit", "safety_critical": True}]},
    {"name": "Auxiliary Automotive", "subsystems": [{"name": "Winch Controller"}, {"name": "ABS ECU"}]},
    {"name": "Power Package/Drive Train", "subsystems": [{"name": "brake controller"}]}],
    "unplaced_subsystems": [{"name": "Radio"}]}
ADDED = ["SUB-TPM", "SUB-BCM", "SYS-FS", "SUB-FC", "SYS-AA", "SUB-WC"]
ENTRY = {"id": "SSDD-LTV-002", "name": "Vehicle architecture diagram", "type": "ssdd", "revision": "A", "attached_to": "V-001"}


@pytest.fixture(autouse=True)
def fresh():
    llm._CHAINS.clear()


@pytest.fixture
def onto():
    return default_ontology()


@pytest.fixture
def bench(tmp_path, endpoint, monkeypatch):
    """Settings on a copy of the baseline and documents, with a vision model that answers SHOWN."""
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("```json\n" + json.dumps(SHOWN) + "\n```"))
    monkeypatch.setattr(intake, "reload_graph", lambda graph, s: {})
    return replace(settings, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs, diagrams="auto",
                   diagram_baseline="off", vision_model="see-1", vision_base_url=endpoint.url, vision_api_key="k",
                   vision_api="chat", vision_fallbacks=(), chat_fallbacks=(), diagram_cache_dir=tmp_path / "cache",
                   llm_timeout=5)


def data_of(s):
    return yaml.safe_load(s.baseline_yaml.read_text())


def picture(tmp_path, name="architecture.png"):
    (tmp_path / name).write_bytes(wf.png())
    return tmp_path / name


# ── analyse ──────────────────────────────────────────────────────────────────

def test_the_model_is_shown_the_picture_and_what_the_baseline_already_has(bench, endpoint):
    struct = db.existing_structure(data_of(bench))
    assert struct["variant"] == "VAR-001" and set(struct["systems"]) == {"SYS-SUSP", "SYS-VETR", "SYS-PWR"}
    got = db.analyse(Picture("a.png", wf.png(), "image/png"), struct, bench)
    assert got["model"] == "see-1" and got["cached"] is False and len(got["systems"]) == 4         # fenced JSON is read
    content = endpoint.calls[0][1]["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "SYS-VETR: Vehicle Electronics" in content[0]["text"] and "SUB-BRK-ECU: Brake Control ECU (ABS) (also called: brake ECU" in content[0]["text"]
    again = db.analyse(Picture("a.png", wf.png(), "image/png"), struct, bench)
    assert again["cached"] is True and len(endpoint.calls) == 1                                   # same picture, same baseline


def test_no_picture_is_sent_unless_a_vision_model_may_be_used(bench, endpoint):
    struct, pic = db.existing_structure(data_of(bench)), Picture("a.png", wf.png(), "image/png")
    assert "no vision model is configured" in db.analyse(pic, struct, replace(bench, vision_model=""))["skipped"]
    assert "pictures are not sent to a model" in db.analyse(pic, struct, replace(bench, diagrams="local"))["skipped"]
    assert endpoint.calls == []
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("I see a nice diagram."))
    assert "no usable answer" in db.analyse(pic, struct, bench)["skipped"]                         # not JSON: nothing proposed
    endpoint.handlers["/chat/completions"] = lambda b: (500, {"error": "down"})
    assert "no usable answer" in db.analyse(Picture("b.png", wf.png(5, 5), "image/png"), struct, bench)["skipped"]
    assert db.mode_of(bench) == "off" and db.mode_of(bench, "Apply") == "apply"
    with pytest.raises(ValueError):
        db.mode_of(bench, "always")


def test_an_svg_and_a_word_drawing_go_as_text(bench, endpoint):
    struct = db.existing_structure(data_of(bench))
    db.analyse(Picture("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>Fire Control Unit</text></svg>'), struct, bench)
    assert isinstance(endpoint.calls[-1][1]["messages"][1]["content"], str) and "<text>Fire Control Unit</text>" in endpoint.calls[-1][1]["messages"][1]["content"]
    db.analyse(Picture("drawing1", labels=["Fire Suppression", "Fire Control Unit"]), struct, bench)
    assert "- Fire Control Unit" in endpoint.calls[-1][1]["messages"][1]["content"]
    assert "no picture data and no text" in db.analyse(Picture("empty"), struct, bench)["skipped"]


# ── propose ──────────────────────────────────────────────────────────────────

def proposal(s, shown=SHOWN, document="SSDD-LTV-002"):
    return db.propose([{"n": 1, "name": "architecture.png", "model": "see-1", **shown}], data_of(s), document=document)


def test_only_what_the_baseline_lacks_is_proposed(bench):
    p = proposal(bench)
    adds = {a["id"]: a for a in p["additions"]}
    assert list(adds) == ADDED
    assert adds["SUB-TPM"] == {"class": "Subsystem", "id": "SUB-TPM", "name": "Tire Pressure Monitor", "parent": "SYS-VETR",
                               "from": "picture 1 (architecture.png)", "aliases": ["TPMS"], "evidence": "box right of the cluster",
                               "needs": ["safety_critical (written as true until an SE decides)"]}
    assert adds["SUB-BCM"]["safety_critical"] is False and adds["SUB-BCM"]["needs"] == []           # the acronym in the name
    assert adds["SYS-FS"]["wbs_code"] == "1.1.1.16" and adds["SYS-FS"]["parent"] == "VAR-001" and adds["SUB-FC"]["parent"] == "SYS-FS"
    assert adds["SYS-AA"]["needs"] == ["wbs_code (the picture shows none)"] and "wbs_code" not in adds["SYS-AA"]
    # matched to what exists by id, by name and by alias: left alone
    assert {x["id"]: x["seen_as"] for x in p["already_present"]} == {
        "SYS-VETR": "Vehicle Electronics", "SUB-DCL": "Driver Digital Cluster", "SUB-BRK-ECU": "ABS ECU",
        "SYS-PWR": "Power Package/Drive Train"}
    why = {x["name"]: x["why"] for x in p["not_added"]}
    assert "not moved; an SE decides" in why["brake controller"] and "does not place it in a system" in why["Radio"]
    text = "\n".join(db.summary(p))
    assert "2 system(s) and 4 subsystem(s) the baseline does not have" in text and '+ System SYS-AA "Auxiliary Automotive" under VAR-001' in text


def test_ids_are_unique_and_in_the_ontologys_form(bench, onto):
    shown = {"systems": [{"name": "Vehicle Electronics", "subsystems": [
        {"name": "Display Control Logic (DCL)"}, {"name": "Data Concentrator, Left"}, {"name": "Data Concentrator, Right"},
        {"name": "Tire Pressure Monitor"}, {"name": "tire pressure monitor"}, {"name": ""}, {"name": "Übersicht №1"}]}],
        "unplaced_subsystems": []}
    ids = [a["id"] for a in proposal(bench, shown)["additions"]]
    assert ids == ["SUB-DCL2", "SUB-DCL3", "SUB-DCR", "SUB-TPM", "SUB-B1"]            # SUB-DCL exists; the same name once; no empty name
    import re
    assert all(re.match(onto.classes["Subsystem"]["id_pattern"], i) for i in ids)
    # a used or malformed WBS code is not taken from the picture
    for wbs in ("1.1.1.6", "six", "1"):
        add = proposal(bench, {"systems": [{"name": "Armament", "wbs_code": wbs, "subsystems": []}]})["additions"][0]
        assert "wbs_code" not in add and add["needs"]


def test_pictures_that_show_no_structure_propose_nothing(bench):
    p = db.propose([{"n": 1, "name": "photo.png", "is_structure_diagram": False, "systems": [], "unplaced_subsystems": []},
                    {"n": 2, "name": "x.emf", "skipped": "a image/x-emf cannot be sent to the model from this machine"}], data_of(bench))
    assert p["additions"] == [] and [x["analysed"] for x in p["pictures"]] == [True, False]
    assert db.summary(p) == ["Picture 1 (photo.png): not a diagram of systems and subsystems; nothing proposed.",
                             "Picture 2 (x.emf): not analysed for structure: a image/x-emf cannot be sent to the model from this machine."]
    assert db.summary(None) == []


# ── apply and revert ─────────────────────────────────────────────────────────

def test_additions_are_written_as_candidates_where_they_belong(bench, onto):
    before = bench.baseline_yaml.read_text()
    record = db.apply(proposal(bench), bench.baseline_yaml, onto)
    assert record["applied"] == ADDED
    after = bench.baseline_yaml.read_text()
    import difflib
    ops = {op for op, *_ in difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False).get_opcodes()}
    assert ops == {"equal", "insert"}                                          # lines were only added: nothing moved or changed
    data = data_of(bench)
    systems = {s["id"]: s for s in data["vehicle"]["families"][0]["variants"][0]["systems"]}
    assert list(systems) == ["SYS-SUSP", "SYS-VETR", "SYS-PWR", "SYS-FS", "SYS-AA"]
    assert [x["id"] for x in systems["SYS-VETR"]["subsystems"]] == ["SUB-J1939-BB", "SUB-DCL", "SUB-TPM", "SUB-BCM"]
    tpm = systems["SYS-VETR"]["subsystems"][2]
    assert tpm == {"id": "SUB-TPM", "name": "Tire Pressure Monitor", "safety_critical": True, "aliases": ["TPMS"],
                   "status": "candidate", "derived_from": "SSDD-LTV-002 picture 1 (architecture.png)"}
    assert systems["SYS-VETR"]["subsystems"][3]["safety_critical"] is False
    assert systems["SYS-FS"]["wbs_code"] == "1.1.1.16" and "wbs_code" not in systems["SYS-AA"]
    assert "# wbs_code: the diagram shows none" in after and "safety_critical: true       # assumed" in after
    # still a baseline: it loads, and the only finding is the WBS code an SE has to give
    found = [(f.level, f.where, f.message) for f in check_baseline(onto, data)]
    assert found == [("warning", "SYS-AA", "candidate: required attribute 'wbs_code' is still to be given by an SE")]
    base = load_baseline(bench.baseline_yaml)
    assert base.vertex("SYS-AA").props["status"] == "candidate" and base.vertex("SYS-AA").props["wbs_code"] == ""
    assert base.vertex("SUB-WC").props["derived_from"].startswith("SSDD-LTV-002 picture 1")
    assert any(e.type == "PART_OF" and (e.from_eid, e.to_eid) == ("SUB-WC", "SYS-AA") for e in base.edges)

    assert db.revert(record, bench.baseline_yaml, onto) == {"reverted": ADDED, "kept": []}
    assert bench.baseline_yaml.read_text() == before


def test_a_baseline_that_cannot_take_the_additions_is_left_exactly_as_it_was(bench, onto):
    before = bench.baseline_yaml.read_text()
    p = proposal(bench)
    gone = dict(p, additions=[dict(p["additions"][0], parent="SYS-NOPE")])
    with pytest.raises(db.DiagramBaselineError, match="could not find the entry '- id: SYS-NOPE'"):
        db.apply(gone, bench.baseline_yaml, onto)
    bad = dict(p, additions=[dict(p["additions"][0], id="sub tpm")])                  # would fail the ontology check
    with pytest.raises(db.DiagramBaselineError, match="fail the ontology check"):
        db.apply(bad, bench.baseline_yaml, onto)
    db.apply(p, bench.baseline_yaml, onto)
    with pytest.raises(db.DiagramBaselineError, match="is already in the baseline: make the proposal again"):
        db.apply(p, bench.baseline_yaml, onto)
    assert db.apply(dict(p, additions=[]), bench.baseline_yaml, onto)["applied"] == []
    bench.baseline_yaml.write_text(before)
    assert db.revert(None, bench.baseline_yaml, onto) == {"reverted": [], "kept": []}


def test_a_candidate_an_se_has_touched_or_that_is_in_use_is_not_taken_back(bench, onto):
    record = db.apply(proposal(bench), bench.baseline_yaml, onto)
    # a document is now attached to one of the candidates
    icd = {"id": "ICD-TPM-001", "name": "TPM interface", "type": "icd", "revision": "A", "attached_to": "SUB-TPM", "file": "ICD-TPM-001.md"}
    with_doc = bench.baseline_yaml.read_text().rstrip("\n") + "\n" + intake.baseline_line(icd) + "\n"
    bench.baseline_yaml.write_text(with_doc)
    out = db.revert(record, bench.baseline_yaml, onto)
    assert out["reverted"] == [] and out["kept"] == ADDED and "depend on them" in out["why"]
    assert bench.baseline_yaml.read_text() == with_doc
    # an SE confirmed one (deleted its status line)
    edited = with_doc.replace("                  status: candidate          # read from a diagram: check it, then delete this line\n", "", 1)
    bench.baseline_yaml.write_text(edited)
    out = db.revert(record, bench.baseline_yaml, onto)
    assert out["kept"] == ADDED and "edited since" in out["why"] and bench.baseline_yaml.read_text() == edited


# ── through the intake ───────────────────────────────────────────────────────

def test_off_by_default_nothing_is_analysed(bench, endpoint, tmp_path):
    got = receive(picture(tmp_path), tmp_path / "incoming", bench, ENTRY)
    assert got["diagram_baseline"] == "off" and got["baseline_proposal"] is None
    assert len(endpoint.calls) == 1 and not (tmp_path / "incoming" / "baseline_proposal.json").exists()    # the description only


def test_propose_lists_the_additions_and_changes_nothing(bench, endpoint, tmp_path):
    before = bench.baseline_yaml.read_text()
    got = receive(picture(tmp_path), tmp_path / "incoming", bench, ENTRY, diagram_baseline="propose")
    assert got["diagram_baseline"] == "propose" and [a["id"] for a in got["baseline_proposal"]["additions"]] == ADDED
    assert got["baseline_proposal"]["document"] == "SSDD-LTV-002" and len(endpoint.calls) == 2      # describe + analyse
    saved = json.loads((tmp_path / "incoming" / "baseline_proposal.json").read_text())
    assert saved == got["baseline_proposal"] and bench.baseline_yaml.read_text() == before
    # the setting does the same as the argument
    got2 = receive(picture(tmp_path), tmp_path / "incoming2", replace(bench, diagram_baseline="propose"), ENTRY)
    assert [a["id"] for a in got2["baseline_proposal"]["additions"]] == ADDED and len(endpoint.calls) == 2   # both answers cached


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


def test_apply_adds_them_on_ingest_and_undo_takes_them_out(bench, tmp_path, onto):
    before = bench.baseline_yaml.read_text()
    got = receive(picture(tmp_path), tmp_path / "incoming", bench, ENTRY, diagram_baseline="apply")
    assert bench.baseline_yaml.read_text() == before                                  # not yet: only on ingest
    store = Store()
    manifest = ingest(tmp_path / got["document"], got["entry"], Emb(), store, None, tmp_path / "replaced", bench,
                      original=tmp_path / got["original"], baseline_proposal=got["baseline_proposal"])
    assert manifest["baseline_additions"]["applied"] == ADDED and store.rows
    base = load_baseline(bench.baseline_yaml)
    assert base.vertex("SUB-FC").props["status"] == "candidate"
    assert any(e.type == "HAS_ARTIFACT" and e.to_eid == "SSDD-LTV-002" for e in base.edges)
    assert [f for f in check_baseline(onto, data_of(bench), bench.documents_dir) if f.level == "error"] == []

    out = undo(manifest, store, None, None, bench)
    assert out["baseline_candidates_removed"] == ADDED and out["baseline_candidates_kept"] == []
    assert bench.baseline_yaml.read_text() == before and not store.rows


def test_an_ingest_goes_ahead_when_the_additions_cannot_be_written(bench, tmp_path):
    got = receive(picture(tmp_path), tmp_path / "incoming", bench, ENTRY, diagram_baseline="apply")
    stale = dict(got["baseline_proposal"], additions=[dict(got["baseline_proposal"]["additions"][0], parent="SYS-GONE")])
    manifest = ingest(tmp_path / got["document"], got["entry"], Emb(), Store(), None, tmp_path / "replaced", bench,
                      baseline_proposal=stale)
    assert manifest["baseline_additions"]["applied"] == [] and "SYS-GONE" in manifest["baseline_additions"]["error"]
    assert "SSDD-LTV-002" in bench.baseline_yaml.read_text() and "SUB-TPM" not in bench.baseline_yaml.read_text()


def test_a_word_document_with_a_diagram_in_it(bench, tmp_path):
    body = (wf.p("SSDD-LTV-002 — Vehicle architecture", "Title")
            + wf.table([["Document", "Type", "Revision", "System"], ["SSDD-LTV-002", "SSDD", "A", "V-001"]])
            + wf.p("Architecture", "Heading1") + wf.p("", extra=wf.picture("rId1")) + wf.p("Figure 1 — Systems", "Caption"))
    (tmp_path / "arch.docx").write_bytes(wf.make_docx(body, {"image1.png": wf.png()}))
    got = receive(tmp_path / "arch.docx", tmp_path / "incoming", bench, diagram_baseline="propose")
    assert got["entry"]["id"] == "SSDD-LTV-002" and got["baseline_proposal"]["additions"][0]["from"] == "picture 1 (image1.png)"
    plain = receive(settings.documents_dir / "SDD-DCL-001.md", tmp_path / "incoming3", bench, diagram_baseline="propose")
    assert plain["baseline_proposal"] is None                                         # no pictures: nothing to analyse


def test_candidates_await_an_se_in_the_health_report(bench, onto):
    from esamkit.health import health_report
    from test_requirement_levels import Graph
    db.apply(proposal(bench), bench.baseline_yaml, onto)
    g, base = Graph(), load_baseline(bench.baseline_yaml)
    g.load(base)
    data = data_of(bench)
    rep = health_report(g, onto, data, bench.documents_dir, {a["id"]: 1 for a in data["artifacts"]})
    waiting = {f["where"]: f for f in next(c for c in rep["checks"] if c["id"] == 3)["findings"]}
    assert set(ADDED) <= set(waiting)
    assert waiting["SUB-TPM"]["message"].startswith("subsystem 'Tire Pressure Monitor' is a candidate, read from SSDD-LTV-002 picture 1")
    assert waiting["SUB-TPM"]["critical"] and not waiting["SUB-BCM"]["critical"]       # assumed safety-critical until an SE decides
    assert not rep["blocked"]
    warnings = next(c for c in rep["checks"] if c["id"] == 5)["warnings"]
    assert any(w["where"] == "SYS-AA" and "wbs_code" in w["message"] for w in warnings)


def test_profiles_and_the_environment_file_carry_the_option(monkeypatch):
    from esamkit import environment as env
    monkeypatch.delenv("ESAM_DIAGRAM_BASELINE", raising=False)
    for name in env.profile_names():
        assert env.settings_for(env.load_profile(name)).diagram_baseline == "off"
    monkeypatch.setenv("ESAM_DIAGRAM_BASELINE", "propose")
    assert env.settings_for(env.load_profile("local")).diagram_baseline == "propose"
