"""Pure-Python tests: no database, no model."""

import pytest

from esamkit.baseline import load_baseline
from esamkit.chunking import chunk_markdown, has_own_title
from esamkit.config import settings


# ── baseline ────────────────────────────────────────────────────────────────

def test_baseline_counts(baseline):
    labels = [v.label for v in baseline.vertices]
    assert len(baseline.vertices) == 22
    assert labels.count("Subsystem") == 4
    assert labels.count("DesignArtifact") == 7      # 5 on subsystems, 2 system-level Word documents
    types = [e.type for e in baseline.edges]
    assert (types.count("PART_OF"), types.count("HAS_ARTIFACT"), types.count("INTERFACES_WITH")) == (14, 7, 3)


def test_wbs_codes_are_real_881f_appendix_g(baseline):
    # G.3 table: 1.1.1.5 Suspension/Steering, .6 Vehicle Electronics, .7 Power Package/Drive Train
    systems = {v.eid: v.props["wbs_code"] for v in baseline.vertices if v.label == "System"}
    assert systems == {"SYS-SUSP": "1.1.1.5", "SYS-VETR": "1.1.1.6", "SYS-PWR": "1.1.1.7"}


def test_subsystems_have_no_invented_wbs_code(baseline):
    assert all("wbs_code" not in v.props for v in baseline.vertices if v.label == "Subsystem")


def test_artifact_index_links_documents_to_wbs(baseline):
    idx = baseline.artifact_index()
    assert idx["ICD-BRK-001.md"]["wbs_code"] == "1.1.1.5"
    assert idx["SRS-DCL-001.md"]["subsystem_id"] == "SUB-DCL"
    assert idx["SRS-DCL-001.md"]["wbs_code"] == "1.1.1.6"
    # every listed document exists on disk
    for f in idx:
        assert (settings.documents_dir / f).exists(), f


def test_dangling_edge_is_rejected(tmp_path):
    bad = settings.baseline_yaml.read_text().replace("to: SUB-DCL", "to: SUB-TYPO")
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="SUB-TYPO"):
        load_baseline(p)


# ── chunking ────────────────────────────────────────────────────────────────

def test_chunks_carry_document_and_section_label():
    text = (settings.documents_dir / "SDD-BRK-001.md").read_text()
    chunks = chunk_markdown(text, 600, 80)
    task_set = next(c for c in chunks if "Task set" in c.splitlines()[0])
    assert task_set.startswith("[SDD-BRK-001 — Brake Control ECU Software Design › 2. Task set]")


def test_chunks_respect_size_and_never_split_words():
    words = " ".join(f"word{i}" for i in range(400))       # one 3 KB paragraph, no sentences
    chunks = chunk_markdown("# T\n\n## S\n\n" + words, size=300, overlap=40)
    assert len(chunks) > 5
    vocab = set(words.split())
    for c in chunks:
        body = c.split("\n", 1)[1]                          # drop the "[T › S]" label
        assert len(body) <= 300 + 40 + 5                    # size + overlap carry + ellipsis
        for w in body.replace("…", " ").split():
            assert w in vocab, f"split word: {w!r}"


def test_overlap_does_not_cross_sections():
    text = "# T\n\n## A\n\n" + "alpha " * 120 + "\n\n## B\n\n" + "beta " * 20
    chunks = chunk_markdown(text, size=300, overlap=60)
    b = next(c for c in chunks if "› B]" in c)
    assert "alpha" not in b



# A paper exported from Word with no Title style: the title is bold text, the
# sections are Heading 1 ('#'), and the subsections Heading 3.
PAPER = """**High-Performance Microprocessor Design for SHA-3 via FPGA**

Alexander Nickita

# Abstract

Modern computing systems demand both high performance and strong security.

# 2. Main Theory

### 2.1 Microprocessor Design

The Alpha microprocessor family was optimised for high clock frequency.

# 3. Results

Throughput reached 22.94 Gbps."""


def labels(chunks):
    return [c.splitlines()[0] for c in chunks]


def test_a_document_without_its_own_title_is_labelled_with_the_supplied_one():
    assert not has_own_title(PAPER)
    assert labels(chunk_markdown(PAPER, title="SHA-3 paper")) == [
        "[SHA-3 paper]",                                     # the title page: was unlabelled
        "[SHA-3 paper › Abstract]",                          # was "[Abstract]"
        "[SHA-3 paper › 2. Main Theory › 2.1 Microprocessor Design]",
        "[SHA-3 paper › 3. Results]",                        # a later '#' is a section, not a new title
    ]


def test_without_a_supplied_title_nothing_changes():
    assert labels(chunk_markdown(PAPER)) == [
        "**High-Performance Microprocessor Design for SHA-3 via FPGA**",
        "[Abstract]", "[2. Main Theory › 2.1 Microprocessor Design]", "[3. Results]"]


def test_a_document_that_names_itself_ignores_the_supplied_title():
    for name in ("SDD-BRK-001.md", "SRS-BRK-001.md", "ICD-BRK-001.md"):
        text = (settings.documents_dir / name).read_text()
        assert has_own_title(text)
        assert chunk_markdown(text, title="something else") == chunk_markdown(text)


def test_text_with_no_headings_at_all_is_labelled_with_the_title():
    chunks = chunk_markdown("plain words " * 80, size=300, overlap=40, title="Notes")
    assert len(chunks) > 1 and all(c.startswith("[Notes]\n") for c in chunks)


def test_make_chunks_uses_the_baseline_title_then_the_file_name():
    from esamkit.ingest import make_chunks
    first = lambda facts, rel: make_chunks(PAPER, rel, facts)[1].text.splitlines()[0]
    assert first({"artifact_id": "P-1", "title": "SHA-3 on FPGA"}, "papers/p1.docx") == "[SHA-3 on FPGA › Abstract]"
    assert first({"artifact_id": "P-1"}, "papers/sha3_fpga.docx") == "[sha3_fpga › Abstract]"