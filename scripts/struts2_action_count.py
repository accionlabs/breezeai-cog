"""Struts2 action-count check: cog's ``route`` records against an independent count.

The acceptance check of the Struts2 support (BREEZEAI-1311): on a real Struts2 repository, the
number of actions cog records must match the actions declared in its configuration and
annotations. The expected side is counted here **without cog's parser code**, so a shared bug
cannot hide on both sides:

* XML: every ``<action name="…">`` directly inside a ``<package>`` of an XML document whose root
  element is ``<struts>`` (parsed with ElementTree);
* annotations: every ``@Action`` (the inner ones of ``@Actions`` included; ``//`` and ``/* */``
  comments removed first) in a Java file that imports
  ``org.apache.struts2.convention.annotation``.

Un-annotated Convention-plugin actions have no declaration to count, so they are listed apart
(``convention``): cog's records for them, for review, never part of the match.

Usage::

    python scripts/struts2_action_count.py <repo> [--json]

Exit status 0 when every file matches, 1 otherwise. The repository is only read, never run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

#: Folders never counted: VCS, dependencies, build output (cog prunes the same by default).
_SKIP = {".git", "node_modules", "target", "build", "out", "bin", "dist", ".idea", ".gradle"}
_ANNOTATIONS = "org.apache.struts2.convention.annotation"
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRINGS = re.compile(r'"(?:\\.|[^"\\])*"')
_ACTION = re.compile(r"@(?:[\w.]+\.)?Action\b(?!s)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _files(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def expected(repo: Path) -> Counter[str]:
    """Declared actions per repo-relative file."""
    counts: Counter[str] = Counter()
    for path in _files(repo):
        rel = path.relative_to(repo).as_posix()
        if path.suffix == ".xml":
            try:
                root = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            if _local(root.tag) != "struts":
                continue
            for package in (c for c in root if _local(c.tag) == "package"):
                counts[rel] += sum(1 for a in package if _local(a.tag) == "action" and a.get("name"))
        elif path.suffix == ".java":
            try:
                text = path.read_text("utf-8", "replace")
            except OSError:
                continue
            if _ANNOTATIONS not in text:
                continue
            code = _STRINGS.sub('""', _COMMENTS.sub("", text))
            found = len(_ACTION.findall(code))
            if found:
                counts[rel] += found
    return +counts


def recorded(repo: Path) -> tuple[Counter[str], Counter[str], list[dict]]:
    """cog's Struts2 actions per file: declared ones, Convention ones, and every record."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from breezeai_cog import iter_file_records

    declared: Counter[str] = Counter()
    convention: Counter[str] = Counter()
    rows: list[dict] = []
    for record in iter_file_records(repo):
        for s in record.statements:
            if s.framework != "struts2" or s.semanticType != "route" or s.routeKind != "route":
                continue
            # A Convention record sits on the class name; a declared one on its <action>/@Action.
            implicit = record.path.endswith(".java") and not s.text.lstrip().startswith("@")
            (convention if implicit else declared)[record.path] += 1
            rows.append({"path": record.path, "line": s.startLine, "endpoint": s.endpoint,
                         "handler": s.handler, "handlerPath": s.handlerPath,
                         "handlerLine": s.handlerLine, "convention": implicit})
    return declared, convention, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--json", action="store_true", help="print every record as JSON")
    args = parser.parse_args()
    repo = args.repo.resolve()

    want = expected(repo)
    got, convention, rows = recorded(repo)
    files = sorted(set(want) | set(got))
    mismatches = [(f, want[f], got[f]) for f in files if want[f] != got[f]]
    located = sum(1 for r in rows if not r["convention"] and r["handlerLine"] is not None)

    if args.json:
        print(json.dumps(rows, indent=2))
    print(f"files with actions: {len(files)}")
    print(f"declared actions:   expected {sum(want.values())}, recorded {sum(got.values())}")
    print(f"  with a located handler: {located}")
    print(f"convention actions (not declared, for review): {sum(convention.values())}")
    for f, n in sorted(convention.items()):
        print(f"    {f}: {n}")
    if mismatches:
        print(f"MISMATCH in {len(mismatches)} file(s):")
        for f, w, g in mismatches:
            print(f"    {f}: expected {w}, recorded {g}")
        return 1
    print("OK: every file matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
