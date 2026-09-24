"""LaravelParser — PHP framework parser for Laravel applications."""

from __future__ import annotations

from pathlib import Path

from ...emit import SeenIds
from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser, composer_requires
from ..statements_common import normalize_route_endpoints
from ..treesitter import parse_source
from .routes import detect_laravel_routes


class LaravelParser(PhpParser):
    name = "php-laravel"
    priority = 20
    frameworks = ["laravel"]

    def claims(self, path: str, source: bytes, repo_root: Path | str | None = None) -> bool:
        return (
            b"Illuminate\\" in source
            or b"Route::" in source
            or composer_requires(path, b"laravel/framework", repo_root=repo_root)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        seen_ids = SeenIds()
        record = self.extract(root, ctx, seen_ids=seen_ids)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            index = getattr(ctx.resolution_index, "fqcn", ctx.resolution_index)
            routes = detect_laravel_routes(
                root, ctx.source, ctx.path, seen_ids=seen_ids, fqcn_index=index
            )
            if routes:
                record.statements.extend(routes)
            normalize_route_endpoints(record.statements)
            if any(s.semanticType == "route" and s.framework == "laravel" for s in record.statements):
                record.framework = "laravel"
        return record
