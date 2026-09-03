"""KtorParser — framework parser for Ktor (server) applications.

Extends KotlinParser. Claimed for any .kt file that imports from io.ktor.server.*,
then detects Ktor HTTP route registrations (get/post/put/delete/patch/head/options
DSL calls) and emits them as Statement records with semanticType="route".

Route detection is gated by --capture-statements, consistent with the Spring Boot
parser's behaviour for annotated @RequestMapping routes.
"""

from __future__ import annotations

from ...emit import file_id as _file_id
from ...schemas import FileRecord
from ..base import ParseContext
from ..kotlin.parser import KotlinParser
from ..treesitter import parse_source
from .routes import detect_ktor_routes


class KtorParser(KotlinParser):
    name = "kotlin-ktor"
    priority = 10
    frameworks = ["ktor"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"io.ktor.server" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("kotlin", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # full Kotlin structural extraction

        record.framework = "ktor"

        if ctx.capture_statements:
            fid = _file_id(ctx.path)
            seen_ids: set[str] = (
                {record.id}
                | {c.id for c in record.classes}
                | {f.id for f in record.functions}
                | {s.id for s in record.statements}
            )
            routes = detect_ktor_routes(
                root,
                ctx.source,
                ctx.path,
                fid,
                seen_ids,
                ctx.statement_text_limit,
            )
            record.statements.extend(routes)

        return record
