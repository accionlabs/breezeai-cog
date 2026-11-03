"""Check a breezeai-cog capture: schema validity, entry-point contract, and counts.

Usage (from the cog checkout, so the installed package is used):

    uv run python skills/gap-review/scripts/check_capture.py <capture.ndjson.gz> [--framework X]

Prints, for every file record (the projectMetaData line is skipped):
- schema errors (lines that do not validate as a FileRecord);
- entry-point contract violations (parsers/contract.py), by rule and severity;
- counts by language / framework, and statements by semanticType and by framework.

--framework limits the contract and statement counts to statements of that framework. The
output is the "Capture summary" and "Correctness of the run" evidence of a review; it does not
judge whether a count is right — compare it against an independent count of the source.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from breezeai_cog.parsers.contract import validate
from breezeai_cog.schemas.capture import FileRecord

_SHOW = 10  # examples printed per problem kind


def _records(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            raw = json.loads(line)
            if raw.get("__type") == "projectMetaData":
                continue
            yield n, raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--framework", help="only statements with this framework label")
    args = ap.parse_args()

    files: Counter[tuple[str, str]] = Counter()
    by_type: Counter[str] = Counter()
    by_framework: Counter[str] = Counter()
    classes = functions = 0
    schema_errors: list[str] = []
    violations: Counter[tuple[str, str]] = Counter()
    examples: list[str] = []

    for n, raw in _records(args.capture):
        try:
            rec = FileRecord.model_validate(raw)
        except ValidationError as exc:
            schema_errors.append(f"line {n} {raw.get('path')}: {exc.errors()[0]['msg']}")
            continue
        files[(rec.language, rec.framework or "-")] += 1
        classes += len(rec.classes)
        functions += len(rec.functions)
        for s in rec.statements:
            if args.framework and s.framework != args.framework:
                continue
            by_type[s.semanticType or "(plain)"] += 1
            if s.semanticType:
                by_framework[s.framework or "-"] += 1
        for v in validate(rec):
            stmt = next(s for s in rec.statements if s.id == v.statement_id)
            if args.framework and stmt.framework != args.framework:
                continue
            violations[(v.rule, v.severity)] += 1
            if len(examples) < _SHOW:
                examples.append(f"{rec.path}:{stmt.startLine} {v.rule} ({v.severity}): {v.detail}")

    print(f"# {args.capture.name}")
    print(f"\nfiles: {sum(files.values())}  classes: {classes}  functions: {functions}")
    print("\n## files by language / framework")
    for (lang, fw), c in sorted(files.items()):
        print(f"  {lang:<14} {fw:<24} {c}")
    print("\n## statements by semanticType")
    for t, c in by_type.most_common():
        print(f"  {t:<24} {c}")
    print("\n## classified statements by framework")
    for fw, c in by_framework.most_common():
        print(f"  {fw:<24} {c}")
    print(f"\n## schema errors: {len(schema_errors)}")
    for e in schema_errors[:_SHOW]:
        print(f"  {e}")
    print(f"\n## contract violations: {sum(violations.values())}")
    for (rule, sev), c in violations.most_common():
        print(f"  {rule:<14} {sev:<6} {c}")
    for e in examples:
        print(f"  e.g. {e}")
    return 1 if schema_errors or any(sev == "error" for _, sev in violations) else 0


if __name__ == "__main__":
    sys.exit(main())
