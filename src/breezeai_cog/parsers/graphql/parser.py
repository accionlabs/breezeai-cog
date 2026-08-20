"""GraphQLParser — a standalone-file **language parser** owning ``.graphql`` / ``.gql``.

Distinct from the ``typescript-graphql`` framework parser, which handles SDL/operations
**embedded** in ``.ts`` files (``gql`…`` template strings). A standalone schema or operation
document is not valid TypeScript, so it needs its own parser: it parses the whole file with
the ``graphql`` tree-sitter grammar and emits a ``Statement`` per construct (entities,
routes, value types, operations, fragments) — see :mod:`.sdl`.

Semantic capture is gated behind ``--capture-statements`` (the whole GraphQL surface is
semantic); the shared comment pass adds ``# …`` comments when capture is on.
"""

from __future__ import annotations

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from tree_sitter import Node

from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from .mappings import COMMENT_TYPES, FRAMEWORKS, STATEMENT_TYPES
from .sdl import collect_graphql_statements


class GraphQLParser(BaseParser):
    name = "graphql"
    extensions: tuple[str, ...] = (".graphql", ".gql")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("graphql", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        statements: list[Statement] = []

        # The GraphQL surface is entirely semantic (entities/routes/api_calls), so gate it on
        # --capture-statements like every other route/db/event emitter; skip fixture dirs.
        if ctx.capture_statements and not self.is_fixture_file(path):
            statements.extend(
                collect_graphql_statements(root, source, path, seen_ids, ctx.statement_text_limit)
            )

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="graphql",
            loc=count_loc(source.decode("utf-8", "replace")),
            statements=statements,
            framework="graphql" if statements else None,
        )

        if ctx.capture_statements:
            record.statements.extend(
                comment_statements_for(
                    root,
                    source,
                    path,
                    file_id=fid,
                    functions=[],
                    classes=[],
                    statements=record.statements,
                    control_flow=frozenset(),
                    comment_types=COMMENT_TYPES,
                    limit=ctx.statement_text_limit,
                    seen_ids=seen_ids,
                )
            )
        return record
