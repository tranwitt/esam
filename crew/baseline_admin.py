"""
Add to the vehicle baseline from the command line, without editing YAML.

    python -m crew.main baseline show                      # the tree: ids, names, WBS codes, documents
    python -m crew.main baseline check                     # the ontology check of the baseline, WBS standard included
    python -m crew.main baseline standard                  # the WBS numbers MIL-STD-881F Appendix G fixes
    python -m crew.main baseline add system "Fire Control"                 # WBS 1.1.1.9, the standard's number for it
    python -m crew.main baseline add subsystem "Turret Drive Controller (TDC)" --in SYS-FC --safety-critical yes --os rtos
    python -m crew.main baseline add function "Turret slew control" --in SUB-TDC --description "Drives the turret to the commanded bearing."
    python -m crew.main baseline add vehicle "SYNTH-MTV Medium Tactical Vehicle"
    python -m crew.main baseline add family "Tracked Family" --in V-001
    python -m crew.main baseline add variant "Ambulance" --in FAM-001
    python -m crew.main baseline remove SYS-FC             # only an entry with nothing under it or attached to it
    python -m crew.main baseline undo                      # take the last change back; again for the one before
    python -m crew.main baseline history                   # the changes that can be undone
    (each takes --env <profile>; `python -m crew.baseline_admin ...` is the same thing)

  --in ID         the entry it goes in. Left out when there is only one possible.
  --id ID         instead of the id made from the name.
  --wbs CODE      a whole WBS code, or just its last number. Left out: the number the standard gives.
  --dry-run       show what would be written, and write nothing.
  --reload-graph  rebuild the graph afterwards (otherwise: python -m crew.main graph reload).

What happens, the checks made before anything is written, and what a new
vehicle brings with it: esamkit/baseline_edit.py. No agent, no model.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from esamkit import baseline_edit as be  # noqa: E402
from esamkit.config import Settings  # noqa: E402
from esamkit.ontology import check_baseline, load_ontology  # noqa: E402

# option -> the ontology attribute it sets
_OPTIONS = {"safety_critical": "safety_critical", "alias": "aliases", "os": "os_class",
            "safety_classification": "safety_classification", "j1939_address": "j1939_source_address",
            "description": "description", "role": "role", "universality": "universality"}


def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="action", required=True, metavar="show | check | standard | add | remove | undo | history")
    sub.add_parser("show", help="the vehicle tree")
    sub.add_parser("check", help="the ontology check of the baseline")
    sub.add_parser("standard", help="the WBS numbers the standard fixes, as the ontology holds them")
    a = sub.add_parser("add", help="add a vehicle, family, variant, system, subsystem or function")
    a.add_argument("kind", choices=list(be.KINDS))
    a.add_argument("name", help="its name, in quotes")
    a.add_argument("--in", dest="parent", metavar="ID", help="the entry it goes in (left out when only one is possible)")
    a.add_argument("--id", help="instead of the id made from the name")
    a.add_argument("--wbs", help="family, variant, system: a whole WBS code or its last number, instead of the standard's")
    a.add_argument("--safety-critical", metavar="yes|no", help="subsystem (required): can its failure contribute to a hazard?")
    a.add_argument("--alias", action="append", metavar="NAME", help="subsystem: an everyday name people use (repeatable)")
    a.add_argument("--os", metavar="CLASS", help="subsystem: its operating-system class (rtos, gpos)")
    a.add_argument("--safety-classification", metavar="TEXT", help='subsystem, function: e.g. "SwCI 1"')
    a.add_argument("--j1939-address", metavar="N", help="subsystem: its J1939 source address")
    a.add_argument("--description", help="function: one sentence on what it does")
    a.add_argument("--role", help="variant: lead or derivative")
    a.add_argument("--universality", help="system: universal or variant_specific")
    a.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="any other attribute the ontology has for it")
    a.add_argument("--family-name", help="vehicle: the name of its first family")
    a.add_argument("--variant-name", help="vehicle: the name of its lead variant")
    a.add_argument("--bare", action="store_true", help="vehicle: add it alone, without a family and a lead variant")
    r = sub.add_parser("remove", help="take out an entry with nothing under it and nothing attached to it")
    r.add_argument("id")
    u = sub.add_parser("undo", help="take the last add or remove back (again: the one before)")
    u.add_argument("--force", action="store_true", help="restore the earlier file even if it was edited since")
    sub.add_parser("history", help="the changes that can be undone, newest first")
    for x in (a, r, u):
        x.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
        x.add_argument("--reload-graph", action="store_true", help="rebuild the graph from the baseline afterwards")


def _attributes(args) -> dict:
    out = {}
    for option, key in _OPTIONS.items():
        value = getattr(args, option, None)
        if value not in (None, []):
            out[key] = value
    for pair in args.set:
        key, eq, value = pair.partition("=")
        if not eq or not key.strip():
            raise be.BaselineEditError(f"--set takes KEY=VALUE, not {pair!r}")
        out[key.strip()] = value.strip()
    return out


def _after(args, s: Settings, graph) -> int:
    if getattr(args, "reload_graph", False):
        from crew import graph_admin
        print("\nGraph:")
        return graph_admin.run(argparse.Namespace(action="reload", yes=False, force=False, env=getattr(args, "env", None)),
                               graph=graph, s=s)
    print("The graph was not changed. To bring it in step: python -m crew.main graph reload")
    return 0


def run(args, s: Settings | None = None, graph=None) -> int:
    """Returns the exit code. `s` and `graph` are for tests; normally the profile decides."""
    if s is None:
        from esamkit.environment import load_profile, settings_for
        s = settings_for(load_profile(getattr(args, "env", None)))
    onto = load_ontology(s.ontology_yaml)
    path = Path(s.baseline_yaml)

    if args.action == "show":
        print(f"Baseline: {path}\n")
        print("\n".join(be.tree(yaml.safe_load(path.read_text()))))
        return 0

    if args.action == "standard":
        print("\n".join(be.standard(onto)))
        return 0

    if args.action == "check":
        findings = check_baseline(onto, yaml.safe_load(path.read_text()), s.documents_dir)
        for f in findings:
            print(f)
        errors = sum(1 for f in findings if f.level == "error")
        print(f"{errors} error(s), {len(findings) - errors} warning(s) in {path}" if findings
              else f"The baseline passes the ontology check ({path}).")
        return 1 if errors else 0

    if args.action == "history":
        found = be.history(path)
        print(f"Changes to {path} that can be undone, newest first:" if found else
              f"No change made by these commands is remembered for {path}.")
        for n, e in enumerate(found, 1):
            print(f"  {n:>2}. {e['when']}  {e['change']}")
        if found:
            print("Undo the newest: python -m crew.main baseline undo")
        return 0

    try:
        if args.action == "undo":
            res = be.undo(path, onto, force=args.force, dry_run=args.dry_run)
            print(f"{'Would undo' if args.dry_run else 'Undone'}: {res['change']} (made {res['when']}). "
                  + ("The file would be" if args.dry_run else "The file is") + " as it was before that change."
                  + (" An edit made since was discarded (--force)." if res["edited_since"] and not args.dry_run else ""))
            for line in res["problems"]:
                print(f"  now fails the ontology check (--force): {line}")
            print(f"{res['left']} more change(s) can be undone.")
        elif args.action == "remove":
            res = be.remove(args.id, path, onto, dry_run=args.dry_run)
            print(f"{'Would remove' if args.dry_run else 'Removed'} {res['class']} {res['id']} ({res['name']}) "
                  f"from {res['parent'] or 'the baseline'}:\n\n{res['text']}\n")
        else:
            res = be.add(args.kind, args.name, path, onto, parent=args.parent, id=args.id, wbs=args.wbs,
                         attributes=_attributes(args), bare=args.bare, family_name=args.family_name,
                         variant_name=args.variant_name, dry_run=args.dry_run)
            first, rest = res["created"][0], res["created"][1:]
            print(f"{'Would add' if args.dry_run else 'Added'} {first['class']} {first['id']} ({first['name']})"
                  + (f", WBS {first['wbs_code']}" if first["wbs_code"] else "")
                  + (f", in {res['parent']}" if res["parent"] else "")
                  + "".join(f"\n  with {c['class']} {c['id']} ({c['name']}), WBS {c['wbs_code']}" for c in rest)
                  + f":\n\n{res['text']}\n")
            for note in res["notes"]:
                print(f"Note: {note}")
            nxt = {"Vehicle": f"add system \"...\" --in {res['created'][-1]['id']}" if rest else f"add family \"...\" --in {first['id']}",
                   "VehicleFamily": f"add variant \"...\" --in {first['id']}", "Variant": f"add system \"...\" --in {first['id']}",
                   "System": f"add subsystem \"...\" --in {first['id']} --safety-critical yes|no",
                   "Subsystem": f"add function \"...\" --in {first['id']}"}.get(first["class"])
            if nxt:
                print(f"Next: python -m crew.main baseline {nxt}")
    except be.BaselineEditError as e:
        print(f"NOT CHANGED: {e}")
        return 1
    if args.dry_run:
        print(f"Nothing was written (--dry-run): {path}")
        return 0
    print(f"Written to {path}")
    if args.action != "undo" and res.get("undo"):
        print("To take this back: python -m crew.main baseline undo")
    return _after(args, s, graph)


def main() -> None:
    ap = argparse.ArgumentParser(description="Add to the ESAM vehicle baseline: vehicles, systems, subsystems, functions")
    add_arguments(ap)
    ap.add_argument("--env", default=None, help="environment profile (default: ESAM_ENV or local)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
