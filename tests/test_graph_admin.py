"""The crew's graph utility (crew/graph_admin.py): status, reload, reset.
The graph here is an in-memory fake with the store's upsert semantics."""

import argparse
import shutil
from dataclasses import replace

import pytest

from crew import graph_admin as ga
from esamkit.config import PROJECT_ROOT, settings
from esamkit.intake import baseline_line

STD = PROJECT_ROOT / "lessons" / "06_missing_document" / "staged" / "STD-BRK-001.md"


class FakeGraph:
    def __init__(self):
        self.v, self.e, self.resets = {}, {}, 0

    def reset(self):
        self.resets += 1
        self.v, self.e = {}, {}

    def load(self, baseline):
        for v in baseline.vertices:
            self.v[v.eid] = {"eid": v.eid, "label": v.label, "props": dict(v.props)}
        for e in baseline.edges:
            assert e.from_eid in self.v and e.to_eid in self.v, e
            self.e[(e.from_eid, e.type, e.to_eid)] = {"from_eid": e.from_eid, "to_eid": e.to_eid, "type": e.type, "props": dict(e.props)}
        return {"vertices": len(self.v), "edges": len(self.e)}

    def snapshot(self):
        return {"vertices": list(self.v.values()), "edges": list(self.e.values())}

    def remove_derived(self, source):
        gone = [k for k, v in self.v.items() if v["label"] in ("Requirement", "TestCase") and v["props"].get("source_artifact") == source]
        for k in gone:
            del self.v[k]
        self.e = {k: e for k, e in self.e.items() if k[0] in self.v and k[2] in self.v}
        return len(gone)

    def existing_ids(self, label):
        return {k for k, v in self.v.items() if v["label"] == label}


@pytest.fixture
def s(tmp_path):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    return replace(settings, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs)


def args(action, **kw):
    return argparse.Namespace(action=action, yes=kw.get("yes", False), force=kw.get("force", False), env=None)


def test_reload_builds_the_graph_from_the_baseline_and_its_documents(s):
    g = FakeGraph()
    assert ga.status(g, s)["in_step"] is False and len(ga.status(g, s)["missing"]) > 20
    res = ga.reload(g, s)
    assert res["before"]["vertices"] == 0 and res["in_step"] and res["ontology_errors"] == []
    assert res["graph"]["by_label"]["Subsystem"] == 4 and res["graph"]["by_label"]["DesignArtifact"] == 7
    assert res["graph"]["by_label"]["Requirement"] == res["traceability"]["requirements"] > 0
    assert res["graph"]["by_type"]["HAS_ARTIFACT"] == 7 and ga.status(g, s)["in_step"]
    again = ga.reload(g, s)                                         # repeatable: same graph, nothing doubled
    assert again["graph"] == res["graph"] and again["before"] == res["graph"]


def test_status_names_what_is_out_of_step_and_reload_fixes_it(s):
    g = FakeGraph()
    ga.reload(g, s)
    # the SE edits the baseline by hand: a test description is added, and its file put in place
    shutil.copy2(STD, s.documents_dir / "STD-BRK-001.md")
    entry = {"id": "STD-BRK-001", "name": "Brake Control ECU Software Test Description", "type": "std",
             "revision": "A", "attached_to": "SUB-BRK-ECU", "file": "STD-BRK-001.md"}
    s.baseline_yaml.write_text(s.baseline_yaml.read_text().rstrip("\n") + "\n" + baseline_line(entry) + "\n")
    st = ga.status(g, s)
    assert not st["in_step"] and "STD-BRK-001" in st["missing"] and "TC-BRK-011" in st["missing"] and st["extra"] == []
    assert st["missing_links"] > 0
    res = ga.reload(g, s)
    assert res["in_step"] and res["traceability"]["test_cases"] == 4 and res["graph"]["by_label"]["TestCase"] == 4
    # something in the graph that the baseline no longer has is reported, then gone after a reload
    g.v["SUB-GHOST"] = {"eid": "SUB-GHOST", "label": "Subsystem", "props": {"id": "SUB-GHOST"}}
    assert ga.status(g, s)["extra"] == ["SUB-GHOST"]
    assert ga.reload(g, s)["in_step"] and "SUB-GHOST" not in g.v


def test_a_bad_baseline_is_refused_before_the_graph_is_touched(s):
    g = FakeGraph()
    ga.reload(g, s)
    good, resets = ga.counts(g), g.resets
    text = s.baseline_yaml.read_text()

    s.baseline_yaml.write_text(text.replace("attached_to: SUB-DCL, ", "attached_to: SUB-NOPE, ", 1))   # dangling reference
    with pytest.raises(ga.GraphAdminError, match="can't be read"):
        ga.reload(g, s)
    with pytest.raises(ga.GraphAdminError):                        # --force does not load what cannot be loaded
        ga.reload(g, s, force=True)
    assert ga.status(g, s)["problem"] and ga.status(g, s)["in_step"] is False

    s.baseline_yaml.write_text(text.replace("type: srs,  revision: A", "type: srs,  revision: a1", 1))  # loads, fails the ontology
    with pytest.raises(ga.GraphAdminError, match="fails the ontology check"):
        ga.reload(g, s)
    assert ga.counts(g) == good and g.resets == resets              # untouched through all of it
    forced = ga.reload(g, s, force=True)
    assert forced["ontology_errors"] and forced["graph"]["vertices"] == good["vertices"]


def test_reset_empties_the_graph_only_when_told_to(s, capsys):
    g = FakeGraph()
    ga.reload(g, s)
    assert ga.run(args("reset"), g, s) == 2 and g.v                 # no --yes: nothing happens
    assert "add --yes" in capsys.readouterr().out
    assert ga.run(args("reset", yes=True), g, s) == 0 and not g.v and not g.e
    assert "It is now empty" in capsys.readouterr().out
    assert ga.run(args("status"), g, s) == 1
    assert "OUT OF STEP" in capsys.readouterr().out
    assert ga.run(args("reload"), g, s) == 0
    out = capsys.readouterr().out
    assert "Before : 0 nodes, 0 links (empty)" in out and "Graph reloaded from the baseline." in out
    assert ga.run(args("status"), g, s) == 0 and "In step" in capsys.readouterr().out
    # neither command touches the baseline or the documents
    assert s.baseline_yaml.read_text() == settings.baseline_yaml.read_text()


def test_the_command_line_reports_a_refused_reload(s, capsys):
    g = FakeGraph()
    s.baseline_yaml.write_text(s.baseline_yaml.read_text().replace("type: srs,  revision: A", "type: srs,  revision: a1", 1))
    assert ga.run(args("reload"), g, s) == 1 and g.resets == 0
    assert "NOT RELOADED" in capsys.readouterr().out
    assert ga.run(args("reload", force=True), g, s) == 0
    assert "Loaded with --force despite 1 ontology error(s)" in capsys.readouterr().out
