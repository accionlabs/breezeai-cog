from ..typescript.parser import TypeScriptParser
from .routes import detect_fastify_routes
from ..base import ParseContext
from ...schemas import FileRecord
from ..treesitter import parse_source
import re


_FASTIFY_IMPORT_SIG = re.compile(
    rb"(?:\bfrom\s*[\"']fastify[\"']|\brequire\s*\(\s*[\"']fastify[\"']\s*\))"
)


class FastifyParser(TypeScriptParser):
    name = "typescript-fastify"
    priority = 10
    frameworks = ["fastify"]

    def claims(self, path: str, source: bytes) -> bool:
        return bool(_FASTIFY_IMPORT_SIG.search(source))

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = (
            "tsx"
            if ctx.path.endswith((".tsx", ".jsx"))
            else "typescript"
        )

        root = parse_source(
            grammar,
            ctx.source,
            ctx.parse_timeout_micros,
        ).root_node

        record = self.extract(root, ctx)

        if (
            ctx.capture_statements
            and not self.is_fixture_file(ctx.path)
        ):
            routes = detect_fastify_routes(
                root,
                ctx.source,
                ctx.path,
                record,
                seen_ids={s.id for s in record.statements},
            )

            if routes:
                record.statements.extend(routes)
                record.framework = "fastify"

        return record
