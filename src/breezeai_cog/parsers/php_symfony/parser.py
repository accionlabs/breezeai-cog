"""Symfony framework parser with route-attribute detection."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.index import PhpIndex
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import detect_symfony_routes, strip_route_decorators


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 40
    frameworks = ["symfony"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Symfony\\Component\\Routing\\Attribute\\Route" in source
            or b"Symfony\\Component\\Routing\\Annotation\\Route" in source
            or b"Symfony\\Component\\Routing\\" in source and b"#[Route" in source
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "symfony"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            index = ctx.resolution_index if isinstance(ctx.resolution_index, PhpIndex) else None
            if detect_symfony_routes(root, ctx.source, ctx.path, record, index):
                strip_route_decorators(record)
        return record
