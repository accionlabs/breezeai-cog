"""RubyParser — extracts one .rb file into a FileRecord."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import CallResolver, make_resolver
from ..treesitter import parse_source
from .classes import build_class, iter_definitions
from .functions import build_function, defined_names
from .imports import extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .models import active_record_model_names, build_active_record_model_index
from .statement import extract_statements


class RubyParser(BaseParser):
    name = "ruby"
    extensions = (".rb",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
        return True

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> frozenset[str]:
        return build_active_record_model_index(repo_root, files, jobs)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit
        indexed_models = (
            ctx.resolution_index
            if isinstance(ctx.resolution_index, frozenset)
            else frozenset()
        )
        model_names = indexed_models | active_record_model_names(root, source)

        internal, external, exports, bindings = extract_imports(root, source, path, ctx.repo_root)

        def resolve_for_scope(scope: Node) -> CallResolver:
            return make_resolver(bindings, defined_names(scope, source), path)

        resolve = resolve_for_scope(root)

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for node in iter_definitions(root):
            if node.type in {"class", "module"}:
                cls_list, methods, cls_stmts = build_class(
                    node,
                    source,
                    path,
                    parent_id=fid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve_for_scope=resolve_for_scope,
                    model_names=model_names,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_stmts)
            elif node.type == "method":
                fns, fn_stmts = build_function(
                    node,
                    source,
                    path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                functions.extend(fns)
                statements.extend(fn_stmts)

        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids)
        )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="ruby",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,
        )
