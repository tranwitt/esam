"""Word documents and pictures through Pipeline A's front door (esamkit.intake):
`receive` turns what arrived into the markdown that is reviewed, and `ingest`
/ `undo` keep the delivered file beside it. No database: the stores are fakes."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import wordfiles as wf
from esamkit import intake
from esamkit.config import PROJECT_ROOT, settings
from esamkit.intake import ORIGINALS, conversion_attention, ingest, receive, undo

WORD = Path(__file__).parent / "fixtures" / "word"
STAGED = PROJECT_ROOT / "lessons" / "06_missing_document" / "staged" / "STD-BRK-001.md"
LOCAL = replace(settings, diagrams="local", vision_model="", doc_converter="builtin")
ENTRY = {"id": "STD-BRK-001", "name": "Brake Control ECU Software Test Description", "type": "std",
         "revision": "A", "attached_to": "SUB-BRK-ECU", "file": "STD-BRK-001.md"}


class Emb:
    model_id, dim = "fake", 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class Store:
    def __init__(self):
        self.rows = {}

    def upsert(self, chunks, vectors):
        assert len(chunks) == len(vectors)
        self.rows.update({c.id: c for c in chunks})

    def delete_source(self, source):
        gone = [k for k, c in self.rows.items() if c.source_file == source]
        for k in gone:
            del self.rows[k]
        return len(gone)

    def text(self):
        return "\n".join(c.text for c in self.rows.values())


@pytest.fixture
def bench(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    shutil.copytree(settings.documents_dir, docs)
    shutil.copy2(settings.baseline_yaml, tmp_path / "baseline.yaml")
    monkeypatch.setattr(intake, "reload_graph", lambda graph, s: {"reloaded": True})
    return replace(LOCAL, baseline_yaml=tmp_path / "baseline.yaml", documents_dir=docs), Store(), tmp_path


@pytest.mark.parametrize("name", ["STD-BRK-001.docx", "STD-BRK-001.doc"])
def test_a_word_file_is_received_as_markdown_with_its_original_kept(tmp_path, name):
    got = receive(WORD / name, tmp_path / "incoming", LOCAL)
    assert got["entry"] == ENTRY and got["document"] == "incoming/STD-BRK-001.md"
    assert got["original"] == f"incoming/original/{name}"
    assert (tmp_path / "incoming" / "original" / name).read_bytes() == (WORD / name).read_bytes()
    md = (tmp_path / "incoming" / "STD-BRK-001.md").read_text()
    assert md.startswith("# STD-BRK-001 — Brake Control ECU Software Test Description\n")
    for case in ("TC-BRK-010", "TC-BRK-011", "TC-BRK-021", "TC-BRK-030"):
        assert case in md
    conv = got["conversion"]
    assert conv["from"] == name and conv["converter"] and conv["pictures"] == []
    assert conversion_attention(conv)[0].startswith(f"Converted from {name} (")


def test_markdown_is_received_unchanged(tmp_path):
    got = receive(STAGED, tmp_path / "incoming", LOCAL)
    assert got["entry"] == ENTRY and got["original"] is None and got["conversion"] is None
    assert (tmp_path / "incoming" / "STD-BRK-001.md").read_bytes() == STAGED.read_bytes()
    assert not (tmp_path / "incoming" / "original").exists() and conversion_attention(None) == []


def test_a_document_that_does_not_say_what_it_is_is_not_staged(tmp_path):
    (tmp_path / "notes.docx").write_bytes(wf.make_docx(wf.p("Meeting notes", "Title") + wf.p("Nothing formal.")))
    got = receive(tmp_path / "notes.docx", tmp_path / "incoming", LOCAL)
    assert "id" not in got["entry"] and got["document"] is None and not (tmp_path / "incoming").exists()


def test_a_picture_needs_the_se_to_say_what_it_is(tmp_path):
    svg = tmp_path / "brake bus.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>BRAKE_ECU</text><text>EBC1 100 ms</text></svg>')
    assert receive(svg, tmp_path / "incoming", LOCAL)["document"] is None
    got = receive(svg, tmp_path / "incoming", LOCAL,
                  {"id": "ICD-BRK-009", "name": "Brake bus diagram", "type": "icd", "revision": "A", "attached_to": "SUB-BRK-ECU"})
    assert got["entry"]["file"] == "ICD-BRK-009.md" and got["entry"]["type"] == "icd"
    md = (tmp_path / "incoming" / "ICD-BRK-009.md").read_text()
    assert md.startswith("# ICD-BRK-009 — Brake bus diagram\n") and "Text in the picture: BRAKE_ECU; EBC1 100 ms." in md
    assert got["original"] == "incoming/original/brake bus.svg" and got["conversion"]["pictures"][0]["read_by"] == ["SVG text"]


def test_attention_names_model_described_and_undescribed_pictures():
    notes = conversion_attention({"from": "a.docx", "converter": "built-in .docx reader", "notes": ["1 text box flattened"],
                                  "pictures": [{"n": 1, "name": "i1.png", "read_by": ["vision model gpt-5.1 (cached)"]},
                                               {"n": 2, "name": "i2.emf", "read_by": [], "note": "format not readable"},
                                               {"n": 3, "name": "d", "read_by": ["shape text"]}]})
    assert len(notes) == 4 and notes[1] == "Conversion note: 1 text box flattened"
    assert notes[2].startswith("Picture 1 (i1.png): described by a model")
    assert notes[3].startswith("Picture 2 (i2.emf): NOT described (format not readable)")


def test_ingest_keeps_the_original_and_undo_takes_it_away(bench):
    s, store, tmp = bench
    baseline_before = s.baseline_yaml.read_text()
    got = receive(WORD / "STD-BRK-001.doc", tmp / "incoming", s)
    manifest = ingest(tmp / got["document"], got["entry"], Emb(), store, None, tmp / "replaced", s,
                      original=tmp / got["original"])
    kept = s.documents_dir / ORIGINALS / "STD-BRK-001.doc"
    assert manifest["original"] == str(kept) and kept.read_bytes() == (WORD / "STD-BRK-001.doc").read_bytes()
    assert (s.documents_dir / "STD-BRK-001.md").exists() and manifest["chunks"] == len(store.rows) > 0
    assert "TC-BRK-011" in store.text() and manifest["previous_originals"] == []
    # the originals folder is not itself a document to index
    from esamkit.readers import document_files
    assert kept not in document_files(s.documents_dir)

    out = undo(manifest, store, None, None, s)
    assert out["chunks_removed"] == manifest["chunks"] and not store.rows
    assert not kept.exists() and not (s.documents_dir / "STD-BRK-001.md").exists()
    assert s.baseline_yaml.read_text() == baseline_before


def test_a_new_revision_replaces_the_original_and_undo_brings_the_old_one_back(bench):
    s, store, tmp = bench
    first = receive(WORD / "STD-BRK-001.docx", tmp / "in1", s)
    ingest(tmp / first["document"], first["entry"], Emb(), store, None, tmp / "replaced1", s, original=tmp / first["original"])
    old = s.documents_dir / ORIGINALS / "STD-BRK-001.docx"
    assert old.exists()

    second = receive(WORD / "STD-BRK-001.doc", tmp / "in2", s, {"revision": "B"})
    manifest = ingest(tmp / second["document"], second["entry"], Emb(), store, None, tmp / "replaced2", s,
                      original=tmp / second["original"])
    assert not old.exists() and (s.documents_dir / ORIGINALS / "STD-BRK-001.doc").exists()
    assert manifest["previous_originals"] == [str(tmp / "replaced2" / ORIGINALS / "STD-BRK-001.docx")]
    assert manifest["previous_file"] and "revision: B" in manifest["line"] and "revision: A" in manifest["previous_line"]

    out = undo(manifest, store, None, Emb(), s)
    assert out["previous_revision_restored"] and old.exists()
    assert not (s.documents_dir / ORIGINALS / "STD-BRK-001.doc").exists()
    assert "revision: A" in s.baseline_yaml.read_text() and "revision: B" not in s.baseline_yaml.read_text()
    assert store.rows
