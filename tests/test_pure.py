"""Pure-Python tests: no database, no model."""

import pytest

from esamkit.baseline import load_baseline
from esamkit.chunking import chunk_markdown
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



# ── tables ──────────────────────────────────────────────────────────────────

def long_table(rows=12):
    head = "| Desk Dimension |  | Grade 4 |  |  | Grade 5 |  |  |\n|---|---|---|---|---|---|---|---|\n" \
           "| Desk Dimension |  | Female | Male | Total | Female | Male | Total |\n"
    body = "\n".join(f"| Dimension {i} | Mismatch | {i} | {i + 1} | {i + 2} | {i + 3} | {i + 4} | {i + 5} |" for i in range(rows))
    return head + body


def test_a_long_table_is_cut_between_rows_and_every_piece_keeps_caption_and_header():
    text = "# T\n\n## Results\n\nIntro text.\n\n**Table 3.3.** Percentages of mismatch.\n\n" + long_table()
    chunks = [c for c in chunk_markdown(text, size=400, overlap=60) if "|" in c]
    assert len(chunks) > 2
    for i, c in enumerate(chunks):
        body = c.split("\n", 1)[1]
        rows = [ln for ln in body.splitlines() if ln.startswith("|")]
        assert all(r.endswith("|") for r in rows), "a row was cut"
        assert "…|" not in body and "| …" not in body, "a half row was carried as overlap"
        assert "| Desk Dimension |  | Female | Male | Total |" in body, "sub-header missing"
        assert ("**Table 3.3.** Percentages of mismatch." + ("" if i == 0 else " (continued)")) in body
    every_row = [r for c in chunks for r in c.splitlines() if r.startswith("| Dimension ")]
    assert len(every_row) == 12 and len(set(every_row)) == 12      # each data row once, none lost


def test_a_caption_is_never_a_chunk_on_its_own():
    text = "# T\n\n## S\n\n" + "words " * 60 + "\n\n**Table 1.** Short.\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    chunks = chunk_markdown(text, size=400, overlap=60)
    assert not any(c.rstrip().endswith("**Table 1.** Short.") for c in chunks)
    assert any("**Table 1.** Short.\n\n| a | b |" in c for c in chunks)


def test_text_after_a_table_carries_no_table_row_as_overlap():
    text = "# T\n\n## S\n\n| a | b |\n|---|---|\n" + "\n".join(f"| row {i} | value {i} |" for i in range(20)) \
           + "\n\n" + "after " * 80
    after = [c for c in chunk_markdown(text, size=300, overlap=80) if "after" in c]
    assert after and not any("| row" in c for c in after)


def test_header_rows_stop_at_the_first_row_with_numbers():
    from esamkit.chunking import _split_table
    table = "| Item | Value |\n|---|---|\n|  | 5 |\n" + "\n".join(f"| x{i} | {i} |" for i in range(40))
    pieces = _split_table(table, 120, "")
    assert all(p.startswith("| Item | Value |\n|---|---|\n|") for p in pieces)
    assert sum(p.count("|  | 5 |") for p in pieces) == 1             # a data row is never repeated as header
