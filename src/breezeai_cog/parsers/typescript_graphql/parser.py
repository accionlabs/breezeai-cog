"""GraphQLParser — a TypeScript/JavaScript framework parser. Selected over the base
TypeScriptParser (single parser per file) when ``claims`` finds an Apollo /
``graphql-tools`` resolver map or GraphQL SDL; reuses ``TypeScriptParser.extract`` on
the shared tree, then detects GraphQL routes. It coexists with the other TS framework
parsers (Express, NestJS, Angular, LoopBack, React) because selection is per-file by
``claims``. GraphQL operations are object-literal / SDL config, so
route detection adds statements parented to the file (mirrors the React detector)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .routes import detect_embedded_sdl, detect_resolver_maps


class GraphQLParser(TypeScriptParser):
    name = "typescript-graphql"
    priority = 10
    frameworks = ["graphql"]

    def claims(self, path: str, source: bytes) -> bool:
        # Resolver map: an object typed ``Resolvers`` with a root-operation key. SDL: a
        # ``type Query|Mutation|Subscription {`` block (the ``{`` excludes the TS alias
        # ``type Query =``). Either signal marks a GraphQL operation source; the byte
        # sniff is cheap and specific enough to not steal Express/NestJS/React files.
        has_resolver_map = b"Resolvers" in source and (
            b"Query:" in source or b"Mutation:" in source or b"Subscription:" in source
        )
        has_sdl = (
            b"type Query {" in source
            or b"type Mutation {" in source
            or b"type Subscription {" in source
        )
        return has_resolver_map or has_sdl

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        if ctx.capture_statements and not self.is_fixture_file(
            ctx.path
        ):  # gated by statement capture; skip fixtures (R4)
            # Resolver-map operations first, then the embedded SDL — the order their ids are
            # given in (the SDL extractor disambiguates against the record as it then is).
            routes = detect_resolver_maps(root, ctx.source, ctx.path, ctx.resolution_index)
            apply(record, routes, ctx.source, ctx.path)
            gql_classes, sdl = detect_embedded_sdl(
                root, ctx.source, ctx.path, {s.id for s in record.statements},
                ctx.parse_timeout_micros, ctx.statement_text_limit,
            )
            if gql_classes or routes or sdl:
                record.classes.extend(gql_classes)
                record.statements.extend(sdl)
                record.framework = "graphql"
        return record
