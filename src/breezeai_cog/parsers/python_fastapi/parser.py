"""FastAPIParser — a Python framework parser that OVERRIDES the base PythonParser
for the files it matches (single parse, reuses ``PythonParser.extract`` on the
shared tree, then adds FastAPI route statements).

It subclasses PythonParser, so all base extraction is inherited — no duplicated
code. ``overrides = ("python",)`` makes the registry skip the base PythonParser.
(For stacking multiple frameworks on one file, use composition instead; see
ARCHITECTURE.md §4. First-party framework support will ultimately live in the
shared ``parsers/detection`` in M4.)
"""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..python.parser import PythonParser
from ..treesitter import parse_source
from .routes import detect_routes


class FastAPIParser(PythonParser):
    name = "python-fastapi"
    overrides = ("python",)  # supersede the base PythonParser; no composition
    frameworks = ["fastapi"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("python", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        routes = detect_routes(
            root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
        )
        if routes:
            record.statements.extend(routes)
            record.framework = "fastapi"
        return record
