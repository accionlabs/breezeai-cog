"""Capture a repository with templates on and compare two captures record by record.

A change to a shared module or a parser needs a before/after capture diff. This script makes
that diff mechanical: it reports every record (file, function, class, statement) and every
field that is **lost**, **added** or **changed** between two captures, and exits non-zero when
anything is lost.

Matching ignores what a refactor may legitimately change: record ``id`` / ``parentId`` and a
statement's ``nodeType`` (a ``synthetic`` node may become a real grammar node). A statement is
matched on its file, line, ``semanticType`` and ``name``; inside one key, records with the same
``nodeType`` and text pair first.

    python scripts/capture_compare.py capture <repo> <out.ndjson>
    python scripts/capture_compare.py compare <before> <after> [--json]
    python scripts/capture_compare.py check [--update]

``check`` captures every sample project under ``tests/unit/fixtures/templates/`` and compares
it with its baseline in ``tests/unit/fixtures/template_baselines/``; ``--update`` rewrites the
baselines instead (only on purpose, with the change explained in the commit).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "unit" / "fixtures" / "templates"
BASELINES = ROOT / "tests" / "unit" / "fixtures" / "template_baselines"

#: Fields a refactor may change without anything being lost.
_IGNORED = {"id", "parentId"}
#: Child collections of a file record, compared record by record rather than as a field.
_CHILDREN = ("functions", "classes", "statements")


# ── capture ──────────────────────────────────────────────────────────────────────────────────


def capture(repo: Path) -> list[dict]:
    """File records of ``repo`` captured with templates on, sorted by path.

    ``projectMetaData`` is dropped: it holds a timestamp and an absolute path."""
    from breezeai_cog import analyze_repo

    with tempfile.TemporaryDirectory() as out:
        result = analyze_repo(repo, capture_templates=True, out=out, jobs=1)
        assert result.out_path is not None
        with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    files = [r for r in records if r.get("__type") != "projectMetaData"]
    return sorted(files, key=lambda r: r["path"])


def write(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def load(path: Path) -> list[dict]:
    """File records from a ``.ndjson`` / ``.ndjson.gz`` capture, or the one inside a directory."""
    if path.is_dir():
        found = sorted(path.glob("*.ndjson.gz")) or sorted(path.glob("*.ndjson"))
        if len(found) != 1:
            raise SystemExit(f"{path}: expected exactly one capture file, found {len(found)}")
        path = found[0]
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    return [r for r in records if r.get("__type") != "projectMetaData"]


# ── compare ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class Diff:
    lost: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {"lost": self.lost, "added": self.added, "changed": self.changed}


def _key(kind: str, path: str, rec: dict) -> tuple:
    if kind == "statements":
        return (kind, path, rec.get("startLine"), rec.get("semanticType"), rec.get("name"))
    return (kind, path, rec.get("name"), rec.get("startLine"))


def _label(key: tuple) -> str:
    kind, path, *rest = key
    return f"{kind[:-1] if kind.endswith('s') else kind} {path} " + " ".join(
        str(part) for part in rest if part is not None
    )


def _fields(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in _IGNORED and k not in _CHILDREN}


def _compare_fields(label: str, before: dict, after: dict, diff: Diff) -> None:
    for name, old in before.items():
        new = after.get(name)
        if name not in after or new is None:
            diff.lost.append(f"{label}: field {name}={json.dumps(old)[:80]}")
        elif isinstance(old, list) and isinstance(new, list):
            gone = [item for item in old if item not in new]
            extra = [item for item in new if item not in old]
            if gone:
                diff.lost.append(f"{label}: {name} lost {json.dumps(gone)[:120]}")
            if extra:
                diff.added.append(f"{label}: {name} added {json.dumps(extra)[:120]}")
        elif old != new:
            diff.changed.append(f"{label}: {name} {json.dumps(old)[:60]} -> {json.dumps(new)[:60]}")
    for name, new in after.items():
        if name not in before and new is not None:
            diff.added.append(f"{label}: field {name}={json.dumps(new)[:80]}")


def _pair(old: list[dict], new: list[dict]) -> tuple[list[tuple[dict, dict]], list, list]:
    """Pair records sharing a key: identical first, then same nodeType and text, then in order."""
    old, new = list(old), list(new)
    pairs: list[tuple[dict, dict]] = []
    for same in (
        lambda a, b: _fields(a) == _fields(b),
        lambda a, b: (a.get("nodeType"), a.get("text")) == (b.get("nodeType"), b.get("text")),
        lambda a, b: True,
    ):
        for a in list(old):
            match = next((b for b in new if same(a, b)), None)
            if match is not None:
                pairs.append((a, match))
                old.remove(a)
                new.remove(match)
    return pairs, old, new


def compare(before: list[dict], after: list[dict]) -> Diff:
    diff = Diff()
    old_files = {r["path"]: r for r in before}
    new_files = {r["path"]: r for r in after}
    for path in sorted(old_files.keys() - new_files.keys()):
        diff.lost.append(f"file {path}")
    for path in sorted(new_files.keys() - old_files.keys()):
        diff.added.append(f"file {path}")
    for path in sorted(old_files.keys() & new_files.keys()):
        old, new = old_files[path], new_files[path]
        _compare_fields(f"file {path}", _fields(old), _fields(new), diff)
        for kind in _CHILDREN:
            groups: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
            for rec in old.get(kind) or []:
                groups[_key(kind, path, rec)][0].append(rec)
            for rec in new.get(kind) or []:
                groups[_key(kind, path, rec)][1].append(rec)
            gone: list[tuple[tuple, dict]] = []
            extra: list[tuple[tuple, dict]] = []
            for key in sorted(groups, key=str):
                pairs, old_left, new_left = _pair(*groups[key])
                gone += [(key, r) for r in old_left]
                extra += [(key, r) for r in new_left]
                for a, b in pairs:
                    _compare_fields(_label(key), _fields(a), _fields(b), diff)
            _moved(kind, path, gone, extra, diff)
    return diff


def _moved(kind: str, path: str, gone: list, extra: list, diff: Diff) -> None:
    """A record left over on both sides that differs only in its line has moved, not been lost.

    Lines shift when a sample file is edited; reporting that as lost + added would bury a real
    loss in noise. Pairs are made in line order between records of the same name and type."""

    def same(rec: dict) -> tuple:
        return (rec.get("semanticType"), rec.get("name"), rec.get("nodeType"))

    for key, old in sorted(gone, key=lambda item: item[1].get("startLine") or 0):
        match = next((item for item in extra if same(item[1]) == same(old)), None)
        if match is None:
            diff.lost.append(f"{_label(key)} [{old.get('nodeType', '')}]")
            continue
        extra.remove(match)
        label = _label(key)
        diff.changed.append(f"{label}: moved to line {match[1].get('startLine')}")
        lines = {"startLine", "endLine"}
        before = {k: v for k, v in _fields(old).items() if k not in lines}
        after = {k: v for k, v in _fields(match[1]).items() if k not in lines}
        _compare_fields(label, before, after, diff)
    diff.added.extend(f"{_label(key)} [{rec.get('nodeType', '')}]" for key, rec in extra)


def report(diff: Diff, title: str = "") -> str:
    lines = [title] if title else []
    for name in ("lost", "added", "changed"):
        items = getattr(diff, name)
        lines.append(f"  {name}: {len(items)}")
        lines.extend(f"    - {item}" for item in items)
    return "\n".join(lines)


# ── check ────────────────────────────────────────────────────────────────────────────────────


def sample_projects() -> list[Path]:
    return sorted(p for p in SAMPLES.iterdir() if p.is_dir())


def baseline_path(project: Path) -> Path:
    return BASELINES / f"{project.name}.ndjson"


def check(update: bool = False) -> dict[str, Diff]:
    """Capture each sample project and compare it with its baseline (or rewrite the baseline)."""
    results: dict[str, Diff] = {}
    for project in sample_projects():
        records = capture(project)
        if update:
            write(records, baseline_path(project))
            continue
        results[project.name] = compare(load(baseline_path(project)), records)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="capture a repository with templates on")
    cap.add_argument("repo", type=Path)
    cap.add_argument("out", type=Path)
    cmp_ = sub.add_parser("compare", help="compare two captures")
    cmp_.add_argument("before", type=Path)
    cmp_.add_argument("after", type=Path)
    cmp_.add_argument("--json", action="store_true", help="print the diff as JSON")
    chk = sub.add_parser("check", help="compare every sample project with its baseline")
    chk.add_argument("--update", action="store_true", help="rewrite the baselines")
    args = parser.parse_args(argv)

    if args.command == "capture":
        write(capture(args.repo), args.out)
        return 0
    if args.command == "compare":
        diff = compare(load(args.before), load(args.after))
        print(json.dumps(diff.as_dict(), indent=2) if args.json else report(diff))
        return 1 if diff.lost else 0
    results = check(update=args.update)
    if args.update:
        print(f"baselines written to {BASELINES}")
        return 0
    for name, diff in results.items():
        print(report(diff, title=name))
    return 1 if any(diff.lost for diff in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
