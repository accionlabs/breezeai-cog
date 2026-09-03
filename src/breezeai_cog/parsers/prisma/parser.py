"""PrismaParser — a standalone-file **language parser** owning ``.prisma``.

Parses a Prisma Schema Language (PSL) document — the declarative schema (data ``model``s,
``enum``s, and ``datasource``/``generator`` config) that Prisma generates its client from.
It parses the whole file with the ``prisma`` tree-sitter grammar and emits a flat
``Statement`` per top-level block: ``model`` → a ``data_model`` entity (fields + relations on
``text``), ``enum`` and the config blocks → plain statements. See :mod:`.schema`.

Semantic capture is gated behind ``--capture-statements`` (the whole PSL surface is
semantic); the shared comment pass adds ``// …`` / ``/// …`` comments when capture is on.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from .mappings import COMMENT_TYPES, FRAMEWORKS, STATEMENT_TYPES
from .schema import collect_prisma_statements


class PrismaParser(BaseParser):
    name = "prisma"
    extensions: tuple[str, ...] = (".prisma",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("prisma", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        statements: list[Statement] = []

        # The PSL surface is entirely semantic (models/enums/config), so gate it on
        # --capture-statements like every other route/db/entity emitter; skip fixture dirs.
        if ctx.capture_statements and not self.is_fixture_file(path):
            statements.extend(
                collect_prisma_statements(root, source, path, seen_ids, ctx.statement_text_limit)
            )

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="prisma",
            loc=count_loc(source.decode("utf-8", "replace")),
            statements=statements,
            framework="prisma" if statements else None,
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
