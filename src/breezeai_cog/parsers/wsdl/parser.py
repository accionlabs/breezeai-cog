"""WsdlParser — a standalone-file **language parser** owning ``.wsdl``.

Parses a WSDL 1.1 SOAP service-contract document with the generic tree-sitter ``xml``
grammar (same grammar and ``parsers/xml_common.py`` namespace-stripping as
``xsd/parser.py``) and emits: one ``data_model`` statement per ``message``, one ``route``
statement per operation (``portType/operation`` correlated with ``binding/operation`` — see
:mod:`.operations`), one ``import`` statement per ``wsdl:import``, and reuses
``xsd/schema.py``'s walker for every ``<xsd:schema>`` embedded in ``<wsdl:types>`` (the
common case — most real WSDLs declare their message types inline rather than only importing
external ``.xsd`` files).

WSDL 2.0 (``interface``, no ``portType``) is out of scope — this parser targets WSDL 1.1,
the version this repo's existing SOAP support (``csharp_wcf``, ASMX) already targets.

Semantic capture is gated behind ``--capture-statements``; the shared comment pass adds
``<!-- … -->`` comments when capture is on.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from ..xml_common import child_elements, document_root, element_tag, find_child
from ..xsd.schema import collect_schema_statements
from .mappings import COMMENT_TYPES, FRAMEWORKS, STATEMENT_TYPES
from .operations import collect_definitions_statements


class WsdlParser(BaseParser):
    name = "wsdl"
    extensions: tuple[str, ...] = (".wsdl",)
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

        definitions = document_root(root)
        if (
            ctx.capture_statements
            and definitions is not None
            and element_tag(definitions, source) == "definitions"
            and not self.is_fixture_file(path)
        ):
            statements.extend(
                collect_definitions_statements(
                    definitions, source, path, seen_ids, ctx.statement_text_limit
                )
            )
            types = find_child(definitions, source, "types")
            if types is not None:
                for schema_node in child_elements(types):
                    if element_tag(schema_node, source) == "schema":
                        statements.extend(
                            collect_schema_statements(
                                schema_node, source, path, seen_ids, ctx.statement_text_limit
                            )
                        )

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="wsdl",
            loc=count_loc(source.decode("utf-8", "replace")),
            statements=statements,
            framework="wsdl" if statements else None,
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
