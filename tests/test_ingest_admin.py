"""The crew's search-store utility (crew/ingest_admin.py): status, reload, reset.
The store and the embedding model here are in-memory fakes; the real pgvector
store needs PostgreSQL."""

import argparse
import shutil
from dataclasses import replace

import pytest
import yaml

from crew import ingest_admin as ia
from esamkit.config import settings
from esamkit.vectorstores.base import VectorStore
from test_graph_admin import FakeGraph

IDS = ["ICD-BRK-001", "SDD-BRK-001", "SDD-DCL-001", "SRS-BRK-001", "SRS-DCL-001", "SSDD-PWR-001", "SSDD-SUSP-001"]


class FakeStore(VectorStore):
    def __init__(self):
        self.rows, self.log = {}, []

    def upsert(self, chunks, vectors):
        assert len(chunks) == len(vectors)
        for c, v in zip(chunks, vectors):
            self.rows[c.id] = (c, v)
        self.log.append(("upsert", chunks[0].source_file if chunks else ""))

    def delete_source(self, source_file):
        gone = [k for k, (c, _) in self.rows.items() if c.source_file == source_file]
        for k in gone:
            del self.rows[k]
        self.log.append(("delete", source_file))
        return len(gone)

    def search(self, query_vector, k=5, where=None):
        return []

    def count(self):
        return len(self.rows)

    def reset(self):
        self.rows = {}
        self.log.append(("reset", ""))

    def sources(self):
        out = {}
        for c, _ in self.rows.values():
            out[c.source_file] = out.get(c.source_file, 0) + 1
        return out

    def by_document(self):
        out = {}
        for c, _ in self.rows.values():
            out[c.artifact_id] = out.get(c.artifact_id, 0) + 1
        return out


class FakeEmbedder:
    model_id, dim = "fake-embed", 3

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def embed_documents(self, texts):
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("HTTP 503 from the embeddings endpoint")
        self.calls.append(len(texts))
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    def embed_query(self, text):
        return [0.0, 0.0, 1.0]

    def describe(self):
        return "FakeEmbedder(fake-embed, 3)"


@pytest.fixture
def s(tmp_path):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    return replace(settings, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs, diagrams="off", vision_model="")


def args(action, **kw):
    return argparse.Namespace(action=action, only=kw.get("only"), fresh=kw.get("fresh", False), force=kw.get("force", False),
                              with_graph=kw.get("with_graph", False), yes=kw.get("yes", False), env=None)


def test_reload_stores_every_document_the_baseline_lists(s):
    store, emb = FakeStore(), FakeEmbedder()
    res = ia.reload(store, emb, s)
    assert sorted(d["id"] for d in res["documents"]) == IDS
    assert res["before"] == 0 and res["stored"] == store.count() == sum(d["chunks"] for d in res["documents"]) > 30
    chunk = store.rows["SSDD-SUSP-001::0000"][0]
    assert (chunk.source_file, chunk.artifact_id, chunk.metadata["wbs_code"]) == ("SSDD-SUSP-001.md", "SSDD-SUSP-001", "1.1.1.5")
    assert emb.calls[0] == 1                                       # the model is asked for one vector before anything changes
    assert ia.status(store.by_document(), s)["in_step"]

    # again: the same chunks replace themselves, nothing piles up
    again = ia.reload(store, emb, s)
    assert again["before"] == again["stored"] == res["stored"]
    assert all(d["replaced"] == d["chunks"] for d in again["documents"])


def test_an_edited_document_is_replaced_and_may_have_fewer_chunks(s):
    store, emb = FakeStore(), FakeEmbedder()
    ia.reload(store, emb, s)
    before = store.by_document()
    path = s.documents_dir / "ICD-BRK-001.md"
    text = path.read_text()
    path.write_text(text[: text.index("\n## ", text.index("\n## ") + 1)] + "\n\nThe ESAM reload test added this sentence.\n")
    assert not ia.status(before, s)["in_step"]
    res = ia.reload(store, emb, s, only=["ICD-BRK-001"])
    assert [d["id"] for d in res["documents"]] == ["ICD-BRK-001"] and res["documents"][0]["replaced"] == before["ICD-BRK-001"]
    after = store.by_document()
    assert after["ICD-BRK-001"] < before["ICD-BRK-001"]
    assert {k: v for k, v in after.items() if k != "ICD-BRK-001"} == {k: v for k, v in before.items() if k != "ICD-BRK-001"}
    assert any("reload test added this sentence" in c.text for c, _ in store.rows.values())
    assert [x for x in store.log if x[0] == "delete"][-1] == ("delete", "ICD-BRK-001.md")


def test_a_full_reload_removes_what_the_baseline_no_longer_lists(s):
    store, emb = FakeStore(), FakeEmbedder()
    ia.reload(store, emb, s)
    data = yaml.safe_load(s.baseline_yaml.read_text())
    lines = [ln for ln in s.baseline_yaml.read_text().splitlines() if "id: SSDD-PWR-001" not in ln]
    s.baseline_yaml.write_text("\n".join(lines) + "\n")
    assert len(yaml.safe_load(s.baseline_yaml.read_text())["artifacts"]) == len(data["artifacts"]) - 1
    st = ia.status(store.by_document(), s)
    assert not st["in_step"] and list(st["extra"]) == ["SSDD-PWR-001"] and st["unlisted"] == ["SSDD-PWR-001.md"]

    part = ia.reload(store, emb, s, only=["SRS-BRK-001"])           # a reload of one document removes nothing else
    assert part["removed_unlisted"] == {} and "SSDD-PWR-001" in store.by_document()
    res = ia.reload(store, emb, s)
    assert list(res["removed_unlisted"]) == ["SSDD-PWR-001.md"] and res["unlisted"] == ["SSDD-PWR-001.md"]
    assert "SSDD-PWR-001" not in store.by_document() and ia.status(store.by_document(), s)["in_step"]


def test_nothing_is_touched_when_a_document_is_missing_or_the_model_does_not_answer(s):
    store, emb = FakeStore(), FakeEmbedder()
    ia.reload(store, emb, s)
    kept, store.log = dict(store.rows), []

    (s.documents_dir / "SDD-DCL-001.md").unlink()
    with pytest.raises(ia.IngestAdminError, match="SDD-DCL-001: SDD-DCL-001.md is listed in the baseline but the file is missing"):
        ia.reload(store, emb, s)
    with pytest.raises(ia.IngestAdminError, match="not in the baseline: SRS-NOPE-001"):
        ia.reload(store, emb, s, only=["SRS-NOPE-001"])
    with pytest.raises(ia.IngestAdminError, match="cannot be combined with --only"):
        ia.reload(store, emb, s, only=["SRS-BRK-001"], fresh=True)

    class Down(FakeEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("401 Unauthorized")
    with pytest.raises(ia.IngestAdminError, match="does not answer .401 Unauthorized.; the search store was not changed"):
        ia.reload(store, Down(), s, force=True, fresh=True)
    assert store.rows == kept and store.log == []

    # --force skips the missing one, says so, and leaves its old chunks alone
    res = ia.reload(store, emb, s, force=True)
    assert len(res["skipped"]) == 1 and "SDD-DCL-001" in res["skipped"][0] and len(res["documents"]) == 6
    assert store.by_document()["SDD-DCL-001"] > 0


def test_a_model_that_fails_half_way_leaves_each_document_old_or_new(s):
    store = FakeStore()
    ia.reload(store, FakeEmbedder(), s)
    kept = store.by_document()
    with pytest.raises(ia.IngestAdminError) as err:
        ia.reload(store, FakeEmbedder(fail_on="Driver Digital Cluster Software Requirements"), s)
    msg = str(err.value)
    done, left = msg.split("Not reloaded")
    assert "failed on SRS-DCL-001" in done and all(x in done for x in ("ICD-BRK-001", "SDD-BRK-001", "SDD-DCL-001", "SRS-BRK-001"))
    assert "still as they were" in left and "SRS-DCL-001, SSDD-SUSP-001, SSDD-PWR-001" in left
    assert store.by_document() == kept                             # every document is whole


def test_fresh_empties_the_store_first(s):
    store, emb = FakeStore(), FakeEmbedder()
    ia.reload(store, emb, s)
    store.rows["OLD::0000"] = (next(iter(store.rows.values()))[0], [9.0])
    store.log = []
    res = ia.reload(store, emb, s, fresh=True)
    assert store.log[0] == ("reset", "") and "OLD::0000" not in store.rows and res["fresh"]
    assert all(d["replaced"] == 0 for d in res["documents"])


def test_command_line(s, capsys):
    store, emb = FakeStore(), FakeEmbedder()
    assert ia.run(args("status"), s=s, stored={}) == 1
    out = capsys.readouterr().out
    assert "0 chunks in collection" in out and "! SRS-BRK-001" in out and "Fix: python -m crew.main ingest reload" in out

    assert ia.run(args("reload"), store=store, emb=emb, s=s) == 0
    out = capsys.readouterr().out
    assert "Documents   : 7 to store" in out and "✓ SSDD-PWR-001" in out and "The graph was not touched" in out
    assert f"now holds {store.count()}" in out

    assert ia.run(args("status"), s=s, stored=store.by_document()) == 0
    assert "In step (by number of chunks)" in capsys.readouterr().out

    assert ia.run(args("reload", only=["NOPE"]), store=store, emb=emb, s=s) == 1
    assert "NOT RELOADED: not in the baseline: NOPE" in capsys.readouterr().out
    assert ia.run(args("reload", only=["SRS-BRK-001"], fresh=True), store=store, emb=emb, s=s) == 1
    assert "cannot be combined with --only" in capsys.readouterr().out

    assert ia.run(args("reset"), store=store, s=s) == 2 and store.count() > 0      # asks for --yes
    assert "add --yes" in capsys.readouterr().out
    assert ia.run(args("reset", yes=True), store=store, s=s) == 0 and store.count() == 0
    assert "It is now empty" in capsys.readouterr().out


def test_reload_with_graph_rebuilds_the_graph_too(s, capsys):
    store, graph = FakeStore(), FakeGraph()
    assert ia.run(args("reload", with_graph=True), store=store, emb=FakeEmbedder(), s=s, graph=graph) == 0
    out = capsys.readouterr().out
    assert "Graph reloaded from the baseline." in out and graph.resets == 1 and len(graph.v) == 44


def test_the_crew_command_line_offers_it():
    text = (ia.ROOT / "crew" / "main.py").read_text()
    assert 'add("ingest", cmd_ingest' in text and "ingest_admin.add_arguments(i)" in text
