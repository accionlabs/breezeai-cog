from ..typescript.parser import TypeScriptParser
from .routes import detect_fastify_routes
from ..base import ParseContext
from ...schemas import FileRecord
from ..treesitter import parse_source

_CLAIMS_PARSE_TIMEOUT_MICROS = 1_000_000


def _is_fastify_string(node, source: bytes) -> bool:
    return node.type == "string" and source[node.start_byte:node.end_byte][1:-1] == b"fastify"


def _has_runtime_fastify_import(node, source: bytes) -> bool:
    if node.type == "import_statement":
        return (
            not any(child.type == "type" for child in node.children)
            and any(_is_fastify_string(child, source) for child in node.children)
        )

    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        return (
            function is not None
            and function.type == "identifier"
            and source[function.start_byte:function.end_byte] == b"require"
            and arguments is not None
            and any(_is_fastify_string(child, source) for child in arguments.children)
        )

    return any(_has_runtime_fastify_import(child, source) for child in node.children)


class FastifyParser(TypeScriptParser):
    name = "typescript-fastify"
    # Keep framework-specific parsing above base TypeScript, but below the other
    # priority-10 TypeScript framework parsers when claims overlap.
    priority = 9
    frameworks = ["fastify"]

    def claims(self, path: str, source: bytes) -> bool:
        grammar = "tsx" if path.endswith((".tsx", ".jsx")) else "typescript"
        try:
            root = parse_source(grammar, source, _CLAIMS_PARSE_TIMEOUT_MICROS).root_node
        except ValueError:
            return False
        return _has_runtime_fastify_import(root, source)

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
