"""
Change the ontology from the command line, without editing YAML.

    python -m crew.main ontology show
    python -m crew.main ontology add value os_class hypervisor "Separation kernel hosting several partitions"
    python -m crew.main ontology add attribute Subsystem supplier --type text --description "Who builds it"
    python -m crew.main ontology add document-type sss "System/Subsystem Specification" --attaches-to Vehicle System
    python -m crew.main ontology add rule --when software has_interfaces --require icd --why "..."
    python -m crew.main ontology add word software bootloader
    python -m crew.main ontology set attribute Subsystem os_class --required yes
    python -m crew.main ontology remove value os_class hypervisor
    (crew/ontology_admin.py is the command line; this module has no CrewAI and no database)

The ontology (data/ontology/esam_ontology.yaml) is the SE team's file and is
mostly comment. A change is made to its TEXT: the one line or block
concerned is added, rewritten or taken out; every other line and every
comment stays, so `git diff` shows exactly the change.

What can be changed:

    value            a value in a value set (--new-set starts a set)
    attribute        an attribute of a class: its type, value set, pattern,
                     limits, whether it is required
    document-type    a kind of document, with the words that point to it and
                     what it may be attached to. Its code is added to the
                     form of a document id (ICD|SRS|...).
    rule             a required-documents rule: when ... require ...
    word             a word that marks the subject of a "shall" as software
                     or as system (requirement_classification.subjects)
    answer, synonym  the words in a question that mean a document type

Not from here: classes and relationships (ESAM's code knows the ones there
are), the WBS table, the id forms. Those are still edited in the file.

Before the file is written the result is checked. It must be valid YAML, the
change must be where it was meant to be, the ontology's own check must find
nothing new, and the BASELINE must not gain an error under the changed
ontology (making an attribute required that entries lack, removing a value
entries use). Otherwise nothing is written and the reason is given; --force
writes a change that breaks the baseline, for when the baseline is fixed
next. The version in `metadata` goes up by one step with each change.

`undo` takes the last change back (and the version with it), and again for
the one before: every change these commands write is remembered
(esamkit/edit_history.py). `history` lists them.
"""

import json
import os
import re
from pathlib import Path

import yaml

from . import edit_history
from .diagram_baseline import _block_end, _indent
from .ontology import Ontology, check_baseline

TYPES = ("text", "integer", "boolean", "enum", "text_list")
CONDITIONS = ("software", "has_interfaces", "safety_critical")
_SPEC_ORDER = ("type", "values", "class", "pattern", "min", "max", "required", "description")
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}


class OntologyEditError(ValueError):
    """Nothing was written. The message says why and what to give instead."""


# ── Text helpers ─────────────────────────────────────────────────────────────

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_/-]*( [A-Za-z0-9_/-]+)*", text) and text.lower() not in _RESERVED:
        return text
    if "\\" in text:                                    # a pattern: single quotes keep backslashes as they are
        return "'" + text.replace("'", "''") + "'"
    return json.dumps(text, ensure_ascii=False)


def _quoted(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _flow(value) -> str:
    if isinstance(value, dict):
        style = {"description": _quoted, "pattern": lambda v: "'" + str(v).replace("'", "''") + "'"}
        return "{ " + ", ".join(f"{k}: {style.get(k, _flow)(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return _scalar(value)


def _find(lines: list[str], path: list[str]) -> int | None:
    """The line of the key at `path` (keys written one per line, block style), or None."""
    lo, hi, at = 0, len(lines), None
    for depth, key in enumerate(path):
        indent = None
        for i in range(lo, hi):
            ln = lines[i]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if indent is None:
                indent = 0 if depth == 0 else _indent(ln)
            if _indent(ln) == indent and re.match(rf"\s*(['\"]?){re.escape(str(key))}\1:(\s|$)", ln):
                at = i
                break
        else:
            return None
        lo, hi = at + 1, _block_end(lines, at)
    return at


def _need(lines: list[str], path: list[str]) -> int:
    at = _find(lines, path)
    if at is None:
        raise OntologyEditError(f"could not find `{'.'.join(map(str, path))}` in the ontology file, written one key per line")
    return at


def _child_indent(lines: list[str], at: int, default: int = 2) -> int:
    for i in range(at + 1, _block_end(lines, at)):
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            return _indent(lines[i])
    return _indent(lines[at]) + default


def _split_comment(line: str) -> tuple[str, str]:
    """(the line without its trailing comment, the comment with its spacing)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip(), "  " + line[i:]
    return line.rstrip(), ""


def _flow_list(lines: list[str], at: int) -> tuple[int, list, bool]:
    """A list written in brackets starting on line `at` (it may run over
    several lines): (the line after it, its items, whether items are quoted)."""
    text, end = "", at
    while end < len(lines):
        text += _split_comment(lines[end])[0] + "\n"
        end += 1
        if text.count("[") and text.count("[") == text.count("]"):
            break
    body = text.split(":", 1)[1] if ":" in text else ""
    if not body.strip().startswith("["):
        raise OntologyEditError(f"the list at line {at + 1} of the ontology file is not written in [brackets]")
    try:
        items = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise OntologyEditError(f"the list at line {at + 1} of the ontology file can't be read ({e})") from e
    return end, list(items or []), '"' in body


def _write_list(lines: list[str], at: int, end: int, items: list, quoted: bool, width: int = 118) -> list[str]:
    head = _split_comment(lines[at])[0].split(":", 1)[0] + ": ["
    comment = _split_comment(lines[end - 1])[1]
    out, cur = [], head
    for n, item in enumerate(items):
        piece = (_quoted(item) if quoted else _scalar(item)) + ("," if n < len(items) - 1 else "]")
        if cur.strip() and cur != head and len(cur) + 1 + len(piece) > width:
            out.append(cur)
            cur = " " * len(head) + piece
        else:
            cur += ("" if cur.endswith("[") or cur.endswith(" " * len(head)) else " ") + piece
    if not items:
        cur += "]"
    out.append(cur + comment)
    lines[at:end] = out
    return out


# ── The check and the write ──────────────────────────────────────────────────

def _problems(onto: Ontology) -> set[str]:
    return {f"{f.where}: {f.message}" for f in onto.check_self() if f.level == "error"}


def _baseline_errors(onto: Ontology, baseline_yaml: Path | None) -> set[str]:
    if not baseline_yaml or not Path(baseline_yaml).is_file():
        return set()
    data = yaml.safe_load(Path(baseline_yaml).read_text())
    return {f"{f.where}: {f.message}" for f in check_baseline(onto, data) if f.level == "error"}


def _baseline_findings(onto: Ontology, baseline_yaml: Path | None) -> set[str]:
    """Errors and warnings alike."""
    if not baseline_yaml or not Path(baseline_yaml).is_file():
        return set()
    return {f"{f.where}: {f.message}" for f in check_baseline(onto, yaml.safe_load(Path(baseline_yaml).read_text()))}


def _bump(lines: list[str]) -> str | None:
    at = _find(lines, ["metadata", "version"])
    if at is None:
        return None
    body, comment = _split_comment(lines[at])
    m = re.fullmatch(r"(\s*version:\s*)(['\"]?)(.*?)(\d+)\2", body)
    if not m:
        return None
    new = f"{m[3]}{int(m[4]) + 1}"
    lines[at] = f'{m[1]}"{new}"{comment}'
    return new


def _commit(path: Path, before: str, lines: list[str], expect, baseline_yaml: Path | None, dry_run: bool,
            force: bool, keep_version: bool, result: dict) -> dict:
    """Check the new text and write it. `expect(data)` returns a message when the change did not land."""
    version = None if keep_version else _bump(lines)
    after = "\n".join(lines)
    try:
        data = yaml.safe_load(after)
        onto = Ontology(data)
    except Exception as e:
        raise OntologyEditError(f"the result would not be a valid ontology ({e}); nothing was written") from e
    missing = expect(data)
    if missing:
        raise OntologyEditError(f"{missing} (the file is laid out in a way this command does not follow); nothing was "
                                "written. Make the change by hand.")
    old = Ontology(yaml.safe_load(before))
    new = _problems(onto) - _problems(old)
    if new:
        raise OntologyEditError("not written: the ontology would no longer be consistent.\n  " + "\n  ".join(sorted(new)))
    try:
        broken = _baseline_errors(onto, baseline_yaml) - _baseline_errors(old, baseline_yaml)
    except Exception as e:
        raise OntologyEditError(f"not written: the baseline could not be checked against the result ({e})") from e
    result.update(ontology=str(path), written=not dry_run, version=version, baseline_errors=sorted(broken), undo=False)
    if broken and not force:
        raise OntologyEditError("not written: the baseline would no longer pass the ontology check.\n  "
                                + "\n  ".join(sorted(broken))
                                + "\nChange the baseline first, or add --force to write the ontology and fix the baseline next.")
    if not dry_run:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(after)
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise OntologyEditError(f"NOT written: {path} could not be changed ({e.strerror or e}). Check that the file "
                                    "and its folder can be written by this user") from e
        if path.read_text() != after:                    # read back: never report a write that is not on disk
            raise OntologyEditError(f"NOT written: {path} does not hold the change after writing it")
        result["undo"] = edit_history.record(path, before, after, result["change"]) is not None
    return result


class Edit:
    """One change to an ontology file. Each method returns
    {"change": one line saying what, "text": the lines written or removed,
     "notes": [...], "ontology": path, "written": bool, "version": new version or None,
     "baseline_errors": [...]} or raises OntologyEditError with the file untouched."""

    def __init__(self, ontology_yaml: Path, baseline_yaml: Path | None = None, dry_run: bool = False,
                 force: bool = False, keep_version: bool = False):
        self.path, self.baseline = Path(ontology_yaml), baseline_yaml
        self.opts = {"dry_run": dry_run, "force": force, "keep_version": keep_version}
        self.before = self.path.read_text()
        self.data = yaml.safe_load(self.before)
        self.onto = Ontology(self.data)
        self.lines = self.before.split("\n")

    def _done(self, change: str, text: list[str], expect, notes: list[str] | None = None) -> dict:
        return _commit(self.path, self.before, self.lines, expect, self.baseline, result={
            "change": change, "text": "\n".join(text), "notes": notes or []}, **self.opts)

    def _class(self, cls: str) -> dict:
        if cls not in self.onto.classes:
            raise OntologyEditError(f"there is no class {cls!r}. The classes: {', '.join(self.onto.classes)}")
        return self.onto.classes[cls]

    def _doc_type(self, code: str) -> dict:
        if code not in self.onto.document_types:
            raise OntologyEditError(f"there is no document type {code!r}. The types: {', '.join(self.onto.document_types)}")
        return self.onto.document_types[code]

    # ── value sets ──

    def add_value(self, value_set: str, value: str, meaning: str, new_set: bool = False) -> dict:
        value, meaning = str(value).strip(), " ".join(str(meaning or "").split())
        if value_set == "document_types":
            raise OntologyEditError("document types are added with: ontology add document-type CODE \"Name\"")
        if not value or not meaning:
            raise OntologyEditError("give the value and, in quotes, what it means")
        sets = self.onto.value_sets
        if value_set in sets:
            if new_set:
                raise OntologyEditError(f"the value set {value_set} is already there: leave --new-set out")
            if value in {str(k) for k in sets[value_set]}:
                raise OntologyEditError(f"{value_set} already has the value {value!r}")
            at = _need(self.lines, ["value_sets", value_set])
            new = [" " * _child_indent(self.lines, at) + f"{_scalar(value)}: {_quoted(meaning)}"]
            end = _block_end(self.lines, at)
        else:
            if not new_set:
                raise OntologyEditError(f"there is no value set {value_set!r}. The sets: {', '.join(sets)}. "
                                        "To start a new one add --new-set")
            if not _NAME.fullmatch(value_set):
                raise OntologyEditError(f"a value set's name is lower-case words joined by _ , not {value_set!r}")
            at = _need(self.lines, ["value_sets"])
            pad = _child_indent(self.lines, at)
            new = [" " * pad + f"{value_set}:", " " * (pad + 2) + f"{_scalar(value)}: {_quoted(meaning)}"]
            end = _block_end(self.lines, at)
        self.lines[end:end] = new
        return self._done(f"value {value!r} added to {value_set}" + (" (a new value set)" if new_set else ""), new,
                          lambda d: None if str(value) in {str(k) for k in (d["value_sets"].get(value_set) or {})}
                          else f"{value} would not land in value_sets.{value_set}",
                          [f"no attribute uses the value set {value_set} yet: ontology add attribute CLASS NAME --type enum "
                           f"--values {value_set}"] if new_set else [])

    def remove_value(self, value_set: str, value: str) -> dict:
        sets = self.onto.value_sets
        if value_set not in sets or str(value) not in {str(k) for k in sets[value_set]}:
            raise OntologyEditError(f"{value_set} has no value {value!r}" if value_set in sets
                                    else f"there is no value set {value_set!r}. The sets: {', '.join(sets)}")
        if len(sets[value_set]) == 1:
            raise OntologyEditError(f"{value!r} is the last value of {value_set}; a value set cannot be left empty")
        at = _need(self.lines, ["value_sets", value_set, value])
        gone = self.lines[at:_block_end(self.lines, at)]
        del self.lines[at:at + len(gone)]
        return self._done(f"value {value!r} removed from {value_set}", gone,
                          lambda d: f"{value} is still in value_sets.{value_set}"
                          if str(value) in {str(k) for k in d["value_sets"][value_set]} else None,
                          ["requirements and test cases already in the graph are not checked here: reload the graph and "
                           "run the health check"])

    # ── attributes ──

    def _spec(self, cls: str, name: str, given: dict, base: dict | None = None) -> dict:
        spec = dict(base or {})
        for key, value in given.items():
            if value is None:
                continue
            if value == "" or value is False and key != "required":
                spec.pop(key, None)
            else:
                spec[key] = value
        if spec.get("required") is False:
            spec.pop("required")
        kind = spec.get("type")
        if base is None and kind not in TYPES:
            raise OntologyEditError(f"an attribute's type is one of: {', '.join(TYPES)} (not {kind!r})")
        if "type" in given and given["type"] is not None and kind not in TYPES:
            raise OntologyEditError(f"an attribute's type is one of: {', '.join(TYPES)} (not {kind!r})")
        if kind == "enum" and not spec.get("values"):
            raise OntologyEditError("an enum attribute takes its values from a value set: add --values SET "
                                    f"({', '.join(self.onto.value_sets)})")
        if spec.get("values") and spec["values"] != "document_types" and spec["values"] not in self.onto.value_sets:
            raise OntologyEditError(f"there is no value set {spec['values']!r}. The sets: {', '.join(self.onto.value_sets)}. "
                                    f"Start one with: ontology add value {spec['values']} VALUE \"meaning\" --new-set")
        if spec.get("values") and kind not in ("enum", "text_list"):
            raise OntologyEditError(f"--values goes with --type enum or text_list, not {kind}")
        if spec.get("pattern"):
            if kind != "text":
                raise OntologyEditError(f"--pattern goes with --type text, not {kind}")
            try:
                re.compile(spec["pattern"])
            except re.error as e:
                raise OntologyEditError(f"the pattern is not a regular expression ({e})") from e
        for limit in ("min", "max"):
            if limit in spec:
                if kind != "integer":
                    raise OntologyEditError(f"--{limit} goes with --type integer, not {kind}")
                try:
                    spec[limit] = int(spec[limit])
                except (TypeError, ValueError):
                    raise OntologyEditError(f"--{limit} is a whole number, not {spec[limit]!r}") from None
        if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
            raise OntologyEditError(f"--min {spec['min']} is above --max {spec['max']}")
        if spec.get("description"):
            spec["description"] = " ".join(str(spec["description"]).split())
        return {k: spec[k] for k in (*_SPEC_ORDER, *[k for k in spec if k not in _SPEC_ORDER]) if k in spec}

    def add_attribute(self, cls: str, name: str, **given) -> dict:
        c = self._class(cls)
        if not _NAME.fullmatch(name or ""):
            raise OntologyEditError(f"an attribute's name is lower-case words joined by _ , not {name!r}")
        if name in ("id", "name") or name in (c.get("attributes") or {}):
            raise OntologyEditError(f"{cls} already has {name!r}" + ("" if name in ("id", "name") else
                                    f": change it with  ontology set attribute {cls} {name} ..."))
        given.setdefault("type", "text")
        if given.get("type") is None:
            given["type"] = "enum" if given.get("values") else "text"
        spec = self._spec(cls, name, given)
        top = _need(self.lines, ["classes", cls])
        at = _find(self.lines, ["classes", cls, "attributes"])
        if at is None:
            pad = _child_indent(self.lines, top)
            new = [" " * pad + "attributes:", " " * (pad + 2) + f"{name}: {_flow(spec)}"]
            end = _block_end(self.lines, top)
        else:
            new = [" " * _child_indent(self.lines, at) + f"{name}: {_flow(spec)}"]
            end = _block_end(self.lines, at)
        self.lines[end:end] = new
        notes = []
        if c.get("source") == "document":
            notes.append(f"a {cls} is read out of documents, not typed in the baseline: ESAM's reader "
                         "(esamkit/traceability.py) has to be taught to fill this attribute")
        elif cls in ("Vehicle", "VehicleFamily", "Variant"):
            notes.append(f"the graph loader copies new attributes for systems, subsystems and functions; a {cls}'s is "
                         "checked in the baseline but not loaded into the graph")
        else:
            notes.append(f"set it on an entry with:  baseline add {cls.lower()} \"...\" --set {name}=VALUE  (or type it in "
                         "the baseline); then graph reload")
        return self._done(f"attribute {name} added to {cls}", new,
                          lambda d: None if (d["classes"][cls].get("attributes") or {}).get(name) == spec
                          else f"{name} would not land in classes.{cls}.attributes", notes)

    def set_attribute(self, cls: str, name: str, **given) -> dict:
        c = self._class(cls)
        old = (c.get("attributes") or {}).get(name)
        if old is None:
            raise OntologyEditError(f"{cls} has no attribute {name!r}. It has: {', '.join(c.get('attributes') or {}) or 'none'}")
        spec = self._spec(cls, name, given, base=old)
        if spec == old:
            raise OntologyEditError(f"{cls}.{name} is already like that: {_flow(old)}")
        at = _need(self.lines, ["classes", cls, "attributes", name])
        body, comment = _split_comment(self.lines[at])
        if "{" not in body or _block_end(self.lines, at) != at + 1:
            raise OntologyEditError(f"{cls}.{name} is written over several lines in the ontology file; change it by hand")
        self.lines[at] = " " * _indent(self.lines[at]) + f"{name}: {_flow(spec)}" + comment
        return self._done(f"attribute {cls}.{name} changed (was: {_flow(old)})", [self.lines[at]],
                          lambda d: None if d["classes"][cls]["attributes"].get(name) == spec
                          else f"{cls}.{name} would not read back as written")

    def remove_attribute(self, cls: str, name: str) -> dict:
        c = self._class(cls)
        if name not in (c.get("attributes") or {}):
            raise OntologyEditError(f"{cls} has no attribute {name!r}. It has: {', '.join(c.get('attributes') or {}) or 'none'}")
        used = self._baseline_use(cls, name)
        if used and not self.opts["force"]:
            raise OntologyEditError(f"{cls}.{name} is set on {', '.join(used[:8])}{' ...' if len(used) > 8 else ''} in the "
                                    "baseline. Take it off those entries first (or --force)")
        at = _need(self.lines, ["classes", cls, "attributes", name])
        gone = self.lines[at:_block_end(self.lines, at)]
        del self.lines[at:at + len(gone)]
        return self._done(f"attribute {name} removed from {cls}", gone,
                          lambda d: f"{name} is still in classes.{cls}.attributes"
                          if name in (d["classes"][cls].get("attributes") or {}) else None,
                          ["ESAM's own code reads some attributes by name (safety_critical, os_class, wbs_code, aliases ...): "
                           "removing one of those changes what the pipelines can tell"] if not c.get("source") else [])

    def _baseline_use(self, cls: str, name: str) -> list[str]:
        if not self.baseline or not Path(self.baseline).is_file():
            return []
        from .baseline_edit import nodes
        data = yaml.safe_load(Path(self.baseline).read_text())
        out = [n["id"] for n in nodes(data) if n["class"] == cls and name in n["node"]]
        if cls == "DesignArtifact":
            out += [a.get("id", "?") for a in data.get("artifacts") or [] if name in a]
        return out

    # ── document types ──

    def add_document_type(self, code: str, name: str, standard: str | None = None, answers: list[str] | None = None,
                          synonyms: list[str] | None = None, attaches_to: list[str] | None = None) -> dict:
        code, name = str(code or "").strip().lower(), " ".join(str(name or "").split())
        if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", code):
            raise OntologyEditError(f"a document type's code is 2 to 8 letters or digits, like srs or icd (not {code!r})")
        if code in self.onto.document_types:
            raise OntologyEditError(f"the document type {code} is already there ({self.onto.document_types[code].get('name')})")
        if not name:
            raise OntologyEditError("give the document type's full name in quotes")
        owners = self.onto.relationships.get("HAS_ARTIFACT", {}).get("from", [])
        for cls in attaches_to or []:
            if cls not in owners:
                raise OntologyEditError(f"a document cannot be attached to a {cls}: the choices are {', '.join(owners)}")
        at = _need(self.lines, ["document_types"])
        pad = _child_indent(self.lines, at)
        inner = " " * (pad + 2)
        new = [" " * pad + f"{code}:", inner + f"name: {_quoted(name)}"]
        if standard:
            new.append(inner + f"standard: {_quoted(' '.join(standard.split()))}")
        if attaches_to:
            new.append(inner + f"attaches_to: [{', '.join(attaches_to)}]")
        new.append(inner + f"answers: [{', '.join(_scalar(a) for a in answers or [name.lower()])}]")
        new.append(inner + f"synonyms: [{', '.join(_quoted(x) for x in synonyms or [])}]")
        end = _block_end(self.lines, at)
        self.lines[end:end] = new
        notes = self._id_form(code, add=True)
        notes.append(f"nothing requires a {code.upper()} yet: ontology add rule --when ... --require {code} --why \"...\"")
        return self._done(f"document type {code} ({name}) added", new,
                          lambda d: None if (d["document_types"].get(code) or {}).get("name") == name
                          else f"{code} would not land in document_types", notes)

    def remove_document_type(self, code: str) -> dict:
        self._doc_type(code)
        blockers = []
        if self.baseline and Path(self.baseline).is_file():
            arts = yaml.safe_load(Path(self.baseline).read_text()).get("artifacts") or []
            blockers += [f"document {a.get('id')} is one" for a in arts if str(a.get("type", "")).lower() == code]
        blockers += [f"rule {i} requires it" for i, r in enumerate(self.onto.rules.get("required_documents", []), 1)
                     if code in r.get("require", [])]
        if blockers:
            raise OntologyEditError(f"the document type {code} was not removed: " + "; ".join(blockers))
        at = _need(self.lines, ["document_types", code])
        gone = self.lines[at:_block_end(self.lines, at)]
        del self.lines[at:at + len(gone)]
        notes = self._id_form(code, add=False)
        return self._done(f"document type {code} removed", gone,
                          lambda d: f"{code} is still in document_types" if code in d["document_types"] else None, notes)

    def _id_form(self, code: str, add: bool) -> list[str]:
        """Keep the form of a document id (ICD|SRS|...) in step with the document types."""
        at = _find(self.lines, ["classes", "DesignArtifact", "id_pattern"])
        m = re.search(r"\^\(([A-Z0-9|]+)\)-", self.lines[at]) if at is not None else None
        if not m:
            return [f"the form of a document id (classes.DesignArtifact.id_pattern) could not be brought in step: "
                    f"{'add' if add else 'remove'} {code.upper()} there by hand"]
        codes = m[1].split("|")
        if add and code.upper() not in codes:
            codes.append(code.upper())
        if not add and code.upper() in codes:
            codes.remove(code.upper())
        self.lines[at] = self.lines[at][:m.start(1)] + "|".join(codes) + self.lines[at][m.end(1):]
        return [f"document ids may {'now' if add else 'no longer'} start with {code.upper()}- "
                f"(classes.DesignArtifact.id_pattern: {'|'.join(codes)})"]

    # ── rules ──

    def add_rule(self, when: list[str], require: list[str], why: str) -> dict:
        when, require = list(dict.fromkeys(when or [])), [str(t).lower() for t in dict.fromkeys(require or [])]
        why = " ".join(str(why or "").split())
        if not when or not require or not why:
            raise OntologyEditError("a rule needs --when (one or more of: " + ", ".join(CONDITIONS)
                                    + "), --require (document types) and --why \"the reason\"")
        for cond in when:
            if cond not in CONDITIONS:
                raise OntologyEditError(f"{cond!r} is not a condition. The conditions: {', '.join(CONDITIONS)}")
        for t in require:
            self._doc_type(t)
        rules = self.onto.rules.get("required_documents", [])
        for i, r in enumerate(rules, 1):
            if sorted(r.get("when", [])) == sorted(when) and set(require) <= set(r.get("require", [])):
                raise OntologyEditError(f"rule {i} already says that: when {', '.join(r['when'])} require {', '.join(r['require'])}")
        at = _need(self.lines, ["rules", "required_documents"])
        pad = " " * _child_indent(self.lines, at)
        new = [pad + f"- when: [{', '.join(when)}]", pad + f"  require: [{', '.join(require)}]", pad + f"  why: {_quoted(why)}"]
        end = _block_end(self.lines, at)
        self.lines[end:end] = new
        want = {"when": when, "require": require, "why": why}
        return self._done(f"rule {len(rules) + 1} added: when {', '.join(when)} require {', '.join(require)}", new,
                          lambda d: None if d["rules"]["required_documents"][-1] == want else "the rule would not land in rules",
                          ["subsystems the rule covers now show these documents as missing in the coverage report"])

    def remove_rule(self, number: int) -> dict:
        rules = self.onto.rules.get("required_documents", [])
        if not 1 <= number <= len(rules):
            raise OntologyEditError(f"there are {len(rules)} rules; give a number from 1 to {len(rules)} (ontology show rules)")
        at = _need(self.lines, ["rules", "required_documents"])
        starts = [i for i in range(at + 1, _block_end(self.lines, at)) if re.match(r"\s*- ", self.lines[i])]
        if len(starts) != len(rules):
            raise OntologyEditError("the rules are not written one `- when:` entry each in the ontology file; change it by hand")
        start = starts[number - 1]
        gone = self.lines[start:_block_end(self.lines, start)]
        del self.lines[start:start + len(gone)]
        rest = rules[:number - 1] + rules[number:]
        return self._done(f"rule {number} removed (when {', '.join(rules[number - 1].get('when', []))} require "
                          f"{', '.join(rules[number - 1].get('require', []))})", gone,
                          lambda d: None if (d["rules"].get("required_documents") or []) == rest else "the rule is still there")

    # ── word lists ──

    def _words(self, path: list[str], label: str, words: list[str], add: bool, lower: bool = True) -> dict:
        words = [" ".join(str(w).split()) for w in words or []]
        words = [w.lower() if lower else w for w in words if w]
        if not words:
            raise OntologyEditError("give at least one word")
        at = _find(self.lines, path)
        if at is None and add:                                   # the key is not there yet: start the list
            parent = _need(self.lines, path[:-1])
            end = _block_end(self.lines, parent)
            self.lines[end:end] = [" " * _child_indent(self.lines, parent) + f"{path[-1]}: []"]
            at = end
        elif at is None:
            raise OntologyEditError(f"{label} has no words to remove")
        end, items, quoted = _flow_list(self.lines, at)
        have = {str(i).lower() for i in items}
        if add:
            fresh = [w for w in dict.fromkeys(words) if w.lower() not in have]
            if not fresh:
                raise OntologyEditError(f"{label} already has: {', '.join(words)}")
            items, changed = items + fresh, fresh
        else:
            absent = [w for w in words if w.lower() not in have]
            if absent:
                raise OntologyEditError(f"{label} does not have: {', '.join(absent)}. It has: {', '.join(map(str, items))}")
            drop = {w.lower() for w in words}
            items, changed = [i for i in items if str(i).lower() not in drop], words
        new = _write_list(self.lines, at, end, items, quoted or path[-1] == "synonyms")

        def expect(d):
            node = d
            for key in path:
                node = (node or {}).get(key)
            return None if [str(x) for x in node or []] == [str(x) for x in items] else f"{label} would not read back as written"
        return self._done(f"{label}: {'added' if add else 'removed'} {', '.join(changed)}", new, expect)

    def word(self, level: str, words: list[str], add: bool = True) -> dict:
        levels = list(self.onto.requirement_classification()["subjects"]) or list(self.onto.value_sets.get("requirement_level", {}))
        if level not in levels:
            raise OntologyEditError(f"the levels are: {', '.join(levels)} (not {level!r})")
        res = self._words(["requirement_classification", "subjects", level], f"words that mark a {level} requirement", words, add)
        other = {w for lv, ws in self.onto.requirement_classification()["subjects"].items() if lv != level for w in ws}
        clash = [w for w in words if w.lower() in other] if add else []
        if clash:
            res["notes"].append(f"{', '.join(clash)} also marks another level; a subject with a software word is read as software")
        return res

    def answer(self, code: str, words: list[str], add: bool = True) -> dict:
        self._doc_type(code)
        return self._words(["document_types", code, "answers"], f"words in a question that point to the {code.upper()}", words, add)

    def synonym(self, code: str, words: list[str], add: bool = True) -> dict:
        self._doc_type(code)
        return self._words(["document_types", code, "synonyms"], f"other names for the {code.upper()}", words, add, lower=False)


# ── Showing ──────────────────────────────────────────────────────────────────

SECTIONS = ("values", "classes", "documents", "rules", "words")


def show(onto: Ontology, what: str | None = None) -> list[str]:
    """The ontology as lines of text: all of it, one section, one class or one value set."""
    meta = onto.data.get("metadata", {})
    out: list[str] = []

    def spec_text(spec: dict) -> str:
        bits = [spec.get("type", "")]
        if spec.get("values"):
            bits.append(f"one of {spec['values']}" if spec.get("type") != "text_list" else f"from {spec['values']}")
        if spec.get("class"):
            bits.append(f"a {spec['class']}")
        if spec.get("pattern"):
            bits.append(f"pattern {spec['pattern']}")
        if "min" in spec or "max" in spec:
            bits.append(f"{spec.get('min', '')}..{spec.get('max', '')}")
        if spec.get("required"):
            bits.append("REQUIRED")
        return ", ".join(b for b in bits if b) + (f"  — {spec['description']}" if spec.get("description") else "")

    def values(names):
        for name in names:
            out.append(f"{name}:")
            out.extend(f"  {str(k):<18} {v}" for k, v in onto.value_sets[name].items())

    def classes(names):
        for name in names:
            c = onto.classes[name]
            out.append(f"{name}  (id {c.get('id_pattern', '?')}" + (f"; part of {', '.join(c['part_of'])}" if c.get("part_of") else "")
                       + ("; read from documents" if c.get("source") == "document" else "") + ")")
            out.extend(f"  {a:<24} {spec_text(spec)}" for a, spec in (c.get("attributes") or {}).items())

    def documents():
        for code, t in onto.document_types.items():
            out.append(f"{code:<6} {t.get('name', '')}  (attached to {', '.join(onto.attach_classes(code))})")
            out.append(f"       answers : {', '.join(map(str, t.get('answers', [])))}")
            out.append(f"       synonyms: {', '.join(map(str, t.get('synonyms', [])))}")

    def rules():
        for i, r in enumerate(onto.rules.get("required_documents", []), 1):
            out.append(f"{i}. when {', '.join(r.get('when', []))}: require {', '.join(r.get('require', []))}  — {r.get('why', '')}")

    def words():
        rc = onto.requirement_classification()
        out.append(f"ids: " + ", ".join(f"{m}-SRS-... is {lv}" for lv, m in rc["id_marker"].items())
                   + f"; an unmarked SRS-... is {rc['unmarked']}")
        for level, ws in rc["subjects"].items():
            out.append(f"{level:<9} {', '.join(ws)}")

    if what in onto.classes:
        classes([what])
    elif what in onto.value_sets:
        values([what])
    elif what in (None, *SECTIONS):
        if what is None:
            out.append(f"{meta.get('name', 'Ontology')} v{meta.get('version', '?')}")
        for section, title, fn in (("values", "Value sets", lambda: values(onto.value_sets)),
                                   ("classes", "Classes and their attributes", lambda: classes(onto.classes)),
                                   ("documents", "Document types", documents),
                                   ("rules", "Required-document rules", rules),
                                   ("words", "Words that tell a system requirement from a software requirement", words)):
            if what in (None, section):
                out += ["", f"── {title} ──"] if what is None else []
                fn()
    else:
        raise OntologyEditError(f"show takes a section ({', '.join(SECTIONS)}), a class ({', '.join(onto.classes)}) or a "
                                f"value set ({', '.join(onto.value_sets)}), not {what!r}")
    return out


def undo(ontology_yaml: Path, baseline_yaml: Path | None = None, force: bool = False, dry_run: bool = False) -> dict:
    """Put the ontology back as it was before the last change these commands wrote.
    Refused (OntologyEditError) when the file was edited since, or when the baseline
    would fail the earlier ontology (an entry uses what the change added); `force` restores it anyway."""
    path = Path(ontology_yaml)

    def check(text: str) -> list[str]:
        try:
            earlier, now = Ontology(yaml.safe_load(text)), Ontology(yaml.safe_load(path.read_text()))
        except Exception as e:
            return [f"not a valid ontology ({e})"]
        # warnings count here too: an entry that has an attribute the earlier ontology does not know is one
        return sorted((_problems(earlier) - _problems(now))
                      | (_baseline_findings(earlier, baseline_yaml) - _baseline_findings(now, baseline_yaml)))
    try:
        return edit_history.undo(path, force=force, dry_run=dry_run, check=check)
    except edit_history.HistoryError as e:
        raise OntologyEditError(str(e)) from e


def history(ontology_yaml: Path) -> list[dict]:
    """The changes that can be undone, newest first: [{"when", "change"}]."""
    return [{"when": e["when"], "change": e["change"]} for e in edit_history.entries(Path(ontology_yaml))]
