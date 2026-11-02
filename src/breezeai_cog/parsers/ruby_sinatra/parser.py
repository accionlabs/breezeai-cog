"""Sinatra framework parser selected over the base Ruby parser."""

from __future__ import annotations

import re

from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..emits import apply
from ..ruby.parser import RubyParser
from ..treesitter import parse_source
from .routes import detect_sinatra_routes


_SINATRA_REQUIRE = re.compile(
    rb"""(?m)^\s*require(?:_relative)?\s*(?:\(\s*)?['"]sinatra(?:/base)?['"]"""
)
_SINATRA_CLASS = re.compile(
    rb"(?m)^\s*class\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\s*<\s*"
    rb"(?:::)?Sinatra::(?:Base|Application)\b"
)
_SINATRA_FACTORY = re.compile(
    rb"(?m)^\s*(?:[A-Za-z_]\w*\s*=\s*)?Sinatra\s*\.\s*new\b"
    rb"|^\s*(?:[A-Za-z_]\w*\s*=\s*)?Class\s*\.\s*new\s*\(\s*"
    rb"(?:::)?Sinatra::(?:Base|Application)\b"
)
_SPEC_HELPER_REQUIRE = re.compile(
    rb"""(?m)^\s*require(?:_relative)?\s*(?:\(\s*)?['"]spec_helper['"]"""
)
_SINATRA_NAMESPACE = re.compile(
    rb"(?m)^\s*(?!#)[^\n]*\bSinatra::[A-Za-z_]\w*"
)


def _statement_order(statement: Statement) -> tuple[int, int, int]:
    match = re.search(r":\d+:(\d+)(?:#(\d+))?$", statement.id)
    if match is None:
        return statement.startLine, 0, 1
    return statement.startLine, int(match.group(1)), int(match.group(2) or "1")


class SinatraParser(RubyParser):
    name = "ruby-sinatra"
    priority = 10
    frameworks = ["sinatra"]  # noqa: RUF012

    def claims(self, path: str, source: bytes) -> bool:
        if b"sinatra" not in source.lower():
            return False
        direct = any(
            signature.search(source) is not None
            for signature in (_SINATRA_REQUIRE, _SINATRA_CLASS, _SINATRA_FACTORY)
        )
        helper = (
            _SPEC_HELPER_REQUIRE.search(source) is not None
            and _SINATRA_NAMESPACE.search(source) is not None
        )
        return direct or helper

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_sinatra_routes(root, ctx.source, ctx.path, record)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.statements.sort(key=_statement_order)
                record.framework = "sinatra"
        return record
