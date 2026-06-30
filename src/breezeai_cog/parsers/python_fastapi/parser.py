"""FastAPIParser — a Python framework parser. Selected (one parser per file) over the
base PythonParser when ``claims`` finds a FastAPI signature; subclasses PythonParser so
all base extraction is inherited (no duplicated code, single parse), then adds FastAPI
route statements. Selection is per-file by ``claims`` (ARCHITECTURE.md §4)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..python.parser import PythonParser
from ..treesitter import parse_source
from .routes import detect_routes


class FastAPIParser(PythonParser):
    name = "python-fastapi"
    priority = 10  # selected over base PythonParser when it claims the file
    frameworks = ["fastapi"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"fastapi" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("python", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements (spec A4)
            routes = detect_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "fastapi"
        return record
