"""
Undo for the baseline and ontology commands.

Every change `python -m crew.main baseline ...` or `... ontology ...` writes
is remembered: the file as it was before, what the command did, and a
fingerprint of the file as the command left it. The record sits beside the
file, in `.history/<file name>/` (not under version control; the last 100
changes are kept).

    python -m crew.main baseline history       # the changes that can be undone, newest first
    python -m crew.main baseline undo          # take the newest one back; again for the one before
    python -m crew.main ontology history
    python -m crew.main ontology undo

`undo` puts the file back exactly as it was before that change, to the
byte. It does so only when the file is still as the command left it. If
the file was edited since (by hand, by the diagram reader, by the intake
pipeline), undoing would throw that edit away, so it is refused and the
edit is named as the reason; --force restores the earlier file all the
same. `git` remains the record of everything else.

No model, no database; the graph is not touched (graph reload afterwards).
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

KEEP = 100


class HistoryError(ValueError):
    """Nothing was restored. The message says why."""


def _dir(path: Path) -> Path:
    return Path(path).parent / ".history" / Path(path).name


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(path: Path, before: str, after: str, change: str) -> str | None:
    """Remember one change. Returns the record's name, or None when it could
    not be kept (the change itself stands; undo is then not available for it)."""
    folder = _dir(path)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        name = now.strftime("%Y%m%dT%H%M%S%f")
        (folder / f"{name}.json").write_text(json.dumps(
            {"when": now.isoformat(timespec="seconds"), "change": change, "file": str(path),
             "after": fingerprint(after), "before": before}, ensure_ascii=False))
        for old in sorted(folder.glob("*.json"))[:-KEEP]:
            old.unlink(missing_ok=True)
        return name
    except OSError:
        return None


def entries(path: Path) -> list[dict]:
    """The changes that can be undone, newest first: [{"name", "when", "change", "after", "before"}]."""
    out = []
    for f in sorted(_dir(path).glob("*.json"), reverse=True):
        try:
            out.append({"name": f.stem, **json.loads(f.read_text())})
        except (OSError, ValueError):
            continue
    return out


def undo(path: Path, force: bool = False, dry_run: bool = False, check=None) -> dict:
    """Put the file back as it was before the newest remembered change.

    `check(text)` may return a list of problems the restored file would have
    (it is asked before anything is written; with problems the undo is
    refused unless `force`). Returns {"change", "when", "written", "edited_since",
    "problems", "left": how many more can be undone after this one, "file"}."""
    path = Path(path)
    found = entries(path)
    if not found:
        raise HistoryError(f"there is nothing to undo for {path.name}: no change made by these commands is remembered "
                           "(changes made by hand are in git)")
    last = found[0]
    current = path.read_text()
    edited = fingerprint(current) != last["after"]
    if edited and not force:
        raise HistoryError(
            f"{path.name} was edited after the last change these commands made ({last['change']}, {last['when']}): by "
            "hand, by the diagram reader or by the intake pipeline. Undoing would throw that edit away. To put the file "
            "back as it was before that change all the same, add --force; to see what differs: git diff")
    problems = list(check(last["before"]) or []) if check else []
    if problems and not force:
        raise HistoryError(f"undoing '{last['change']}' was refused: the file would then have these problems.\n  "
                           + "\n  ".join(problems) + "\nUndo what depends on it first, or add --force")
    if not dry_run:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(last["before"])
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise HistoryError(f"NOT restored: {path} could not be changed ({e.strerror or e})") from e
        if path.read_text() != last["before"]:
            raise HistoryError(f"NOT restored: {path} does not hold the earlier text after writing it")
        (_dir(path) / f"{last['name']}.json").unlink(missing_ok=True)
    return {"change": last["change"], "when": last["when"], "written": not dry_run, "edited_since": edited,
            "problems": problems, "left": len(found) - 1, "file": str(path)}
