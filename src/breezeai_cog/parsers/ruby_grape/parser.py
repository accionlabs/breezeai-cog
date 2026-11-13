"""Grape framework parser selected over the base Ruby parser."""

from __future__ import annotations

import re

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from .routes import detect_grape_routes

_GRAPE_REQUIRE = re.compile(rb"""(?m)^\s*require(?:_relative)?\s*(?:\(\s*)?['"]grape['"]""")
_GRAPE_API = re.compile(
    rb"(?m)^\s*class\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\s*<\s*"
    rb"(?:::)?Grape\s*::\s*(?:API|Endpoint)\b"
    rb"|^\s*(?:[A-Za-z_]\w*\s*=\s*)?Class\s*\.\s*new\s*\(\s*"
    rb"(?:::)?Grape\s*::\s*API\b"
)


class GrapeParser(RubyParser):
    name = "ruby-grape"
    priority = 20
    frameworks = ["grape"]

    def claims(self, path: str, source: bytes) -> bool:
        if b"grape" not in source.lower():
            return False
        return _GRAPE_REQUIRE.search(source) is not None or _GRAPE_API.search(source) is not None

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_grape_routes(root, ctx.source, ctx.path, record)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "grape"
        return record
