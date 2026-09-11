"""SlimParser — PHP framework parser for Slim framework applications."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser, composer_requires
from ..treesitter import parse_source
from .routes import detect_slim_routes


class SlimParser(PhpParser):
    name = "php-slim"
    priority = 15
    frameworks = ["slim"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Slim\\App" in source
            or b"Slim\\Factory\\AppFactory" in source
            or composer_requires(path, b"slim/slim")
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_slim_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "slim"
        return record
