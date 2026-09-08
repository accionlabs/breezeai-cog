"""CodeIgniterParser — PHP framework parser for CodeIgniter applications (CI3 and CI4)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import detect_codeigniter_routes


class CodeIgniterParser(PhpParser):
    name = "php-codeigniter"
    priority = 10
    frameworks = ["codeigniter"]

    def claims(self, path: str, source: bytes) -> bool:
        path_lower = path.lower().replace("\\", "/")
        return (
            b"CodeIgniter" in source
            or b"$routes->" in source
            or b"Config\\Routes" in source
            or b"CI_Controller" in source
            or b"CI_Model" in source
            or b"BaseController" in source
            or (
                b"$route[" in source
                and ("routes.php" in path_lower or "config" in path_lower)
            )
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction, ONE parse
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            routes = detect_codeigniter_routes(
                root, ctx.source, ctx.path, seen_ids={s.id for s in record.statements}
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "codeigniter"
        return record
