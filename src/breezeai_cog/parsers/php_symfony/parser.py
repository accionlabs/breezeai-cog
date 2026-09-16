"""SymfonyParser — PHP framework parser for Symfony applications."""

from __future__ import annotations

from ...emit import SeenIds
from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser, composer_requires
from ..treesitter import parse_source
from .routes import detect_symfony_routes


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 20
    frameworks = ["symfony"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Symfony\\Component\\Routing\\Attribute\\Route" in source
            or b"Symfony\\Component\\Routing\\Annotation\\Route" in source
            or b"#[AsController" in source
            or composer_requires(path, b"symfony/framework-bundle")
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        seen_ids = SeenIds()
        record = self.extract(root, ctx, seen_ids=seen_ids)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_symfony_routes(record, seen_ids=seen_ids)
            if routes:
                record.statements.extend(routes)
            if any(s.semanticType == "route" and s.framework == "symfony" for s in record.statements):
                record.framework = "symfony"
        return record
