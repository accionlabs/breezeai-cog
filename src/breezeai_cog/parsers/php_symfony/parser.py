"""SymfonyParser — PHP framework parser for Symfony applications."""

from __future__ import annotations

from pathlib import Path

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser, composer_requires
from ..php.ids import SeenIds
from ..statements_common import normalize_route_endpoints
from ..treesitter import parse_source
from .routes import detect_symfony_routes


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 20
    frameworks = ["symfony"]

    def claims(self, path: str, source: bytes, repo_root: Path | str | None = None) -> bool:
        return (
            b"Symfony\\Component\\Routing\\Attribute\\Route" in source
            or b"Symfony\\Component\\Routing\\Annotation\\Route" in source
            or b"#[AsController" in source
            or composer_requires(path, b"symfony/framework-bundle", repo_root=repo_root)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        seen_ids = SeenIds()
        record = self.extract(root, ctx, seen_ids=seen_ids)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            index = getattr(ctx.resolution_index, "fqcn", ctx.resolution_index)
            routes = detect_symfony_routes(
                record, seen_ids=seen_ids, root=root, source=ctx.source, fqcn_index=index
            )
            if routes:
                record.statements.extend(routes)
            normalize_route_endpoints(record.statements)
            if any(s.semanticType == "route" and s.framework == "symfony" for s in record.statements):
                record.framework = "symfony"
        return record
