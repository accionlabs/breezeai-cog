"""CSharpParser — extracts one ``.cs`` file into a FileRecord. C# is Java-like: all
declarations live in classes/structs/etc., which in turn live in namespaces (block or
file-scoped), so extraction recurses through namespaces to reach the type declarations,
then emits imports + classes (with their flat methods and statements).

``build_index`` maps each declared type to its file (``Namespace.TypeName → path``);
because C# ``using`` names a namespace (not a file), cross-file edges resolve from the
*referenced type names* rather than the imports themselves (see ``imports.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..additive import run_additive
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..treesitter import parse_source
from .classes import build_class
from .functions import defined_names, type_map
from .imports import (
    CSharpIndex,
    bind_static_import_members,
    build_csharp_index,
    extract_imports,
)
from ..comments_common import comment_statements_for
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES

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
    extensions: tuple[str, ...] = (".cs",)  # widened by WebFormsParser (adds .aspx/.ascx/.master)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> CSharpIndex:
        """Repo-level pre-pass: map each declared type ``Namespace.TypeName`` → repo path."""
        return build_csharp_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.resolution_index
        )
        local_names = defined_names(root, source)
        # A `using static Ns.Type` puts Type's members in scope unqualified, so a bare
        # `M()` needs them bound; `local_names` keeps a same-file declaration winning.
        bind_static_import_members(
            root, source, bindings, local_names,
            ctx.resolution_index if isinstance(ctx.resolution_index, CSharpIndex) else None,
        )
        resolve = make_resolver(
            bindings, local_names, path, type_map(root, source),
            ext_index=getattr(ctx.resolution_index, "ext_methods", None),
            heritage=getattr(ctx.resolution_index, "class_heritage", None),
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for decl in iter_type_declarations(root):
            cls_list, methods, cls_statements = build_class(
                decl, source, path,
                parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
            )
            classes.extend(cls_list)
            functions.extend(methods)
            statements.extend(cls_statements)

        if capture:
            statements.extend(
                comment_statements_for(
                    root, source, path, file_id=fid, functions=functions, classes=classes,
                    statements=statements, control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES, limit=limit, seen_ids=seen_ids,
                )
            )

        record = FileRecord(
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
        # Additive detectors (parsers/additive.py) — layered on top of base + any framework
        # subclass's extraction (a Lambda handler or a Lucene read co-exists with an ASP.NET
        # controller in the same file rather than displacing it).
        run_additive("csharp", root, ctx, record, is_fixture=self.is_fixture_file(path))
        return record
