"""SlimParser — PHP framework parser for Slim framework applications."""

from __future__ import annotations

from pathlib import Path

from ...emit import SeenIds
from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser, composer_requires
from ..treesitter import parse_source
from .routes import detect_slim_routes


class SlimParser(PhpParser):
    name = "php-slim"
    priority = 15
    frameworks = ["slim"]

    def claims(self, path: str, source: bytes, repo_root: Path | str | None = None) -> bool:
        return (
            b"Slim\\App" in source
            or b"Slim\\Factory\\AppFactory" in source
            or composer_requires(path, b"slim/slim", repo_root=repo_root)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        seen_ids = SeenIds()
        record = self.extract(root, ctx, seen_ids=seen_ids)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_slim_routes(
                root, ctx.source, ctx.path, seen_ids=seen_ids
            )
            if routes:
                record.statements.extend(routes)
            if any(s.semanticType == "route" and s.framework == "slim" for s in record.statements):
                record.framework = "slim"
        return record
