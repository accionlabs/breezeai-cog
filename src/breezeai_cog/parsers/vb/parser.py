"""VbParser — extracts one ``.vb`` file into a FileRecord. VB types (Class/Module/…)
live inside ``type_declaration`` wrappers under ``namespace_block``s; leading
attributes detach as sibling ``attribute_block`` nodes, so the walker accumulates them
and hands them to the following type. Like C#, imports name namespaces (not files), so
calls resolve same-file only; ``build_index`` indexes only class heritage (base + attrs)
for cross-file base-controller route/auth resolution (ASP.NET)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..statements_common import begin_datastore_vendor, end_datastore_vendor, set_datastore_vendor
from .classes import build_class
from .functions import defined_names, type_map
from .imports import VbIndex, build_vb_index, extract_imports
from .source import parse_nested, parse_vb
from ..comments_common import comment_statements_for
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = (
    "class_block", "interface_block", "enum_block",
    "struct_block", "structure_block", "module_block",
)


def iter_type_declarations(container: Node):
    """Yield ``(block_node, [leading attribute_block nodes])`` for every type, descending
    through namespaces. Leading attributes detach from the type in the VB grammar."""
    pending: list[Node] = []
    for child in container.named_children:
        if child.type == "attribute_block":
            pending.append(child)
            continue
        if child.type == "namespace_block":
            yield from iter_type_declarations(child)
            pending = []
            continue
        block = child
        if child.type == "type_declaration":
            block = next((c for c in child.named_children if c.type in _CLASS_TYPES), None)
        if block is not None and block.type in _CLASS_TYPES:
            yield block, pending
        pending = []


class VbParser(BaseParser):
    name = "vb"
    extensions = (".vb",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> VbIndex:
        """Repo-level pre-pass: simple class name → heritage (base + attributes), for
        cross-file base-controller route/auth resolution (ASP.NET). No namespace→file map
        (VB imports name namespaces); heritage only."""
        return build_vb_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_vb(ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        # The ORM this file's imports name, for the shared db-call classifier — set by _extract
        # once the imports are read, reset in a finally so a reused worker never leaks it.
        token = begin_datastore_vendor()
        try:
            return self._extract(root, ctx)
        finally:
            end_datastore_vendor(token)

    def _extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        internal, external, _, bindings = extract_imports(root, source)
        set_datastore_vendor(external)
        # Types nested in another type, one parse per nesting level (see .source).
        nested = parse_nested(source, ctx.parse_timeout_micros)
        trees = [root, *(level.root for level in nested)]
        names: set[str] = set()
        types: dict[str, str] = {}
        for tree in trees:
            names |= defined_names(tree, source)
            for k, v in type_map(tree, source).items():
                types.setdefault(k, v)
        resolve = make_resolver(bindings, names, path, types)
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        declared = [(block, attrs, None) for block, attrs in iter_type_declarations(root)]
        for level in nested:
            declared += [(block, attrs, level.owner(block))
                         for block, attrs in iter_type_declarations(level.root)]
        for block, attrs, owner in declared:
            cls, methods, cls_statements = build_class(
                block, source, path,
                parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit,
                pending_attrs=attrs, resolve=resolve, owner=owner,
            )
            classes.append(cls)
            functions.extend(methods)
            statements.extend(cls_statements)

        if capture:
            for tree in trees:
                statements.extend(
                    comment_statements_for(
                        tree, source, path, file_id=fid, functions=functions, classes=classes,
                        statements=statements, control_flow=CONTROL_FLOW,
                        comment_types=COMMENT_TYPES, limit=limit, seen_ids=seen_ids,
                    )
                )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="vb",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
