"""PhpParser — extracts one PHP source file from a single tree-sitter parse."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, Class, FileRecord, Function, Statement
from ...utils import count_loc
from ..additive import run_additive
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..comments_common import comment_statements_for
from ..statements_common import emit_enum_members
from ..treesitter import node_text, parse_source
from .classes import build_class
from .functions import build_function, function_node_type, function_parent_id
from .imports import extract_imports
from .index import PhpIndex, build_fqcn_index
from .mappings import COMMENT_TYPES, CONTROL_FLOW, STATEMENT_TYPES
from .statements import extract_statements

_CLASS_TYPES = {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}
_FUNCTION_NAMES = {"function_definition", "method_declaration"}


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _defined_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    for node in _walk(root):
        if node.type in _CLASS_TYPES | _FUNCTION_NAMES:
            name = node.child_by_field_name("name")
            if name is not None:
                names.add(node_text(name, source))
    return names


class PhpParser(BaseParser):
    name = "php"
    extensions = (".php", ".phtml")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks: list[str] = []

    def matches(self, path: str | Path) -> bool:
        value = str(path).lower().replace("\\", "/")
        return not value.endswith(".blade.php") and super().matches(path)

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PhpIndex:
        return build_fqcn_index(repo_root, files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def link_handlers(self, record: FileRecord, ctx: ParseContext) -> None:
        """Resolve PHP handlers only when the declaration is directly verifiable.

        PHP route syntax commonly names a controller without carrying its declaration
        location.  The registration line is not that location, so leave the line empty
        until a same-file method or an unambiguous indexed class file is found.
        """
        if not ctx.capture_statements:
            return
        index = ctx.resolution_index if isinstance(ctx.resolution_index, PhpIndex) else None
        for statement in record.statements:
            if statement.semanticType not in {"route", "eventbus_consumer", "timer"}:
                continue
            if not statement.handler or statement.handlerLine is not None or statement.handlerPath is not None:
                continue
            handler = statement.handler
            if "@" in handler:
                class_name, method_name = handler.split("@", 1)
            else:
                class_name, method_name = handler, "__invoke"
            simple_class = class_name.lstrip("\\").rsplit("\\", 1)[-1]
            same_file_classes = {
                cls.id: cls for cls in record.classes
                if cls.name.lower() == simple_class.lower()
            }
            same_file_methods = [
                fn for fn in record.functions
                if fn.name.lower() == method_name.lower()
                and (not same_file_classes or fn.parentId in same_file_classes)
            ]
            if len(same_file_methods) == 1:
                statement.handlerLine = same_file_methods[0].startLine
                continue
            if index is None:
                continue
            candidates: set[str] = set()
            wanted = class_name.lstrip("\\").lower()
            for fqcn, indexed_path in index.fqcn.items():
                if indexed_path is None:
                    continue
                normalized = fqcn.lstrip("\\").lower()
                if normalized == wanted or normalized.rsplit("\\", 1)[-1] == simple_class.lower():
                    candidates.add(indexed_path)
            if len(candidates) == 1 and next(iter(candidates)) != ctx.path:
                statement.handlerPath = next(iter(candidates))

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        index = ctx.resolution_index if isinstance(ctx.resolution_index, PhpIndex) else None
        internal, external, exports, bindings = extract_imports(
            root, source, path, ctx.repo_root, index
        )
        resolve = make_resolver(bindings, _defined_names(root, source), path)
        classes: list[Class] = []
        functions: list[Function] = []
        statements: list[Statement] = []
        class_ids: dict[tuple[int, int], str] = {}
        class_names: dict[tuple[int, int], str] = {}
        function_ids: dict[tuple[int, int], str] = {}

        def extract_scope(node: Node) -> None:
            if node.type in _CLASS_TYPES:
                parent_id = function_parent_id(node, class_ids, function_ids, fid)
                cls = build_class(
                    node, source, path, parent_id=parent_id, seen_ids=seen_ids
                )
                span = (node.start_byte, node.end_byte)
                class_ids[span] = cls.id
                class_names[span] = cls.name
                classes.append(cls)
                body = node.child_by_field_name("body")
                if body is not None:
                    statements.extend(
                        extract_statements(
                            body, source, path, parent_id=cls.id,
                            capture=ctx.capture_statements, limit=ctx.statement_text_limit,
                            seen_ids=seen_ids,
                        )
                    )
                for child in node.named_children:
                    extract_scope(child)
                return

            if function_node_type(node):
                parent_id = function_parent_id(node, class_ids, function_ids, fid)
                owner = node.parent
                class_name: str | None = None
                while owner is not None:
                    span = (owner.start_byte, owner.end_byte)
                    if span in class_names:
                        class_name = class_names[span]
                        break
                    owner = owner.parent
                fn, fn_statements = build_function(
                    node,
                    source,
                    path,
                    parent_id=parent_id,
                    class_name=class_name,
                    seen_ids=seen_ids,
                    capture=ctx.capture_statements,
                    limit=ctx.statement_text_limit,
                    resolve=resolve,
                )
                function_ids[(node.start_byte, node.end_byte)] = fn.id
                functions.append(fn)
                statements.extend(fn_statements)
                for child in node.named_children:
                    extract_scope(child)
                return

            for child in node.named_children:
                extract_scope(child)

        extract_scope(root)
        if ctx.capture_statements:
            for enum_node in (node for node in _walk(root) if node.type == "enum_declaration"):
                body = enum_node.child_by_field_name("body")
                enum_id = class_ids.get((enum_node.start_byte, enum_node.end_byte))
                if body is not None and enum_id is not None:
                    statements.extend(
                        emit_enum_members(
                            body,
                            source,
                            path,
                            member_types={"enum_case"},
                            parent_id=enum_id,
                            limit=ctx.statement_text_limit,
                            seen_ids=seen_ids,
                        )
                    )
        statements.extend(
            extract_statements(
                root, source, path, parent_id=fid, capture=ctx.capture_statements,
                limit=ctx.statement_text_limit, seen_ids=seen_ids,
            )
        )
        if ctx.capture_statements:
            statements.extend(
                comment_statements_for(
                    root, source, path, file_id=fid, functions=functions, classes=classes,
                    statements=statements, control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES, limit=ctx.statement_text_limit,
                    seen_ids=seen_ids,
                )
            )

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="php",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        run_additive(
            "php", root, ctx, record, is_fixture=self.is_fixture_file(path)
        )
        return record
