"""
Reload or reset the graph, without the lessons folder and without an agent.

    python -m crew.main graph status              # what is in the graph, and is it in step with the baseline?
    python -m crew.main graph reload              # empty it, then load the baseline and the documents' traceability
    python -m crew.main graph reset --yes         # empty it and leave it empty
    (each takes --env <profile>; `python -m crew.graph_admin ...` is the same thing)

The graph is DERIVED: the structure comes from the SE baseline
(data/baseline/*.yaml) and the requirements and test cases from the SRS and
STD documents it lists. Nothing an SE wrote lives only in the graph, so it
can always be rebuilt, and `reload` is the fix whenever the graph and the
baseline disagree (health check 5: "reload the graph"): after editing the
baseline by hand, after a `git checkout`, after restoring a database.

`reload` reads and checks everything BEFORE it touches the database: the
baseline must load, pass the ontology check (--force to load it anyway) and
its documents must be readable. Only then is the graph emptied and loaded,
so a typo in the baseline cannot leave an empty graph behind.

`reset` only empties the graph. The Cockpit, `ask` and the pipelines have
nothing to work with until the next `reload`, so it asks for --yes.

Neither touches the baseline, the documents or the search store (the
embedded chunks). No model is called and CrewAI is not needed.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from esamkit.baseline import Baseline, load_baseline  # noqa: E402
from esamkit.config import Settings, settings as default_settings  # noqa: E402
from esamkit.graphstores.base import GraphStore  # noqa: E402
from esamkit.ontology import check_baseline, load_ontology  # noqa: E402
from esamkit.traceability import extract, load_traceability  # noqa: E402


class GraphAdminError(RuntimeError):
    """The graph was NOT changed; the message says why."""


def counts(graph: GraphStore) -> dict:
    """{"vertices": n, "edges": n, "by_label": {...}, "by_type": {...}}"""
    snap = graph.snapshot()
    return {"vertices": len(snap["vertices"]), "edges": len(snap["edges"]),
            "by_label": dict(Counter(v["label"] for v in snap["vertices"])),
            "by_type": dict(Counter(e["type"] for e in snap["edges"]))}


def expected(s: Settings = default_settings) -> tuple[Baseline, set[str], set[tuple[str, str, str]]]:
    """What a reload would put in the graph: (baseline, vertex ids, edges as (from, type, to))."""
    base = load_baseline(s.baseline_yaml)
    ex = extract(base, s.documents_dir)
    ids = {v.eid for v in base.vertices} | {v.eid for v in ex.vertices}
    edges = {(e.from_eid, e.type, e.to_eid) for e in list(base.edges) + list(ex.edges) if e.to_eid in ids}
    return base, ids, edges


def ontology_errors(s: Settings = default_settings) -> list[str]:
    onto = load_ontology(s.ontology_yaml)
    data = yaml.safe_load(s.baseline_yaml.read_text())
    return [str(f) for f in onto.check_self() + check_baseline(onto, data, s.documents_dir) if f.level == "error"]


def status(graph: GraphStore, s: Settings = default_settings) -> dict:
    """The graph's contents and how they differ from the baseline and documents."""
    now = counts(graph)
    snap = graph.snapshot()
    have_ids = {v["eid"] for v in snap["vertices"]}
    have_edges = {(e["from_eid"], e["type"], e["to_eid"]) for e in snap["edges"]}
    out = {"graph": now, "baseline": str(s.baseline_yaml), "problem": None}
    try:
        _, ids, edges = expected(s)
    except Exception as e:                                     # a baseline that doesn't load: say so, don't crash
        out.update(in_step=False, problem=f"the baseline can't be read: {e}")
        return out
    out.update(missing=sorted(ids - have_ids), extra=sorted(have_ids - ids),
               missing_links=len(edges - have_edges), extra_links=len(have_edges - edges))
    out["in_step"] = not (out["missing"] or out["extra"] or out["missing_links"] or out["extra_links"])
    return out


def reset(graph: GraphStore) -> dict:
    """Empty the graph. Returns what was removed."""
    before = counts(graph)
    graph.reset()
    return {"removed": before, "graph": counts(graph)}


def reload(graph: GraphStore, s: Settings = default_settings, force: bool = False) -> dict:
    """Empty the graph and load it again from the baseline and its documents.
    Everything is read and checked first; on a problem GraphAdminError is
    raised and the graph is as it was."""
    try:
        base, _, _ = expected(s)                               # the baseline loads; every document is readable
    except Exception as e:
        raise GraphAdminError(f"the baseline or its documents can't be read ({e}); the graph was not changed") from e
    errors = ontology_errors(s)
    if errors and not force:
        raise GraphAdminError("the baseline fails the ontology check; the graph was not changed "
                              "(fix the baseline, or reload with --force):\n  " + "\n  ".join(errors))
    before = counts(graph)
    graph.reset()
    graph.load(base)
    trace = load_traceability(graph, base, s.documents_dir)
    return {"before": before, "graph": counts(graph), "traceability": trace,
            "ontology_errors": errors, "in_step": status(graph, s)["in_step"]}


# ── Command line ─────────────────────────────────────────────────────────────

def _line(c: dict) -> str:
    labels = ", ".join(f"{n} {k}" for k, n in sorted(c["by_label"].items())) or "empty"
    return f"{c['vertices']} nodes, {c['edges']} links ({labels})"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["status", "reload", "reset"])
    p.add_argument("--yes", action="store_true", help="reset: really empty the graph")
    p.add_argument("--force", action="store_true", help="reload: load a baseline that fails the ontology check")


def run(args, graph: GraphStore | None = None, s: Settings | None = None) -> int:
    """`args` has action, yes, force and env. Returns the exit code."""
    if s is None:
        from esamkit.environment import load_profile, settings_for
        s = settings_for(load_profile(getattr(args, "env", None)))
    if graph is None:
        from esamkit.factory import get_graph_store
        try:
            graph = get_graph_store(s)
        except Exception as e:
            print(f"Can't open the graph store: {e}")
            return 1

    if args.action == "status":
        st = status(graph, s)
        print(f"Graph   : {_line(st['graph'])}")
        print(f"Baseline: {st['baseline']}")
        if st["problem"]:
            print(f"NOT CHECKED: {st['problem']}")
            return 1
        if st["in_step"]:
            print("In step: the graph holds exactly what the baseline and its documents say.")
            return 0
        print("OUT OF STEP with the baseline:")
        for key, what in (("missing", "in the baseline, not in the graph"), ("extra", "in the graph, not in the baseline")):
            if st[key]:
                print(f"  {len(st[key])} {what}: {', '.join(st[key][:12])}{' …' if len(st[key]) > 12 else ''}")
        if st["missing_links"] or st["extra_links"]:
            print(f"  links: {st['missing_links']} missing, {st['extra_links']} extra")
        print("Fix: python -m crew.main graph reload")
        return 1

    if args.action == "reset":
        if not args.yes:
            print(f"The graph holds {_line(counts(graph))}.\n"
                  "reset empties it and leaves it empty: the Cockpit, questions and the pipelines have nothing to work "
                  "with until the next reload.\nTo do that, add --yes. To rebuild it from the baseline: graph reload")
            return 2
        res = reset(graph)
        print(f"Graph reset: removed {_line(res['removed'])}. It is now empty.\n"
              "Load it again with: python -m crew.main graph reload")
        return 0

    try:
        res = reload(graph, s, force=args.force)
    except GraphAdminError as e:
        print(f"NOT RELOADED: {e}")
        return 1
    print(f"Before : {_line(res['before'])}")
    print(f"After  : {_line(res['graph'])}")
    t = res["traceability"]
    print(f"Traceability (from the documents): {t['requirements']} requirements, {t['test_cases']} test cases, "
          f"{t['verifies']} verifies links")
    for u in t["unresolved"]:
        print(f"  ! {u['test_case']} verifies {u['cites']}, which no SRS defines")
    if res["ontology_errors"]:
        print(f"Loaded with --force despite {len(res['ontology_errors'])} ontology error(s):")
        for e in res["ontology_errors"]:
            print(f"  {e}")
    print("Graph reloaded from the baseline." if res["in_step"] else "Reloaded, but the graph still differs from the baseline: run graph status.")
    return 0 if res["in_step"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Reload or reset the ESAM graph")
    add_arguments(ap)
    ap.add_argument("--env", default=None, help="environment profile (default: ESAM_ENV or local)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
