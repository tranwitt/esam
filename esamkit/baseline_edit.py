"""
Add to the vehicle baseline with simple inputs: a vehicle, a family, a
variant, a system, a subsystem or a function.

    python -m crew.main baseline show
    python -m crew.main baseline add system "Fire Control" --wbs 9
    python -m crew.main baseline add subsystem "Turret Drive Controller (TDC)" --in SYS-FC --safety-critical yes --os rtos
    python -m crew.main baseline add function "Turret slew control" --in SUB-TDC --description "Drives the turret to the commanded bearing."
    python -m crew.main baseline add vehicle "SYNTH-MTV Medium Tactical Vehicle"
    (crew/baseline_admin.py is the command line; this module has no CrewAI and no database)

The baseline (data/baseline/*.yaml) stays the SE's file. An addition is
written INTO it as text, in the same form an SE would type, at the end of
the list it belongs to; nothing else in the file moves, and every comment
stays. So `git diff` shows exactly the new entry.

What you give, and what is worked out when you leave it out:

    name      always given.
    parent    --in ID. Left out, it is the only possible parent when there is
              just one (a single variant for a system, say); otherwise the
              command lists the choices.
    id        made from the name in the ontology's form: SYS-FC, SUB-TDC,
              FN-TDC-TSC, V-002, FAM-002, VAR-002. An acronym in brackets in
              the name is used as it is.
    WBS code  by MIL-STD-881F Appendix G (the ontology's `wbs_standard`; see
              `baseline standard`). --wbs takes a whole code or its last number.
    the rest  one option per attribute of the ontology (--safety-critical,
              --alias, --os, --description, --role ...), or --set key=value.

WBS codes follow the standard's table, and a code that departs from it is
refused:

    vehicle   always 1.0. Every ground vehicle system has its own WBS.
    family    1.1 Family of Vehicles, then 1.2 Secondary Vehicle. There is no
              third: 1.3 onwards are the common elements (Systems
              Engineering, Program Management ...).
    variant   1.1.1 is the lead variant; 1.1.2, 1.1.3 ... the others.
    system    the number the table gives its name: "Fire Control" is
              1.1.1.9, "Hull" 1.1.1.2, a software release 1.1.1.15. A system
              the table does not name is an "Other Subsystems" element,
              1.1.1.16 onwards. A number from 1 to 14 can only carry the
              standard's name for it.

A subsystem must say whether it is safety-critical: that is never guessed.

A new VEHICLE goes under `vehicles:` beside the first one, with its family
(1.1) and lead variant (1.1.1) so systems can be added to it straight away
(--bare: just the vehicle). It is a ground vehicle system of its own, so its
WBS starts at 1.0 again and its codes repeat the first vehicle's; ids, not
WBS codes, tell the two apart.

Before the file is changed the result is checked: it must still be valid
YAML, the new entry must sit where it was meant to, the ontology check must
find no new error, and the baseline must load. Otherwise nothing is written
and the reason is given. The graph is NOT changed: `graph reload` afterwards
(or --reload-graph) brings it in step.

`remove` takes out an entry that has nothing under it and nothing pointing
at it (no children, no documents, no interfaces).
"""

import json
import os
import re
from pathlib import Path

import yaml

from .baseline import load_baseline, vehicle_roots
from .diagram_baseline import DiagramBaselineError, _block_end, _indent, make_id
from .ontology import _CHILDREN, Ontology, check_baseline, default_ontology, standard_element, wbs_findings

KINDS = {"vehicle": "Vehicle", "family": "VehicleFamily", "variant": "Variant",
         "system": "System", "subsystem": "Subsystem", "function": "Function"}
PARENT = {child: (parent, key) for parent, (key, child) in _CHILDREN.items()}     # class -> (parent class, list key)
_NUMBERED = {"Vehicle": "V", "VehicleFamily": "FAM", "Variant": "VAR"}
_VEHICLES_NOTE = "# Further ground vehicle systems, written like `vehicle:` above. Each has its own WBS, from 1.0."
_NOT_SETTABLE = {"status", "derived_from"}           # written by the diagram reader, not by hand


class BaselineEditError(ValueError):
    """Nothing was written. The message says why and what to give instead."""


# ── Reading the tree ─────────────────────────────────────────────────────────

def nodes(data: dict) -> list[dict]:
    """Every entry of the vehicle tree, top down:
    [{"id", "class", "name", "wbs_code", "parent" (id or None), "depth", "node" (the YAML mapping)}]"""
    out = []

    def walk(cname, node, parent, depth):
        out.append({"id": node.get("id"), "class": cname, "name": node.get("name", ""), "wbs_code": node.get("wbs_code"),
                    "parent": parent, "depth": depth, "node": node})
        key, child = _CHILDREN.get(cname, (None, None))
        for c in (node.get(key) or []) if key else []:
            walk(child, c, node.get("id"), depth + 1)

    for veh in vehicle_roots(data):
        walk("Vehicle", veh, None, 0)
    return out


def tree(data: dict) -> list[str]:
    """The vehicle tree as lines of text, with what is attached to each entry."""
    docs: dict[str, list[str]] = {}
    for art in data.get("artifacts") or []:
        docs.setdefault(art.get("attached_to"), []).append(art.get("id", "?"))
    lines = []
    for n in nodes(data):
        extra = [f"WBS {n['wbs_code']}"] if n["wbs_code"] else []
        node = n["node"]
        if n["class"] == "Subsystem":
            extra.append("safety-critical" if node.get("safety_critical") else "not safety-critical")
        if node.get("status") == "candidate":
            extra.append("candidate")
        if docs.get(n["id"]):
            extra.append("documents: " + ", ".join(docs[n["id"]]))
        lines.append(f"{'  ' * n['depth']}{n['id']}  {n['name']}" + (f"  ({'; '.join(extra)})" if extra else ""))
    return lines


def _all_ids(data: dict) -> set[str]:
    return {n["id"] for n in nodes(data)} | {a.get("id") for a in data.get("artifacts") or []}


# ── Working out what was left out ────────────────────────────────────────────

def _pick_parent(kind: str, cname: str, parent: str | None, tree_nodes: list[dict]) -> dict:
    want, _ = PARENT[cname]
    by_id = {n["id"]: n for n in tree_nodes}
    choices = [n for n in tree_nodes if n["class"] == want]
    listing = "; ".join(f"{n['id']} ({n['name']})" for n in choices) or "none yet"
    article = "an" if want[0] in "AEIOU" else "a"
    if parent:
        if parent not in by_id:
            raise BaselineEditError(f"{parent} is not in the baseline. A {kind} goes in {article} {want}: {listing}")
        if by_id[parent]["class"] != want:
            raise BaselineEditError(f"{parent} is a {by_id[parent]['class']}; a {kind} goes in {article} {want}: {listing}")
        return by_id[parent]
    if len(choices) == 1:
        return choices[0]
    if not choices:
        raise BaselineEditError(f"a {kind} goes in {article} {want}, and the baseline has none: add that first")
    raise BaselineEditError(f"which {want} does it go in? Add --in ID. The choices: {listing}")


def _next_number(prefix: str, taken: set[str]) -> str:
    used = [int(m[1]) for x in taken if x and (m := re.fullmatch(rf"{prefix}-(\d{{3}})", x))]
    return f"{prefix}-{max(used, default=0) + 1:03d}"


def _make_id(cname: str, name: str, parent: dict | None, taken: set[str], onto: Ontology) -> str:
    pattern = onto.classes[cname]["id_pattern"]
    if cname in _NUMBERED:
        return _next_number(_NUMBERED[cname], taken)
    prefix = {"System": "SYS", "Subsystem": "SUB"}.get(cname)
    if cname == "Function":                              # FN-<the subsystem's code>-<the function's>
        prefix = "FN-" + parent["id"].split("-")[1]
    try:
        return make_id(prefix, name, taken, pattern)
    except DiagramBaselineError as e:
        raise BaselineEditError(f"{e}; give one with --id") from e


def _wbs(cname: str, name: str, wbs: str | None, parent: dict | None, tree_nodes: list[dict], onto: Ontology,
         notes: list[str]) -> str | None:
    """The WBS code of the new entry by the standard's table (the ontology's
    `wbs_standard`), or None for a class that has none. `notes` gets what
    the user should know about the number chosen."""
    if cname in ("Subsystem", "Function"):
        if wbs:
            raise BaselineEditError(f"a {cname.lower()} has no WBS code of its own (it takes its system's): leave --wbs out")
        return None
    std = onto.wbs_standard
    ref = std.get("reference", "the WBS standard")
    wbs = str(wbs).strip() if wbs else None
    if cname == "Vehicle":
        root = std.get("vehicle", "1.0")
        if wbs and wbs not in (root, root.split(".")[0]):
            raise BaselineEditError(f"a ground vehicle system is WBS {root} in {ref}: every vehicle system has its own WBS, "
                                    f"so a further vehicle is {root} too. Leave --wbs out")
        return root
    root = re.sub(r"\.0$", "", str(parent["wbs_code"] or ""))
    if not root:
        if wbs and "." in wbs:
            return wbs
        raise BaselineEditError(f"{parent['id']} has no WBS code yet, so one can't be worked out: give the whole code with --wbs")
    if wbs:
        return wbs if "." in wbs else f"{root}.{wbs}"
    siblings = {int(m[1]): n for n in tree_nodes if n["parent"] == parent["id"] and n["wbs_code"]
                and (m := re.fullmatch(rf"{re.escape(root)}\.(\d+)", str(n["wbs_code"])))}
    if not std:                                           # no table in the ontology: the next free number
        return f"{root}.{max(siblings, default=0) + 1}"

    if cname == "VehicleFamily":
        elements = std["vehicle_elements"]
        free = [n for n in elements if n not in siblings]
        if not free:
            raise BaselineEditError(
                f"{parent['id']} already has " + " and ".join(f"{root}.{n} ({siblings[n]['id']})" for n in elements)
                + f". {ref} has no further vehicle element: {root}.{std.get('first_common_element', 3)} onwards are the common "
                  "elements (Systems Engineering, Program Management, ...). A further vehicle system is its own WBS: "
                  "add vehicle \"...\"")
        if elements[free[0]] != elements[min(elements)]:
            notes.append(f"WBS {root}.{free[0]} is the standard's {elements[free[0]]!r} element ({root}.{min(elements)} "
                         f"{elements[min(elements)]} is {siblings[min(elements)]['id']})")
        return f"{root}.{free[0]}"

    if cname == "Variant":
        lead = std.get("lead_variant", 1)
        if lead not in siblings:
            return f"{root}.{lead}"
        return f"{root}.{next(n for n in range(lead + 1, 1000) if n not in siblings)}"

    # a system: the number the table gives its name, else an "Other Subsystems" element
    number = standard_element(onto, name)
    if number is not None and number in siblings:
        other = siblings[number]
        hint = (" The standard writes 'Software Release 1...n' and gives further releases no number: give one with --wbs "
                f"({std['other_from']} or above)." if number == std.get("software_release") else
                f" Add what you have in mind as a subsystem of {other['id']}, or give it a name of its own.")
        raise BaselineEditError(f"{parent['id']} already has WBS {root}.{number} ({other['id']}, {other['name']}), which is "
                                f"where {ref} puts {name!r}.{hint}")
    if number is not None:
        element = std["variant_elements"].get(number)
        if element and " ".join(name.lower().split()) != element.lower():
            notes.append(f"WBS {root}.{number} is {element!r} in {ref}")
        return f"{root}.{number}"
    number = next(n for n in range(std["other_from"], 1000) if n not in siblings)
    notes.append(f"{name!r} is not one of the elements {ref} names, so it is an 'Other Subsystems' element: WBS {root}.{number} "
                 f"({root}.{std['other_from']} onwards). If it belongs to a standard element, add it as a subsystem of that "
                 "system instead: python -m crew.main baseline standard")
    return f"{root}.{number}"


def _yes_no(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise BaselineEditError(f"give yes or no, not {value!r}")


def _attributes(cname: str, given: dict, onto: Ontology) -> dict:
    """The attributes to write, in the ontology's order, with yes/no and numbers read from text."""
    specs = onto.classes[cname].get("attributes") or {}
    allowed = [k for k in specs if k not in _NOT_SETTABLE and k != "wbs_code"]
    out = {}
    for key, value in given.items():
        if value is None or value == [] or value == "":
            continue
        if key not in allowed:
            raise BaselineEditError(f"{key!r} is not an attribute of a {cname} in the ontology. It has: "
                                    + (", ".join(allowed) or "none besides its name"))
        kind = specs[key].get("type")
        if kind == "boolean":
            try:
                value = _yes_no(value)
            except BaselineEditError as e:
                raise BaselineEditError(f"{key}: {e}") from None
        elif kind == "integer":
            try:
                value = int(str(value).strip())
            except ValueError:
                raise BaselineEditError(f"{key} must be a whole number, not {value!r}") from None
        elif kind == "text_list":
            value = [str(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
        else:
            value = str(value)
        out[key] = value
    return {k: out[k] for k in specs if k in out}


# ── Writing ──────────────────────────────────────────────────────────────────

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", text) and text.lower() not in (
        "true", "false", "yes", "no", "on", "off", "null", "y", "n") else json.dumps(text, ensure_ascii=False)


def _entry(entry: dict, indent: int) -> list[str]:
    """The lines of one entry, the dash at column `indent`. `entry["children"]` are written beneath it."""
    pad, inner = " " * indent, " " * (indent + 2)
    out = [f"{pad}- id: {entry['id']}", f"{inner}name: {json.dumps(entry['name'], ensure_ascii=False)}"]
    if entry.get("wbs_code"):
        out.append(f"{inner}wbs_code: {json.dumps(entry['wbs_code'])}")
    out += [f"{inner}{k}: {_scalar(v)}" for k, v in entry["attributes"].items()]
    if entry.get("children"):
        out.append(f"{inner}{_CHILDREN[entry['class']][0]}:")
        for child in entry["children"]:
            out += _entry(child, indent + 4)
    return out


def _key_line(lines: list[str], start: int, end: int, key: str, indent: int) -> int | None:
    for i in range(start, end):
        body = lines[i].split("#")[0].rstrip()
        if _indent(lines[i]) == indent and re.fullmatch(rf"\s*{key}:\s*(\[\s*\])?", body):
            return i
        if _indent(lines[i]) == indent and body.strip().startswith(f"{key}:"):
            raise BaselineEditError(f"the `{key}:` list at line {i + 1} is written on one line; write it one entry per line")
    return None


def _locate(lines: list[str], eid: str, data: dict) -> tuple[int, int]:
    """(line where the entry starts, the column its keys hang from) for an id in the tree."""
    if (data.get("vehicle") or {}).get("id") == eid:
        hits = [i for i, ln in enumerate(lines) if re.fullmatch(r"vehicle:\s*(#.*)?", ln)]
        if len(hits) != 1:
            raise BaselineEditError("could not find the line `vehicle:` at the start of a line in the baseline")
        return hits[0], 0
    hits = [i for i, ln in enumerate(lines) if re.fullmatch(rf"\s*- id:\s*[\"']?{re.escape(eid)}[\"']?\s*(#.*)?", ln)]
    if len(hits) != 1:
        raise BaselineEditError(f"could not find the entry `- id: {eid}` in the baseline file (an entry must be written "
                                "one attribute per line, starting with its id)")
    return hits[0], _indent(lines[hits[0]])


def _insert(lines: list[str], entry: dict, parent: dict | None, data: dict) -> list[str]:
    """Put the entry's lines into `lines` (in place); returns the lines added."""
    if entry["class"] == "Vehicle":
        top = [i for i, ln in enumerate(lines) if re.fullmatch(r"vehicles:\s*(\[\s*\])?\s*(#.*)?", ln)]
        if top:
            lines[top[0]] = "vehicles:"
            at, new = _block_end(lines, top[0]), _entry(entry, 2)
        else:
            start, _ = _locate(lines, data["vehicle"]["id"], data)
            at = _block_end(lines, start)
            new = ["", _VEHICLES_NOTE, "vehicles:"] + _entry(entry, 2)
        lines[at:at] = new
        return new
    key = PARENT[entry["class"]][1]
    start, base = _locate(lines, parent["id"], data)
    end = _block_end(lines, start)
    have = _key_line(lines, start + 1, end, key, base + 2)
    new = _entry(entry, base + 4)
    if have is None:
        new = [" " * (base + 2) + f"{key}:"] + new
        at = end
    else:
        lines[have] = " " * (base + 2) + f"{key}:" + (("  #" + lines[have].split("#", 1)[1]) if "#" in lines[have] else "")
        at = _block_end(lines, have)
    lines[at:at] = new
    return new


def _errors(onto: Ontology, data: dict) -> set[str]:
    return {f"{f.where}: {f.message}" for f in check_baseline(onto, data) if f.level == "error"}


def _write(path: Path, text: str) -> None:
    """Replace the file's contents, and read them back: a write that did not
    reach the disk (a read-only file or folder, a full disk) is an error,
    never a silent success."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise BaselineEditError(f"NOT written: {path} could not be changed ({e.strerror or e}). Check that the file and "
                                "its folder can be written by this user") from e
    if path.read_text() != text:
        raise BaselineEditError(f"NOT written: {path} does not hold the change after writing it")


def _commit(path: Path, before: str, after: str, onto: Ontology, dry_run: bool, expect: list[tuple[str, str | None]],
            gone: str | None = None) -> None:
    """Check `after` and write it. `expect` = [(id, parent id)] that must be there; `gone` = an id that must not."""
    try:
        data = yaml.safe_load(after)
    except yaml.YAMLError as e:
        raise BaselineEditError(f"the result would not be valid YAML ({e}); nothing was written") from e
    where = {n["id"]: n["parent"] for n in nodes(data)}
    for eid, parent in expect:
        if eid not in where or where[eid] != parent:
            raise BaselineEditError(f"{eid} would not land under {parent or 'the top'} (the file is laid out in a way this "
                                    "command does not follow); nothing was written. Add it by hand.")
    if gone and gone in where:
        raise BaselineEditError(f"{gone} could not be taken out cleanly; nothing was written")
    was = yaml.safe_load(before)
    new = _errors(onto, data) - _errors(onto, was)
    if new:
        raise BaselineEditError("not written: the ontology check would fail.\n  " + "\n  ".join(sorted(new)))
    off = {f"{f.where}: {f.message}" for f in wbs_findings(onto, data)} - {f"{f.where}: {f.message}" for f in wbs_findings(onto, was)}
    if off:
        raise BaselineEditError("not written: the WBS code would not follow the standard.\n  " + "\n  ".join(sorted(off)))
    if dry_run:
        return
    _write(path, after)
    try:
        load_baseline(path)
    except Exception as e:
        _write(path, before)
        raise BaselineEditError(f"not written: the baseline would not load ({e})") from e


def add(kind: str, name: str, baseline_yaml: Path, onto: Ontology | None = None, parent: str | None = None,
        id: str | None = None, wbs: str | None = None, attributes: dict | None = None, bare: bool = False,
        family_name: str | None = None, variant_name: str | None = None, dry_run: bool = False) -> dict:
    """Add one entry (for a vehicle: with its first family and lead variant unless `bare`).

    Returns {"id", "class", "name", "parent", "wbs_code", "attributes", "created": [{id, class, name, wbs_code}],
             "text": the lines added, "baseline": path, "written": bool, "notes": [..]}.
    Raises BaselineEditError, with the file untouched, when something is missing or wrong."""
    onto = onto or default_ontology()
    if kind not in KINDS:
        raise BaselineEditError(f"can add: {', '.join(KINDS)} (not {kind!r})")
    cname = KINDS[kind]
    name = " ".join(str(name or "").split())
    if not name:
        raise BaselineEditError(f"give the {kind}'s name")
    path = Path(baseline_yaml)
    before = path.read_text()
    data = yaml.safe_load(before)
    tree_nodes = nodes(data)
    taken = _all_ids(data)
    notes: list[str] = []

    same = [n for n in tree_nodes if n["class"] == cname and n["name"].strip().lower() == name.lower()]
    par = None if cname == "Vehicle" else _pick_parent(kind, cname, parent, tree_nodes)
    if cname == "Vehicle" and parent:
        raise BaselineEditError("a vehicle is at the top: it goes in nothing (leave --in out)")
    if any(n["parent"] == (par["id"] if par else None) for n in same):
        raise BaselineEditError(f"{same[0]['id']} is already a {kind} called {name!r} in the same place")
    if same:
        notes.append(f"{same[0]['id']} is also called {name!r} (under {same[0]['parent']})")

    attrs = _attributes(cname, dict(attributes or {}), onto)
    if cname == "Subsystem" and "safety_critical" not in attrs:
        raise BaselineEditError("say whether the subsystem is safety-critical (can its failure contribute to a hazard?): "
                                "--safety-critical yes  or  --safety-critical no")
    if id:
        if id in taken:
            raise BaselineEditError(f"the id {id} is already used in the baseline")
        if not re.match(onto.classes[cname]["id_pattern"], id):
            raise BaselineEditError(f"{id} is not in the form of a {cname} id ({onto.classes[cname]['id_pattern']})")
    eid = id or _make_id(cname, name, par, taken, onto)
    code = _wbs(cname, name, wbs, par, tree_nodes, onto, notes)
    if cname == "Variant" and "role" not in attrs:
        lead = f"{re.sub(r'[.]0$', '', str(par['wbs_code'] or ''))}.{onto.wbs_standard.get('lead_variant', 1)}"
        siblings = [n for n in tree_nodes if n["parent"] == par["id"]]
        is_lead = code == lead if par["wbs_code"] else not any(n["node"].get("role") == "lead" for n in siblings)
        attrs = {"role": "lead" if is_lead else "derivative", **attrs}
    if cname == "System" and "universality" not in attrs:
        attrs = {"universality": "universal", **attrs}

    entry = {"id": eid, "class": cname, "name": name, "wbs_code": code, "attributes": attrs, "children": []}
    created = [entry]
    if cname == "Vehicle" and not bare:
        root = re.sub(r"\.0$", "", code)
        fam = {"id": _next_number("FAM", taken), "class": "VehicleFamily", "wbs_code": f"{root}.1", "attributes": {},
               "name": " ".join((family_name or f"{name} Family of Vehicles").split()), "children": []}
        var = {"id": _next_number("VAR", taken), "class": "Variant", "wbs_code": f"{root}.1.1", "attributes": {"role": "lead"},
               "name": " ".join((variant_name or "Lead Variant").split()), "children": []}
        fam["children"], entry["children"] = [var], [fam]
        created += [fam, var]
    elif family_name or variant_name or bare:
        if cname != "Vehicle":
            raise BaselineEditError("--family-name, --variant-name and --bare go with `add vehicle` only")
        if family_name or variant_name:
            raise BaselineEditError("--bare adds the vehicle alone: leave --family-name and --variant-name out")

    lines = before.split("\n")
    try:
        new = _insert(lines, entry, par, data)
    except DiagramBaselineError as e:
        raise BaselineEditError(f"{e}; nothing was written") from e
    after = "\n".join(lines)
    expect, up = [], par["id"] if par else None
    for c in created:
        expect.append((c["id"], up))
        up = c["id"]
    _commit(path, before, after, onto, dry_run, expect)
    return {"id": eid, "class": cname, "name": name, "parent": par["id"] if par else None, "wbs_code": code,
            "attributes": attrs, "text": "\n".join(new), "baseline": str(path), "written": not dry_run, "notes": notes,
            "created": [{"id": c["id"], "class": c["class"], "name": c["name"], "wbs_code": c["wbs_code"]} for c in created]}


def remove(eid: str, baseline_yaml: Path, onto: Ontology | None = None, dry_run: bool = False) -> dict:
    """Take out an entry that has nothing under it and nothing pointing at it."""
    onto = onto or default_ontology()
    path = Path(baseline_yaml)
    before = path.read_text()
    data = yaml.safe_load(before)
    by_id = {n["id"]: n for n in nodes(data)}
    if eid not in by_id:
        raise BaselineEditError(f"{eid} is not an entry of the vehicle tree" +
                                (" (it is a document: those leave through the intake pipeline's undo)"
                                 if eid in _all_ids(data) else ""))
    n = by_id[eid]
    if (data.get("vehicle") or {}).get("id") == eid:
        raise BaselineEditError(f"{eid} is the baseline's first vehicle; it cannot be removed")
    blockers = [f"it has {c['id']} under it" for c in by_id.values() if c["parent"] == eid]
    blockers += [f"document {a.get('id')} is attached to it" for a in data.get("artifacts") or [] if a.get("attached_to") == eid]
    blockers += [f"an interface ({i.get('from')} → {i.get('to')}) uses it" for i in data.get("interfaces") or []
                 if eid in (i.get("from"), i.get("to"))]
    if blockers:
        raise BaselineEditError(f"{eid} was not removed: " + "; ".join(blockers) + ". Remove or move those first.")
    lines = before.split("\n")
    start, _ = _locate(lines, eid, data)
    end = _block_end(lines, start)
    taken_out = lines[start:end]
    del lines[start:end]
    siblings = [c for c in by_id.values() if c["parent"] == n["parent"] and c["class"] == n["class"] and c["id"] != eid]
    if n["class"] == "Vehicle":                          # the first vehicle is not in the `vehicles:` list
        siblings = [v for v in data.get("vehicles") or [] if v.get("id") != eid]
    if not siblings:                                     # an empty `key:` would read as null: say it is an empty list
        key = "vehicles" if n["class"] == "Vehicle" else PARENT[n["class"]][1]
        i = start - 1
        while i >= 0 and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
            i -= 1
        if i >= 0 and re.fullmatch(rf"\s*{key}:\s*(#.*)?", lines[i]):
            if n["class"] == "Vehicle":                  # no further vehicles left: the section this command wrote goes too
                first = i
                if first > 0 and lines[first - 1] == _VEHICLES_NOTE:
                    first -= 1
                    if first > 0 and not lines[first - 1].strip():
                        first -= 1
                taken_out = lines[first:i + 1] + taken_out
                del lines[first:i + 1]
            else:
                lines[i] = lines[i].split("#")[0].rstrip() + " []"
    _commit(path, before, "\n".join(lines), onto, dry_run, [], gone=eid)
    return {"id": eid, "class": n["class"], "name": n["name"], "parent": n["parent"], "text": "\n".join(taken_out),
            "baseline": str(path), "written": not dry_run}


def standard(onto: Ontology | None = None) -> list[str]:
    """The standard's table as the ontology holds it, as lines of text."""
    std = (onto or default_ontology()).wbs_standard
    if not std:
        return ["The ontology has no `wbs_standard` section: WBS codes are only checked for nesting."]
    root = std["vehicle"]
    top = re.sub(r"\.0$", "", root)
    fam = f"{top}.{min(std['vehicle_elements'])}"
    lines = [std.get("reference", ""), "", f"{root:<10} Ground Vehicle System          add vehicle"]
    lines += [f"{top + '.' + str(n):<10} {name:<30} add family" for n, name in std["vehicle_elements"].items()]
    lines += [f"{fam}.{std['lead_variant']:<6} Lead Variant                   add variant (the first in a family)",
              f"{fam}.{std['lead_variant'] + 1}...n  Variant 2...n                  add variant", ""]
    lines += [f"{fam}.n.{n:<4} {name:<46} add system" for n, name in std["variant_elements"].items()]
    if std.get("software_release"):
        lines.append(f"{fam}.n.{std['software_release']:<4} {'Software Release 1...n':<46} add system")
    lines.append(f"{fam}.n.{std['other_from']:<4} {'Other Subsystems 1...n (Specify)':<46} add system (any other name: "
                 f"{std['other_from']}, {std['other_from'] + 1}, ...)")
    lines += ["", f"{top}.{std.get('first_common_element', 3)} onwards (Systems Engineering, Program Management, Test, "
                  "Training, Data, ...) are common elements, not parts of the vehicle: they are not in the baseline."]
    return lines
