"""Symfony framework parser with route-attribute detection."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.index import PhpIndex
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import (
    detect_symfony_events,
    detect_symfony_routes,
    detect_symfony_route_builders,
    strip_route_decorators,
)


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 40
    frameworks = ["symfony"]

    def claims(self, path: str, source: bytes) -> bool:
        lower = source.lower()
        # HttpFoundation is a dependency of many non-Symfony applications, notably
        # Laravel.  It is not ownership evidence.  Require Symfony's routing,
        # dispatcher, messenger, or framework namespace instead.
        if b"illuminate\\" in lower:
            return False
        return (
            b"namespace symfony\\" in lower
            or b"symfony\\component\\routing\\" in lower
            or b"symfony\\component\\eventdispatcher\\" in lower
            or b"symfony\\component\\messenger\\" in lower
            or b"symfony\\bundle\\frameworkbundle\\" in lower
            or b"#[route" in lower
            or b"routingconfigurator" in lower
            or (b"routecollection" in lower and b"symfony\\component\\routing" in lower)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "symfony"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            index = ctx.resolution_index if isinstance(ctx.resolution_index, PhpIndex) else None
            route_names = detect_symfony_routes(root, ctx.source, ctx.path, record, index)
            detect_symfony_route_builders(root, ctx.source, ctx.path, record)
            detect_symfony_events(root, ctx.source, ctx.path, record)
            if route_names:
                strip_route_decorators(record, route_names)
        return record


