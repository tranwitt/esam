"""
Query / passage prefixes per model family.

These come from each model's own model card: they are part of how the model
was trained, not decoration.

  BGE v1.5 : instruction on the QUERY only, nothing on passages.
  E5       : "query: " on queries AND "passage: " on passages.

Mixing them up (e.g. E5-style prefixes on a BGE model, as the earlier
embeddings.py did) still "works" (you get results) but quietly lowers
retrieval quality. Lesson 02 measures the difference.

The local embedder and the endpoint embedder share this table, so the same
model gives the same vectors wherever it runs.
"""

QUERY_PREFIX = {
    "bge": "Represent this sentence for searching relevant passages: ",
    "e5": "query: ",
}
DOC_PREFIX = {
    "bge": "",
    "e5": "passage: ",
}


def family(model_id: str) -> str:
    m = model_id.lower()
    if "bge" in m:
        return "bge"
    if "e5" in m:
        return "e5"
    return "plain"  # e.g. all-MiniLM-L6-v2, text-embedding-3-*: symmetric, no prefixes


def _choose(table: dict, model_id: str, setting: str) -> str:
    if setting.strip().lower() == "auto":
        return table.get(family(model_id), "")
    return "" if setting.strip().lower() in ("", "none") else setting


def query_prefix(model_id: str, setting: str = "auto") -> str:
    return _choose(QUERY_PREFIX, model_id, setting)


def doc_prefix(model_id: str, setting: str = "auto") -> str:
    return _choose(DOC_PREFIX, model_id, setting)
