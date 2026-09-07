"""SymfonyParser — PHP framework parser for Symfony applications."""

from __future__ import annotations

from typing import ClassVar

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import detect_symfony_routes


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 10
    frameworks: ClassVar[list[str]] = ["symfony"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"Symfony\\" in source or b"#[Route(" in source or b"#[AsController" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_symfony_routes(record, seen_ids={s.id for s in record.statements})
            if routes:
                record.statements.extend(routes)
                record.framework = "symfony"
        return record
