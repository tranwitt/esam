"""
Reload or reset the search store (the embedded chunks of the documents),
without the lessons folder and without an agent.

    python -m crew.main ingest status                 # what is stored, and is it in step with the baseline?
    python -m crew.main ingest reload                 # read, chunk, embed and store every document the baseline lists
    python -m crew.main ingest reload --only SRS-BRK-001 ICD-BRK-001      # just these documents
    python -m crew.main ingest reload --fresh         # empty the store first (needed after changing the embedding model)
    python -m crew.main ingest reload --with-graph    # ... and rebuild the graph afterwards (graph reload)
    python -m crew.main ingest reset --yes            # empty the store and leave it empty
    (each takes --env <profile>; `python -m crew.ingest_admin ...` is the same thing)

The search store is DERIVED, like the graph: every chunk comes from a
document the SE baseline lists (data/baseline/*.yaml, `artifacts:`), so it
can always be rebuilt. Reload it after editing a document by hand, after a
`git checkout` or a restored database, after changing the chunk size, and
(with --fresh) after changing the embedding model.

`reload` reads and chunks every document BEFORE it touches the store, and
asks the embedding model for one vector to see that it answers. Only then
are documents replaced, one at a time: a document's old chunks go and its new
ones arrive together, so the store stays searchable throughout, and a model
endpoint that fails half-way leaves every document either old or new, never
half. A full reload also removes chunks of documents the baseline no longer
lists. A listed document whose file is missing stops the reload (--force
skips it instead).

`reset` only empties the store. Questions and the pipelines find no text
until the next reload, so it asks for --yes.

Neither touches the baseline, the documents or the graph (except
--with-graph). The embedding model is called; no chat model and no CrewAI.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from esamkit.baseline import load_baseline  # noqa: E402
from esamkit.config import Settings, settings as default_settings  # noqa: E402
from esamkit.embedders.base import Embedder  # noqa: E402
from esamkit.ingest import make_chunks  # noqa: E402
from esamkit.readers import document_files, read_document  # noqa: E402
from esamkit.vectorstores.base import ModelMismatchError, VectorStore  # noqa: E402


class IngestAdminError(RuntimeError):
    """The message says why, and what state the store is in."""


def plan(s: Settings = default_settings, only: list[str] | None = None, force: bool = False) -> dict:
    """Read and chunk what a reload would store, touching nothing.

    {"documents": [{"id", "file", "wbs_code", "chunks": [Chunk]}], "listed": {file names the baseline lists},
     "skipped": [listed, file missing (only with force)], "unlisted": [files in the folder the baseline does not list]}"""
    try:
        index = load_baseline(s.baseline_yaml).artifact_index()
    except Exception as e:
        raise IngestAdminError(f"the baseline can't be read ({e}); the search store was not changed") from e
    by_id = {facts["artifact_id"]: rel for rel, facts in index.items()}
    wanted = list(by_id)
    if only:
        unknown = [x for x in only if x not in by_id]
        if unknown:
            raise IngestAdminError(f"not in the baseline: {', '.join(unknown)} (listed: {', '.join(sorted(by_id))}); "
                                   "the search store was not changed")
        wanted = list(dict.fromkeys(only))
    docs, skipped, problems = [], [], []
    for doc_id in wanted:
        rel = by_id[doc_id]
        path = s.documents_dir / rel
        if not path.is_file():
            (skipped if force else problems).append(f"{doc_id}: {rel} is listed in the baseline but the file is missing")
            continue
        try:
            chunks = make_chunks(read_document(path, s, diagrams=True), rel, index[rel], s)
        except Exception as e:
            (skipped if force else problems).append(f"{doc_id}: {rel} can't be read ({e})")
            continue
        docs.append({"id": doc_id, "file": rel, "wbs_code": index[rel].get("wbs_code", ""), "chunks": chunks})
    if problems:
        raise IngestAdminError("the search store was not changed (fix these, or reload with --force to skip them):\n  "
                               + "\n  ".join(problems))
    on_disk = {str(p.relative_to(s.documents_dir)) for p in document_files(s.documents_dir)}
    return {"documents": docs, "listed": set(index), "skipped": skipped, "unlisted": sorted(on_disk - set(index))}


def status(stored: dict[str, int], s: Settings = default_settings) -> dict:
    """Compare what is stored ({document id: chunks}) with what a reload would store.
    Counts only: a document edited without changing its number of chunks looks in step."""
    out = {"stored": sum(stored.values()), "documents": [], "problem": None}
    try:
        p = plan(s, force=True)
    except IngestAdminError as e:
        out.update(in_step=False, problem=str(e))
        return out
    expected = {d["id"]: len(d["chunks"]) for d in p["documents"]}
    for doc_id, n in expected.items():
        out["documents"].append({"id": doc_id, "expected": n, "stored": stored.get(doc_id, 0)})
    out["extra"] = {k: n for k, n in sorted(stored.items()) if k not in expected}
    out["skipped"], out["unlisted"] = p["skipped"], p["unlisted"]
    out["expected"] = sum(expected.values())
    out["in_step"] = not out["extra"] and not out["skipped"] and all(d["expected"] == d["stored"] for d in out["documents"])
    return out


def reload(store: VectorStore, emb: Embedder, s: Settings = default_settings, only: list[str] | None = None,
           force: bool = False, fresh: bool = False, planned: dict | None = None, say=lambda line: None) -> dict:
    """Store every document of the plan again. See the module text for the order of things."""
    if fresh and only:
        raise IngestAdminError("--fresh empties the whole store, so it cannot be combined with --only; "
                               "the search store was not changed")
    p = planned or plan(s, only, force)
    try:
        emb.embed_documents(["ESAM reload check"])               # does the model answer? asked before anything is removed
    except Exception as e:
        raise IngestAdminError(f"the embedding model does not answer ({e}); the search store was not changed") from e
    before = store.count()
    if fresh:
        store.reset()
    done, removed_stale = [], {}
    for d in p["documents"]:
        try:
            vectors = emb.embed_documents([c.text for c in d["chunks"]])
        except Exception as e:
            left = [x["id"] for x in p["documents"] if x["id"] not in {y["id"] for y in done}]
            raise IngestAdminError(
                f"the embedding model failed on {d['id']} ({e}). Reloaded: {', '.join(x['id'] for x in done) or 'none'}. "
                f"Not reloaded{' (the store was emptied first, so these are now absent)' if fresh else ' (still as they were)'}: "
                f"{', '.join(left)}. Run the reload again.") from e
        replaced = store.delete_source(d["file"])
        store.upsert(d["chunks"], vectors)
        done.append({"id": d["id"], "file": d["file"], "wbs_code": d["wbs_code"], "chunks": len(d["chunks"]), "replaced": replaced})
        say(f"  ✓ {d['id']:<16} {len(d['chunks']):>4} chunks  (replaced {replaced})")
    if not only and not fresh:                                    # a full reload: what the baseline no longer lists goes
        try:
            for source, n in sorted(store.sources().items()):
                if source not in p["listed"]:
                    removed_stale[source] = store.delete_source(source)
                    say(f"  - {source}: {n} chunks removed (no longer listed in the baseline)")
        except NotImplementedError:
            pass
    return {"before": before, "stored": store.count(), "documents": done, "removed_unlisted": removed_stale,
            "skipped": p["skipped"], "unlisted": p["unlisted"], "fresh": fresh}


def reset(store: VectorStore) -> dict:
    before = store.count()
    store.reset()
    return {"removed": before, "stored": store.count()}


# ── Command line ─────────────────────────────────────────────────────────────

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["status", "reload", "reset"])
    p.add_argument("--only", nargs="+", metavar="ID", help="reload: only these document ids")
    p.add_argument("--fresh", action="store_true",
                   help="reload: empty the store first (after changing the embedding model or its dimension)")
    p.add_argument("--with-graph", action="store_true", help="reload: rebuild the graph as well, afterwards")
    p.add_argument("--force", action="store_true", help="reload: skip a listed document whose file is missing or unreadable")
    p.add_argument("--yes", action="store_true", help="reset: really empty the store")


def _stored_counts(s: Settings) -> dict[str, int]:
    from esamkit.vectorstores.pgvector_store import chunk_counts
    return chunk_counts(s.pg_dsn, s.collection)


def _built_with(s: Settings):
    from esamkit.vectorstores.pgvector_store import collection_model
    return collection_model(s.pg_dsn, s.collection)


def _drop(s: Settings) -> int:
    from esamkit.vectorstores.pgvector_store import drop_collection
    return drop_collection(s.pg_dsn, s.collection)


def run(args, store: VectorStore | None = None, emb: Embedder | None = None, s: Settings | None = None,
        stored=None, graph=None) -> int:
    """`args` has action, only, fresh, with_graph, force, yes and env. Returns the exit code.
    `store`, `emb`, `stored` ({document id: chunks}) and `graph` are for tests; normally the profile decides."""
    if s is None:
        from esamkit.environment import load_profile, settings_for
        s = settings_for(load_profile(getattr(args, "env", None)))
    from esamkit.factory import embedder_identity, get_embedder, get_vector_store
    only, fresh = getattr(args, "only", None), getattr(args, "fresh", False)

    if args.action == "status":
        try:
            have = stored if stored is not None else _stored_counts(s)
            built = None if stored is not None else _built_with(s)
        except Exception as e:
            print(f"Can't open the search store: {e}")
            return 1
        st = status(have, s)
        print(f"Search store: {st['stored']} chunks in collection {s.collection!r}")
        current = embedder_identity(s)
        if built:
            print(f"Built with  : {built[0]} ({built[1]} numbers per chunk)")
            if built[0] != current:
                print(f"NOT USABLE with the configured embedding model ({current}): vectors from different models cannot be "
                      "compared.\nFix: python -m crew.main ingest reload --fresh")
                return 1
        if st["problem"]:
            print(f"NOT CHECKED: {st['problem']}")
            return 1
        for d in st["documents"]:
            mark = "✓" if d["stored"] == d["expected"] else "!"
            print(f"  {mark} {d['id']:<16} {d['stored']:>4} stored, {d['expected']:>4} expected")
        for doc_id, n in st["extra"].items():
            print(f"  ! {doc_id or '(no document id)'}: {n} chunks stored for a document the baseline does not list")
        for line in st["skipped"]:
            print(f"  ! {line}")
        for name in st["unlisted"]:
            print(f"  · {name} is in the documents folder but not in the baseline: not stored")
        if st["in_step"]:
            print("In step (by number of chunks): every document the baseline lists is stored, and nothing else.\n"
                  "A document edited without changing its number of chunks is not noticed here; reload after editing.")
            return 0
        print("OUT OF STEP with the baseline and its documents.\nFix: python -m crew.main ingest reload")
        return 1

    if args.action == "reset":
        try:
            if store is None and not args.yes:
                n = sum(_stored_counts(s).values())
            else:
                n = store.count() if store is not None else None
            if not args.yes:
                print(f"The search store holds {n} chunks.\n"
                      "reset empties it and leaves it empty: questions and the pipelines find no document text until the "
                      "next reload.\nTo do that, add --yes. To rebuild it from the documents: ingest reload")
                return 2
            n = reset(store)["removed"] if store is not None else _drop(s)
        except Exception as e:
            print(f"Can't open the search store: {e}")
            return 1
        print(f"Search store reset: removed {n} chunks. It is now empty.\n"
              "Fill it again with: python -m crew.main ingest reload")
        return 0

    # reload
    try:
        planned = plan(s, only, args.force)                       # everything is read and chunked first
        if fresh and only:
            raise IngestAdminError("--fresh empties the whole store, so it cannot be combined with --only; "
                                   "the search store was not changed")
    except IngestAdminError as e:
        print(f"NOT RELOADED: {e}")
        return 1
    try:
        emb = emb or get_embedder(s)
        print(f"Embedder    : {emb.describe()}")
        if store is None:
            try:
                store = get_vector_store(emb, s)
            except ModelMismatchError as e:
                if not fresh:
                    print(f"NOT RELOADED: {e}\nThe store was built with another embedding model. To rebuild it with the "
                          "configured one: python -m crew.main ingest reload --fresh")
                    return 1
                emb.embed_documents(["ESAM reload check"])        # the new model answers, so the old store may go
                _drop(s)
                store = get_vector_store(emb, s)
    except IngestAdminError as e:
        print(f"NOT RELOADED: {e}")
        return 1
    except Exception as e:
        print(f"NOT RELOADED: can't open the embedding model or the search store ({e}); the search store was not changed")
        return 1
    total = sum(len(d["chunks"]) for d in planned["documents"])
    print(f"Search store: collection {s.collection!r}, {store.count()} chunks"
          f"{' (emptied first: --fresh)' if fresh else ''}\n"
          f"Documents   : {len(planned['documents'])} to store, {total} chunks\n")
    try:
        res = reload(store, emb, s, only, args.force, fresh, planned, say=print)
    except IngestAdminError as e:
        print(f"\nRELOAD STOPPED: {e}")
        return 1
    for line in res["skipped"]:
        print(f"  ! skipped (--force): {line}")
    for name in res["unlisted"]:
        print(f"  · {name}: not listed in the baseline, not stored (add it under `artifacts:`, or use the intake pipeline)")
    print(f"\nStored {sum(d['chunks'] for d in res['documents'])} chunks from {len(res['documents'])} document(s). "
          f"The search store held {res['before']} chunks and now holds {res['stored']}.")
    code = 1 if res["skipped"] else 0
    if getattr(args, "with_graph", False):
        from crew import graph_admin
        print("\nGraph:")
        code = max(code, graph_admin.run(argparse.Namespace(action="reload", yes=False, force=False, env=getattr(args, "env", None)),
                                         graph=graph, s=s))
    else:
        print("The graph was not touched (python -m crew.main graph status | reload).")
    return code


def main() -> None:
    ap = argparse.ArgumentParser(description="Reload or reset the ESAM search store (the embedded document chunks)")
    add_arguments(ap)
    ap.add_argument("--env", default=None, help="environment profile (default: ESAM_ENV or local)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
