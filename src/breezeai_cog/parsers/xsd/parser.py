"""XsdParser — a standalone-file **language parser** owning ``.xsd``.

Parses an XML Schema (XSD) document with the generic tree-sitter ``xml`` grammar (verified
available via ``tree-sitter-language-pack``; the grammar is namespace-unaware, so
``parsers/xml_common.py`` strips ``xs:``/``xsd:`` prefixes for tag/attribute dispatch — see
that module's docstring) and emits a flat ``Statement`` per global declaration; see
:mod:`.schema`. The same schema-walker is reused by ``wsdl/parser.py`` for the inline
``<wsdl:types><xsd:schema>…`` block almost every real WSDL carries.

Semantic capture is gated behind ``--capture-statements`` (the whole XSD surface is
semantic, same as Prisma); the shared comment pass adds ``<!-- … -->`` comments when capture
is on.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from ..xml_common import document_root, element_tag
from .mappings import COMMENT_TYPES, FRAMEWORKS, STATEMENT_TYPES
from .schema import collect_schema_statements


class XsdParser(BaseParser):
    name = "xsd"
    extensions: tuple[str, ...] = (".xsd",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("xml", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        statements: list[Statement] = []

        schema_node = document_root(root)
        if (
            ctx.capture_statements
            and schema_node is not None
            and element_tag(schema_node, source) == "schema"
            and not self.is_fixture_file(path)
        ):
            statements.extend(collect_schema_statements(schema_node, source, path, seen_ids))

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="xsd",
            loc=count_loc(source.decode("utf-8", "replace")),
            statements=statements,
            framework="xsd" if statements else None,
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
