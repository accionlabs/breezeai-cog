"""CSharpParser — extracts one ``.cs`` file into a FileRecord. C# is Java-like: all
declarations live in classes/structs/etc., which in turn live in namespaces (block or
file-scoped), so extraction recurses through namespaces to reach the type declarations,
then emits imports + classes (with their flat methods and statements).

No ``build_index``: C# ``using`` names a namespace (spanning many files/assemblies),
not a file path — so imports stay external and calls resolve same-file only
(precision-first, Phase 1)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..treesitter import parse_source
from .classes import build_class
from .functions import defined_names, type_map
from .imports import extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = (
    "class_declaration", "interface_declaration", "enum_declaration",
    "struct_declaration", "record_declaration",
)
_NAMESPACE_TYPES = ("namespace_declaration", "file_scoped_namespace_declaration")


def iter_type_declarations(root: Node):
    """Yield every top-level type declaration, descending through namespaces."""
    for child in root.named_children:
        if child.type in _CLASS_TYPES:
            yield child
        elif child.type in _NAMESPACE_TYPES:
            body = child.child_by_field_name("body")
            yield from iter_type_declarations(body if body is not None else child)


class CSharpParser(BaseParser):
    name = "csharp"
    extensions = (".cs",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.text_truncation_limit

        internal, external, _, bindings = extract_imports(root, source)
        resolve = make_resolver(
            bindings, defined_names(root, source), path, type_map(root, source)
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for decl in iter_type_declarations(root):
            cls, methods, cls_statements = build_class(
                decl, source, path,
                parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
            )
            classes.append(cls)
            functions.extend(methods)
            statements.extend(cls_statements)

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="csharp",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
