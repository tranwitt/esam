"""
Change the ontology from the command line, without editing YAML.

    python -m crew.main ontology show [values|classes|documents|rules|words|<Class>|<value set>]
    python -m crew.main ontology check

    python -m crew.main ontology add value os_class hypervisor "Separation kernel hosting several partitions"
    python -m crew.main ontology add value supplier_tier tier1 "Delivers to the prime" --new-set
    python -m crew.main ontology add attribute Subsystem supplier --description "Who builds it"
    python -m crew.main ontology add attribute Subsystem tier --type enum --values supplier_tier
    python -m crew.main ontology add document-type sss "System/Subsystem Specification" --attaches-to Vehicle System
    python -m crew.main ontology add rule --when software has_interfaces --require icd --why "Interfaced software has an ICD."
    python -m crew.main ontology add word software bootloader
    python -m crew.main ontology add answer icd "can bus" "baud rate"
    python -m crew.main ontology add synonym icd "interface sheet"

    python -m crew.main ontology set attribute Subsystem os_class --required yes
    python -m crew.main ontology set attribute Subsystem j1939_source_address --max 255

    python -m crew.main ontology remove value os_class hypervisor
    python -m crew.main ontology remove attribute Subsystem supplier
    python -m crew.main ontology remove document-type sss
    python -m crew.main ontology remove rule 4                 # the number `ontology show rules` gives
    python -m crew.main ontology remove word software bootloader

    python -m crew.main ontology undo                          # take the last change back; again for the one before
    python -m crew.main ontology history                       # the changes that can be undone
    (each takes --env <profile>; `python -m crew.ontology_admin ...` is the same thing)

  --dry-run       show the change, write nothing.
  --force         write a change even though the baseline then fails the ontology check.
  --keep-version  leave metadata.version as it is (otherwise it goes up one step).

What is checked before anything is written, and what cannot be changed from
here: esamkit/ontology_edit.py. No agent, no model.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from esamkit import ontology_edit as oe  # noqa: E402
from esamkit.config import Settings  # noqa: E402
from esamkit.ontology import check_baseline, load_ontology  # noqa: E402


def _yes_no(text: str) -> bool:
    if str(text).lower() in ("yes", "y", "true", "1"):
        return True
    if str(text).lower() in ("no", "n", "false", "0"):
        return False
    raise argparse.ArgumentTypeError(f"yes or no, not {text!r}")


def _attribute_options(p: argparse.ArgumentParser, adding: bool) -> None:
    p.add_argument("cls", metavar="CLASS", help="e.g. Subsystem")
    p.add_argument("name", metavar="NAME", help="the attribute, lower case with _")
    p.add_argument("--type", choices=oe.TYPES, help="text (the default), integer, boolean, enum, text_list")
    p.add_argument("--values", metavar="SET", help="enum, text_list: the value set its values come from")
    p.add_argument("--pattern", metavar="REGEX", help="text: a regular expression the value must match"
                   + ("" if adding else ' ("" takes the pattern off)'))
    p.add_argument("--min", help="integer: the lowest value allowed")
    p.add_argument("--max", help="integer: the highest value allowed")
    p.add_argument("--description", help="one sentence on what it is for")
    if adding:
        p.add_argument("--required", action="store_true", help="every entry of the class must have it")
    else:
        p.add_argument("--required", type=_yes_no, metavar="yes|no", help="must every entry of the class have it?")


def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="action", required=True, metavar="show | check | add | set | remove | undo | history")
    s = sub.add_parser("show", help="the ontology, or one part of it")
    s.add_argument("what", nargs="?", help="values, classes, documents, rules, words, a class or a value set")
    sub.add_parser("check", help="the ontology's own consistency, and the baseline against it")
    changing = []

    a = sub.add_parser("add", help="add a value, an attribute, a document type, a rule or words")
    kinds = a.add_subparsers(dest="kind", required=True, metavar="value | attribute | document-type | rule | word | answer | synonym")
    x = kinds.add_parser("value", help="a value in a value set")
    x.add_argument("value_set", metavar="SET")
    x.add_argument("value")
    x.add_argument("meaning", help="what the value means, in quotes")
    x.add_argument("--new-set", action="store_true", help="start the value set with this value")
    changing.append(x)
    x = kinds.add_parser("attribute", help="an attribute of a class")
    _attribute_options(x, adding=True)
    changing.append(x)
    x = kinds.add_parser("document-type", help="a kind of document")
    x.add_argument("code", help="its short code, e.g. sss")
    x.add_argument("name", help="its full name, in quotes")
    x.add_argument("--standard", help="where the name comes from")
    x.add_argument("--answers", nargs="+", metavar="WORD", help="words in a question that mean this document would answer it")
    x.add_argument("--synonyms", nargs="+", metavar="NAME", help="other names people use for it")
    x.add_argument("--attaches-to", nargs="+", metavar="CLASS", help="what it may be attached to (left out: Subsystem)")
    changing.append(x)
    x = kinds.add_parser("rule", help="a required-documents rule")
    x.add_argument("--when", nargs="+", required=True, metavar="CONDITION", help=", ".join(oe.CONDITIONS))
    x.add_argument("--require", nargs="+", required=True, metavar="TYPE", help="the document types it requires")
    x.add_argument("--why", required=True, help="the reason, in quotes")
    changing.append(x)
    for kind, first, text in (("word", "level", "words that mark a requirement's subject as system or software"),
                              ("answer", "code", "words in a question that point to a document type"),
                              ("synonym", "code", "other names for a document type")):
        for parent in (kinds,):
            x = parent.add_parser(kind, help=text)
            x.add_argument(first, metavar="LEVEL" if first == "level" else "TYPE")
            x.add_argument("words", nargs="+", metavar="WORD")
            changing.append(x)

    st = sub.add_parser("set", help="change an attribute: its type, values, limits, whether it is required")
    kinds = st.add_subparsers(dest="kind", required=True, metavar="attribute")
    x = kinds.add_parser("attribute", help="an attribute of a class")
    _attribute_options(x, adding=False)
    changing.append(x)

    r = sub.add_parser("remove", help="remove a value, an attribute, a document type, a rule or words")
    kinds = r.add_subparsers(dest="kind", required=True, metavar="value | attribute | document-type | rule | word | answer | synonym")
    x = kinds.add_parser("value")
    x.add_argument("value_set", metavar="SET")
    x.add_argument("value")
    changing.append(x)
    x = kinds.add_parser("attribute")
    x.add_argument("cls", metavar="CLASS")
    x.add_argument("name", metavar="NAME")
    changing.append(x)
    x = kinds.add_parser("document-type")
    x.add_argument("code")
    changing.append(x)
    x = kinds.add_parser("rule")
    x.add_argument("number", type=int, help="its number in `ontology show rules`")
    changing.append(x)
    for kind, first in (("word", "level"), ("answer", "code"), ("synonym", "code")):
        x = kinds.add_parser(kind)
        x.add_argument(first, metavar="LEVEL" if first == "level" else "TYPE")
        x.add_argument("words", nargs="+", metavar="WORD")
        changing.append(x)

    for x in changing:
        x.add_argument("--dry-run", action="store_true", help="show the change; write nothing")
        x.add_argument("--force", action="store_true", help="write even though the baseline then fails the ontology check")
        x.add_argument("--keep-version", action="store_true", help="leave metadata.version as it is")
    u = sub.add_parser("undo", help="take the last change back (again: the one before)")
    u.add_argument("--dry-run", action="store_true", help="say what would be undone; write nothing")
    u.add_argument("--force", action="store_true", help="restore the earlier file even if it was edited since, or the baseline then fails")
    sub.add_parser("history", help="the changes that can be undone, newest first")


def _change(args, edit: oe.Edit) -> dict:
    adding = args.action == "add"
    if args.kind == "value":
        return edit.add_value(args.value_set, args.value, args.meaning, args.new_set) if adding \
            else edit.remove_value(args.value_set, args.value)
    if args.kind == "attribute":
        if args.action == "remove":
            return edit.remove_attribute(args.cls, args.name)
        given = {"type": args.type, "values": args.values, "pattern": args.pattern, "min": args.min, "max": args.max,
                 "description": args.description}
        if adding:
            return edit.add_attribute(args.cls, args.name, required=True if args.required else None, **given)
        return edit.set_attribute(args.cls, args.name, required=args.required, **given)
    if args.kind == "document-type":
        return edit.add_document_type(args.code, args.name, args.standard, args.answers, args.synonyms, args.attaches_to) \
            if adding else edit.remove_document_type(args.code)
    if args.kind == "rule":
        return edit.add_rule(args.when, args.require, args.why) if adding else edit.remove_rule(args.number)
    if args.kind == "word":
        return edit.word(args.level, args.words, add=adding)
    if args.kind == "answer":
        return edit.answer(args.code, args.words, add=adding)
    return edit.synonym(args.code, args.words, add=adding)


def run(args, s: Settings | None = None) -> int:
    """Returns the exit code. `s` is for tests; normally the profile decides."""
    if s is None:
        from esamkit.environment import load_profile, settings_for
        s = settings_for(load_profile(getattr(args, "env", None)))
    path = Path(s.ontology_yaml)

    if args.action == "show":
        try:
            print("\n".join(oe.show(load_ontology(path), args.what)))
        except oe.OntologyEditError as e:
            print(e)
            return 1
        return 0

    if args.action == "check":
        onto = load_ontology(path)
        findings = onto.check_self() + check_baseline(onto, yaml.safe_load(Path(s.baseline_yaml).read_text()), s.documents_dir)
        for f in findings:
            print(f)
        errors = sum(1 for f in findings if f.level == "error")
        print(f"{errors} error(s), {len(findings) - errors} warning(s): ontology {path}, baseline {s.baseline_yaml}" if findings
              else f"The ontology is consistent and the baseline passes it ({path}).")
        return 1 if errors else 0

    if args.action == "history":
        found = oe.history(path)
        print(f"Changes to {path} that can be undone, newest first:" if found else
              f"No change made by these commands is remembered for {path}.")
        for n, e in enumerate(found, 1):
            print(f"  {n:>2}. {e['when']}  {e['change']}")
        if found:
            print("Undo the newest: python -m crew.main ontology undo")
        return 0

    if args.action == "undo":
        try:
            res = oe.undo(path, s.baseline_yaml, force=args.force, dry_run=args.dry_run)
        except oe.OntologyEditError as e:
            print(f"NOT CHANGED: {e}")
            return 1
        print(f"{'Would undo' if args.dry_run else 'Undone'}: {res['change']} (made {res['when']}). "
              + ("The file would be" if args.dry_run else "The file is") + " as it was before that change."
              + (" An edit made since was discarded (--force)." if res["edited_since"] and not args.dry_run else ""))
        for line in res["problems"]:
            print(f"  now a problem (--force): {line}")
        print(f"{res['left']} more change(s) can be undone.")
        print(f"Nothing was written (--dry-run): {path}" if args.dry_run else f"Written to {path}")
        return 0

    try:
        res = _change(args, oe.Edit(path, s.baseline_yaml, dry_run=args.dry_run, force=args.force,
                                    keep_version=args.keep_version))
    except oe.OntologyEditError as e:
        print(f"NOT CHANGED: {e}")
        return 1
    print(f"{'Would change' if args.dry_run else 'Changed'}: {res['change']}\n\n{res['text']}\n")
    for note in res["notes"]:
        print(f"Note: {note}")
    if res["baseline_errors"]:
        print("The baseline now FAILS the ontology check (--force):\n  " + "\n  ".join(res["baseline_errors"]))
    if args.dry_run:
        print(f"Nothing was written (--dry-run): {path}")
        return 0
    print(f"Written to {path}" + (f" (now version {res['version']})" if res["version"] else ""))
    if res.get("undo"):
        print("To take this back: python -m crew.main ontology undo")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Show, check or change the ESAM ontology")
    add_arguments(ap)
    ap.add_argument("--env", default=None, help="environment profile (default: ESAM_ENV or local)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
