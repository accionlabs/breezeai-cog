"""SlimParser — PHP framework parser for Slim framework applications."""

from __future__ import annotations

from typing import ClassVar

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import detect_slim_routes


class SlimParser(PhpParser):
    name = "php-slim"
    priority = 10
    frameworks: ClassVar[list[str]] = ["slim"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Slim\\" in source
            or b"\\Slim\\App" in source
            or b"AppFactory" in source
            or (b"$app->get(" in source or b"$app->post(" in source)
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
