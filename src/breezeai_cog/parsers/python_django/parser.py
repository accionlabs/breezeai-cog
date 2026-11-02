"""DjangoParser — a Python framework parser. Selected (one parser per file) over the base
PythonParser when ``claims`` finds a Django signature; subclasses PythonParser so the base
extraction is inherited (one parse), then adds the routes of the file's ``urlpatterns``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..python.parser import PythonParser
from ..python_common import Origins
from ..treesitter import parse_source
from .urls import FRAMEWORK, detect_urls


class DjangoParser(PythonParser):
    name = "python-django"
    # Below FastAPI (10): a FastAPI file that also imports Django (its ORM) stays FastAPI's.
    priority = 8
    frameworks = [FRAMEWORK]

    def claims(self, path: str, source: bytes) -> bool:
        return b"django" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("python", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        routes = []
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_urls(root, ctx.source, ctx.path, index=ctx.resolution_index,
                                 repo_root=ctx.repo_root)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
        if routes or Origins(root, ctx.source).imports_package("django"):
            record.framework = self.label(root, ctx)
        return record

    def label(self, root: object, ctx: ParseContext) -> str:
        return FRAMEWORK
