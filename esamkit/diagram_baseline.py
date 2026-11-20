"""
Systems and subsystems read from a picture diagram into the vehicle baseline.

A block diagram or an architecture drawing shows structure: which systems
the vehicle has and which subsystems sit in each. With this option on, a
picture that is ingested (a picture file, or one inside a Word document) is
also ANALYSED for that structure, and what the baseline does not have yet is
proposed, or added, as systems and subsystems.

    ESAM_DIAGRAM_BASELINE=off       (default) pictures are only described
    ESAM_DIAGRAM_BASELINE=propose   list what the diagram adds; change nothing
    ESAM_DIAGRAM_BASELINE=apply     as propose, and on ingest add them to the
                                    baseline as `status: candidate` entries

    python -m crew.main review topology.png --set id=ICD-BRK-003 ... --diagram-baseline propose
    python -m esamkit.diagram_baseline topology.png            # the proposal for one picture
    python -m esamkit.diagram_baseline topology.png --apply    # and write it to the baseline

How it works, and what it will not do:

  analyse   One call to the vision model per picture (the same model and the
            same rules as a description: no ESAM_VISION_MODEL, or
            ESAM_DIAGRAMS=local/off, means no picture is sent and nothing
            is proposed). The model is given the systems and subsystems the
            baseline already has and returns the ones the picture shows.
  propose   Deterministic. A name the baseline already has is matched to it
            (by id, name or alias) and left alone. What is new gets an id
            in the ontology's form (SYS-…, SUB-…), a parent, and a list of
            what the diagram could not say.
  apply     Writes the new entries into the baseline YAML, each marked

                status: candidate
                derived_from: "<document> picture <n> (<file>)"

            so the health report lists them under "Awaiting SE review" until
            an SE confirms them. Nothing existing is changed or removed. The
            result must still load and pass the ontology check, or the file
            is put back as it was.

A diagram does not state everything the baseline needs, and ESAM does not
invent it: a new system gets a WBS code only if the picture shows one (the
ontology accepts a candidate without it, as a warning); a new subsystem is
written `safety_critical: true` unless the picture says otherwise, because
an unreviewed unit is treated as the more demanding case until an SE decides.
A subsystem the picture does not place in a system is listed, not added.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import diagrams as _diagrams, llm
from .baseline import vehicle_roots
from .config import Settings, settings as default_settings
from .docmodel import Picture
from .ontology import Ontology, check_baseline, default_ontology

MODES = ("off", "propose", "apply")
PROMPT_VERSION = "1"
SYSTEM = ("You read engineering diagrams for a systems-engineering baseline. You report only what the diagram shows. "
          "You answer with one JSON object and nothing else.")
PROMPT = """This picture is from a vehicle engineering document. Find the SYSTEMS it shows and the SUBSYSTEMS inside each.

A system is a major element of the vehicle (for example: Suspension/Steering, Vehicle Electronics, Power Package/Drive
Train). A subsystem is a unit inside a system with its own design: a control unit (ECU), a controller, a display, a
sensor or actuator assembly, a data bus. Signals, messages, wires, connectors, software tasks and functions are NOT
subsystems: leave them out.

The baseline already has these (id: name). Where the picture shows one of them, even under a slightly different name
or an abbreviation, return its id in "existing_id" and do not rename it:

{existing}

Answer with one JSON object in exactly this form:

{{"is_structure_diagram": true,
  "systems": [
    {{"name": "the system's name as written in the picture",
      "existing_id": "SYS-... or null",
      "wbs_code": "a WBS code printed next to it, e.g. 1.1.1.6, or null",
      "subsystems": [
        {{"name": "the subsystem's name as written",
          "existing_id": "SUB-... or null",
          "aliases": ["other names or abbreviations the picture uses for it"],
          "safety_critical": true, false, or null if the picture does not say,
          "evidence": "a few words: where in the picture it is"}}
      ]}}
  ],
  "unplaced_subsystems": [
    {{"name": "a subsystem drawn outside any system box, or whose system the picture does not show",
      "existing_id": "SUB-... or null", "aliases": [], "safety_critical": null, "evidence": "..."}}
  ]}}

Rules: copy names exactly as written, including abbreviations. Do not guess a system for a subsystem: if the picture
does not group it, put it in "unplaced_subsystems". Do not add anything the picture does not show. If the picture is
not a diagram of systems and subsystems (a photo, a chart, a state machine, a timing diagram), answer
{{"is_structure_diagram": false, "systems": [], "unplaced_subsystems": []}}."""
TEXT_ONLY = "\n\nThe diagram is a drawing made of shapes; only the text in its shapes is available:\n\n"
SVG_NOTE = "\n\nThe picture is given as SVG source:\n\n"


class DiagramBaselineError(RuntimeError):
    """The baseline was not changed; the message says why."""


def mode_of(s: Settings = default_settings, override: str | None = None) -> str:
    mode = (override or s.diagram_baseline or "off").strip().lower()
    if mode not in MODES:
        raise ValueError(f"diagram baseline mode {mode!r}: use off, propose or apply")
    return mode


# ── The baseline as it is ────────────────────────────────────────────────────

def existing_structure(data: dict) -> dict:
    """{"variant": id of the variant new systems go under, "systems": {id: {...}}, "subsystems": {id: {...}}}"""
    systems, subsystems, variants = {}, {}, []
    for fam in [f for veh in vehicle_roots(data) for f in veh.get("families") or []]:
        for var in fam.get("variants") or []:
            variants.append(var)
            for sysm in var.get("systems") or []:
                systems[sysm["id"]] = {"name": sysm.get("name", ""), "variant": var["id"], "wbs_code": sysm.get("wbs_code")}
                for sub in sysm.get("subsystems") or []:
                    subsystems[sub["id"]] = {"name": sub.get("name", ""), "system": sysm["id"],
                                             "aliases": list(sub.get("aliases") or [])}
    lead = next((v for v in variants if v.get("role") == "lead"), variants[0] if variants else None)
    return {"variant": lead["id"] if lead else None, "systems": systems, "subsystems": subsystems}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _listing(struct: dict) -> str:
    lines = []
    for sid, sysm in struct["systems"].items():
        lines.append(f"{sid}: {sysm['name']}")
        for uid, sub in struct["subsystems"].items():
            if sub["system"] == sid:
                also = f" (also called: {', '.join(sub['aliases'])})" if sub["aliases"] else ""
                lines.append(f"    {uid}: {sub['name']}{also}")
    return "\n".join(lines) or "(nothing yet)"


# ── Analyse: one model call per picture ──────────────────────────────────────

def _json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def analyse(pic: Picture, struct: dict, s: Settings = default_settings) -> dict:
    """What one picture shows: {"systems": [...], "unplaced_subsystems": [...],
    "is_structure_diagram", "model", "cached"} or {"skipped": why}. Never
    raises: a picture that cannot be analysed adds nothing."""
    if (s.diagrams or "auto").lower() != "auto":
        return {"skipped": f"ESAM_DIAGRAMS is {s.diagrams!r}: pictures are not sent to a model"}
    routes = llm.vision_routes(s)
    if not routes:
        return {"skipped": "no vision model is configured (set ESAM_VISION_MODEL)"}
    model, prompt = routes[0].model, PROMPT.format(existing=_listing(struct))
    mime = _diagrams.sniff(pic.data, pic.mime) if pic.data else ""
    key = hashlib.sha256(pic.data + "\n".join(pic.labels).encode() + prompt.encode()
                         + f"{model}|{PROMPT_VERSION}".encode()).hexdigest()[:32]
    cache = Path(s.diagram_cache_dir) / f"structure_{key}.json"
    if cache.exists():
        try:
            return {**json.loads(cache.read_text()), "cached": True}
        except (ValueError, OSError):
            pass
    try:
        if not pic.data:
            if not pic.labels:
                return {"skipped": "the drawing has no picture data and no text"}
            text = llm.complete(SYSTEM, prompt + TEXT_ONLY + "\n".join(f"- {x}" for x in pic.labels), routes=routes, s=s)
        elif mime == "image/svg+xml":
            source = pic.data.decode("utf-8", "replace")
            if len(source) > _diagrams.MAX_SVG_CHARS:
                return {"skipped": f"the SVG is larger than {_diagrams.MAX_SVG_CHARS} characters"}
            text = llm.complete(SYSTEM, prompt + SVG_NOTE + source, routes=routes, s=s)
        else:
            ready = _diagrams._for_vision(pic.data, mime, s)
            if ready is None:
                return {"skipped": f"a {mime or 'picture of unknown type'} cannot be sent to the model from this machine"}
            text = llm.complete(SYSTEM, prompt, images=[(ready[1], ready[0])], routes=routes, s=s)
        raw = _json_object(text)
    except Exception as e:
        return {"skipped": f"the vision model gave no usable answer ({type(e).__name__}: {str(e)[:200]})"}
    out = {"is_structure_diagram": bool(raw.get("is_structure_diagram", True)),
           "systems": [x for x in raw.get("systems") or [] if isinstance(x, dict)],
           "unplaced_subsystems": [x for x in raw.get("unplaced_subsystems") or [] if isinstance(x, dict)],
           "model": model, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    except OSError:
        pass
    return {**out, "cached": False}


# ── Propose: deterministic ───────────────────────────────────────────────────

_STOP = {"the", "of", "and", "a", "an", "for", "to", "in", "on", "system", "subsystem", "unit", "module"}


def make_id(prefix: str, name: str, taken: set[str], pattern: str) -> str:
    """An id in the ontology's form from a name: "Tire Pressure Monitor" ->
    SUB-TPM; an acronym in the name is used as it is ("Transmission Control
    Unit (TCU)" -> SUB-TCU). A number is added until it is free."""
    acronym = re.search(r"\(([A-Z][A-Z0-9]{1,7})\)", name) or re.fullmatch(r"\s*([A-Z][A-Z0-9]{1,7})\s*", name)
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in _STOP] or re.findall(r"[A-Za-z0-9]+", name)
    if acronym:
        code = acronym[1]
    elif len(words) == 1:
        code = words[0][:6]
    else:
        code = "".join(w if w.isupper() and len(w) <= 5 else w[0] for w in words)[:8]
    code = re.sub(r"[^A-Z0-9]", "", code.upper()) or "X"
    candidate, n = f"{prefix}-{code}", 1
    while candidate in taken or not re.match(pattern, candidate):
        n += 1
        candidate = f"{prefix}-{code}{n}"
        if n > 99:
            raise DiagramBaselineError(f"could not make an id for {name!r}")
    return candidate


def _valid_wbs(code) -> bool:
    return bool(code) and bool(re.fullmatch(r"\d+(\.\d+)+", str(code)))


def propose(analyses: list[dict], data: dict, onto: Ontology | None = None, document: str = "") -> dict:
    """The additions the analysed pictures call for.

    `analyses`: [{"n", "name", **analyse(...)}] for the document's pictures.
    Returns {"document", "variant", "additions": [...], "already_present":
    [...], "not_added": [...], "pictures": [...]}; an addition is
    {"class": "System" | "Subsystem", "id", "name", "parent", "from",
    "needs": [...], plus wbs_code / aliases / safety_critical when known}."""
    onto = onto or default_ontology()
    struct = existing_structure(data)
    sys_pat, sub_pat = onto.classes["System"]["id_pattern"], onto.classes["Subsystem"]["id_pattern"]
    taken = set(struct["systems"]) | set(struct["subsystems"])
    sys_by_name = {_norm(v["name"]): k for k, v in struct["systems"].items()}
    sub_by_name = {}
    for k, v in struct["subsystems"].items():
        for name in [v["name"], *v["aliases"], re.sub(r"\s*\(.*?\)", "", v["name"])]:
            sub_by_name.setdefault(_norm(name), k)
    used_wbs = {str(v["wbs_code"]) for v in struct["systems"].values() if v.get("wbs_code")}
    out = {"document": document, "variant": struct["variant"], "additions": [], "already_present": [], "not_added": [],
           "pictures": []}
    new_sys: dict[str, str] = {}          # normalised name -> id proposed in this run
    new_sub: dict[str, str] = {}

    def present(eid, seen_as, where):
        if not any(p["id"] == eid for p in out["already_present"]):
            out["already_present"].append({"id": eid, "seen_as": seen_as, "from": where})

    def subsystem(item, parent, where):
        name = str(item.get("name") or "").strip()
        if not name:
            return
        eid = item.get("existing_id") if item.get("existing_id") in struct["subsystems"] else sub_by_name.get(_norm(name))
        if eid:
            present(eid, name, where)
            if parent and parent in struct["systems"] and struct["subsystems"][eid]["system"] != parent:
                out["not_added"].append({"name": name, "from": where, "why": f"the baseline has it as {eid} under "
                                         f"{struct['subsystems'][eid]['system']}, the picture shows it under {parent}: "
                                         "not moved; an SE decides which is right"})
            return
        if _norm(name) in new_sub:
            return
        if not parent:
            out["not_added"].append({"name": name, "from": where, "why": "the picture does not place it in a system: "
                                     "add it to the baseline under the right system by hand"})
            return
        eid = make_id("SUB", name, taken, sub_pat)
        taken.add(eid)
        new_sub[_norm(name)] = eid
        add = {"class": "Subsystem", "id": eid, "name": name, "parent": parent, "from": where, "needs": []}
        aliases = [str(a).strip() for a in item.get("aliases") or [] if str(a).strip() and _norm(a) != _norm(name)]
        if aliases:
            add["aliases"] = list(dict.fromkeys(aliases))
        if isinstance(item.get("safety_critical"), bool):
            add["safety_critical"] = item["safety_critical"]
        else:
            add["needs"].append("safety_critical (written as true until an SE decides)")
        if item.get("evidence"):
            add["evidence"] = str(item["evidence"])[:200]
        out["additions"].append(add)

    for a in analyses:
        where = f"picture {a.get('n', '?')} ({a.get('name', '')})"
        record = {"n": a.get("n"), "name": a.get("name"), "model": a.get("model"), "cached": a.get("cached", False)}
        if a.get("skipped"):
            out["pictures"].append({**record, "analysed": False, "note": a["skipped"]})
            continue
        if not a.get("is_structure_diagram", True):
            out["pictures"].append({**record, "analysed": True, "note": "not a diagram of systems and subsystems"})
            continue
        out["pictures"].append({**record, "analysed": True, "note": ""})
        for item in a.get("systems") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            sid = item.get("existing_id") if item.get("existing_id") in struct["systems"] else sys_by_name.get(_norm(name))
            sid = sid or new_sys.get(_norm(name))
            if sid in struct["systems"]:
                present(sid, name, where)
            elif not sid:
                if not struct["variant"]:
                    out["not_added"].append({"name": name, "from": where, "why": "the baseline has no variant to put a system under"})
                    continue
                sid = make_id("SYS", name, taken, sys_pat)
                taken.add(sid)
                new_sys[_norm(name)] = sid
                add = {"class": "System", "id": sid, "name": name, "parent": struct["variant"], "from": where, "needs": []}
                wbs = str(item.get("wbs_code") or "").strip()
                if _valid_wbs(wbs) and wbs not in used_wbs:
                    add["wbs_code"] = wbs
                    used_wbs.add(wbs)
                else:
                    add["needs"].append("wbs_code (the picture shows none)" if not wbs else
                                        f"wbs_code (the picture's {wbs!r} is not usable)")
                out["additions"].append(add)
            for sub in item.get("subsystems") or []:
                if isinstance(sub, dict):
                    subsystem(sub, sid, where)
        for sub in a.get("unplaced_subsystems") or []:
            subsystem(sub, None, where)
    return out


def summary(proposal: dict | None) -> list[str]:
    """The proposal in lines an SE can read."""
    if not proposal:
        return []
    lines = []
    for p in proposal["pictures"]:
        if not p["analysed"]:
            lines.append(f"Picture {p['n']} ({p['name']}): not analysed for structure: {p['note']}.")
        elif p["note"]:
            lines.append(f"Picture {p['n']} ({p['name']}): {p['note']}; nothing proposed.")
    adds = proposal["additions"]
    if adds:
        lines.append(f"The diagram{'s' if len(proposal['pictures']) > 1 else ''} show{'' if len(proposal['pictures']) > 1 else 's'} "
                     f"{sum(a['class'] == 'System' for a in adds)} system(s) and "
                     f"{sum(a['class'] == 'Subsystem' for a in adds)} subsystem(s) the baseline does not have:")
        for a in adds:
            needs = f"; still needed from an SE: {', '.join(a['needs'])}" if a["needs"] else ""
            lines.append(f"  + {a['class']} {a['id']} \"{a['name']}\" under {a['parent']} (from {a['from']}){needs}")
    elif any(p["analysed"] and not p["note"] for p in proposal["pictures"]):
        lines.append("The diagram shows no system or subsystem the baseline lacks.")
    if proposal["already_present"]:
        lines.append("Already in the baseline: " + ", ".join(
            f"{p['id']} (shown as \"{p['seen_as']}\")" for p in proposal["already_present"]) + ".")
    for x in proposal["not_added"]:
        lines.append(f"  ! \"{x['name']}\" (from {x['from']}) is NOT proposed: {x['why']}.")
    return lines


def for_document(pictures: list[Picture], s: Settings = default_settings, document: str = "",
                 onto: Ontology | None = None) -> dict:
    """Analyse a document's pictures and return the proposal against the current baseline."""
    data = yaml.safe_load(Path(s.baseline_yaml).read_text())
    struct = existing_structure(data)
    analyses = [{"n": n, "name": pic.name, **analyse(pic, struct, s)} for n, pic in enumerate(pictures, 1)]
    return propose(analyses, data, onto, document)


# ── Apply: write the additions into the baseline YAML ────────────────────────
# The baseline is an SE's hand-kept file with comments, so entries are
# inserted as text at the right place; nothing else in the file moves.

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_item(lines: list[str], eid: str) -> int:
    hits = [i for i, ln in enumerate(lines) if re.match(rf"^\s*- id:\s*{re.escape(eid)}\s*(#.*)?$", ln)]
    if len(hits) != 1:
        raise DiagramBaselineError(f"could not find the entry '- id: {eid}' in the baseline (it must be written one "
                                   "attribute per line)")
    return hits[0]


def _block_end(lines: list[str], start: int) -> int:
    """Index just past the last line of the list item that starts at `start`
    (trailing blank lines and the comments of what follows are not part of it)."""
    base, end = _indent(lines[start]), start + 1
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        if _indent(ln) <= base:
            break
        if not ln.lstrip().startswith("#") or _indent(ln) > base + 2:
            end = i + 1
    return end


def _quote(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render(add: dict, indent: int, document: str) -> list[str]:
    pad, inner = " " * indent, " " * (indent + 2)
    source = f"{document + ' ' if document else ''}{add['from']}"
    out = [f"{pad}- id: {add['id']}", f"{inner}name: {_quote(add['name'])}"]
    if add["class"] == "System":
        out.append(f"{inner}wbs_code: {_quote(add['wbs_code'])}" if add.get("wbs_code") else
                   f"{inner}# wbs_code: the diagram shows none. An SE gives the MIL-STD-881F code.")
    elif "safety_critical" in add:
        out.append(f"{inner}safety_critical: {'true' if add['safety_critical'] else 'false'}")
    else:
        out.append(f"{inner}safety_critical: true       # assumed: the diagram does not say. An SE decides.")
    if add.get("aliases"):
        out.append(f"{inner}aliases: [{', '.join(_quote(a) for a in add['aliases'])}]")
    out += [f"{inner}status: candidate          # read from a diagram: check it, then delete this line",
            f"{inner}derived_from: {_quote(source)}"]
    return out


def _errors(onto: Ontology, text: str) -> set[str]:
    return {f"{f.where}: {f.message}" for f in check_baseline(onto, yaml.safe_load(text)) if f.level == "error"}


def apply(proposal: dict, baseline_yaml: Path, onto: Ontology | None = None) -> dict:
    """Add the proposal's systems and subsystems to the baseline file.
    Returns {"applied": [ids], "blocks": [the text inserted], "baseline":
    path}; `revert` takes it back out. On any problem the file is left
    exactly as it was and DiagramBaselineError is raised."""
    onto = onto or default_ontology()
    path = Path(baseline_yaml)
    before = path.read_text()
    adds = proposal.get("additions") or []
    if not adds:
        return {"applied": [], "blocks": [], "baseline": str(path)}
    lines, blocks = before.split("\n"), []
    try:
        present = {v for v in re.findall(r"^\s*- id:\s*(\S+)", before, re.M)}
        for add in [a for a in adds if a["class"] == "System"] + [a for a in adds if a["class"] == "Subsystem"]:
            if add["id"] in present:
                raise DiagramBaselineError(f"{add['id']} is already in the baseline: make the proposal again")
            parent = _find_item(lines, add["parent"])
            key = "systems:" if add["class"] == "System" else "subsystems:"
            end = _block_end(lines, parent)
            child_indent = _indent(lines[parent]) + 2
            has_key = next((i for i in range(parent + 1, end)
                            if lines[i].strip().split("#")[0].strip() == key and _indent(lines[i]) == child_indent), None)
            if has_key is not None and any(lines[i].strip().startswith(key + " [") for i in range(parent + 1, end)):
                raise DiagramBaselineError(f"{add['parent']}'s {key} list is written on one line; write it one entry per line")
            new = []
            if has_key is None:
                new.append(" " * child_indent + key)
            item = _render(add, child_indent + 2, proposal.get("document", ""))
            if add["class"] == "System":
                new = new + [""] + [" " * (child_indent + 2) + "# Added from a diagram by ESAM: a candidate for an SE to confirm."] + item
            else:
                new += item
            lines[end:end] = new
            blocks.append("\n".join(new))
            present.add(add["id"])
        after = "\n".join(lines)
        data = yaml.safe_load(after)
        ids = set(existing_structure(data)["systems"]) | set(existing_structure(data)["subsystems"])
        lost = [a["id"] for a in adds if a["id"] not in ids]
        if lost:
            raise DiagramBaselineError(f"after writing, {', '.join(lost)} did not land where expected")
        new_errors = _errors(onto, after) - _errors(onto, before)
        if new_errors:
            raise DiagramBaselineError("the additions fail the ontology check: " + "; ".join(sorted(new_errors)))
        path.write_text(after)
        from .baseline import load_baseline
        load_baseline(path)
    except DiagramBaselineError:
        path.write_text(before)
        raise
    except Exception as e:
        path.write_text(before)
        raise DiagramBaselineError(f"the baseline could not take the additions ({type(e).__name__}: {e})") from e
    return {"applied": [a["id"] for a in adds], "blocks": blocks, "baseline": str(path)}


def revert(record: dict | None, baseline_yaml: Path, onto: Ontology | None = None) -> dict:
    """Take out what `apply` put in. An entry that something else now
    depends on (a document attached to it, an edit by an SE) stays: the
    file is only changed if every block is still there as written and the
    result loads and passes the ontology check."""
    if not record or not record.get("blocks"):
        return {"reverted": [], "kept": []}
    onto = onto or default_ontology()
    path = Path(baseline_yaml)
    before = text = path.read_text()
    for block in reversed(record["blocks"]):
        if text.count(block + "\n") != 1:
            return {"reverted": [], "kept": record["applied"],
                    "why": "an added entry was edited since; remove the candidates by hand if they are not wanted"}
        text = text.replace(block + "\n", "", 1)
    try:
        if _errors(onto, text) - _errors(onto, before):
            raise ValueError("other entries now depend on them")
        path.write_text(text)
        from .baseline import load_baseline
        load_baseline(path)
    except Exception as e:
        path.write_text(before)
        return {"reverted": [], "kept": record["applied"], "why": f"{e}; remove the candidates by hand if they are not wanted"}
    return {"reverted": record["applied"], "kept": []}


# ── Command line ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Systems and subsystems a picture diagram adds to the vehicle baseline")
    ap.add_argument("file", type=Path, help="a picture, or a Word document with pictures in it")
    ap.add_argument("--apply", action="store_true", help="write the additions to the baseline (as candidates)")
    ap.add_argument("--document", default="", help="what to call the source in derived_from (default: the file name)")
    args = ap.parse_args()
    from .readers import convert
    conv = convert(args.file, default_settings, diagrams=False)
    if not conv.pictures:
        sys.exit(f"{args.file.name}: no pictures in it")
    proposal = for_document(conv.pictures, default_settings, args.document or args.file.name)
    print("\n".join(summary(proposal)) or "Nothing to propose.")
    if args.apply and proposal["additions"]:
        try:
            done = apply(proposal, default_settings.baseline_yaml)
        except DiagramBaselineError as e:
            sys.exit(f"NOT APPLIED: {e}")
        print(f"\nAdded to {done['baseline']} as candidates: {', '.join(done['applied'])}.\n"
              "Reload the graph: python -m crew.main graph reload")
    elif proposal["additions"]:
        print("\nNothing was changed. Add --apply to write these to the baseline as candidates.")


if __name__ == "__main__":
    main()
