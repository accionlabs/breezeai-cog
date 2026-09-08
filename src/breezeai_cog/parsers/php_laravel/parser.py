"""LaravelParser — PHP framework parser for Laravel applications."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import detect_laravel_routes


class LaravelParser(PhpParser):
    name = "php-laravel"
    priority = 10
    frameworks = ["laravel"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Illuminate" in source or b"Route::" in source or b"App\\Http\\Controllers" in source
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_laravel_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "laravel"
        return record
