"""JavaParser — extracts one .java file into a FileRecord. Java has no top-level
functions (all declarations live in classes), so extraction is imports + classes
(with their flat methods and statements). ``build_index`` builds the FQCN map for
import resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import parse_source
from ..callresolve import make_resolver
from .classes import build_class
from .functions import defined_names, type_map
from .imports import FqcnIndex, build_fqcn_index, extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration")


class JavaParser(BaseParser):
    name = "java"
    extensions = (".java",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> FqcnIndex:
        """Repo-level pre-pass: map each file's package.ClassName → repo path (FQCN)."""
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.text_truncation_limit

        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.repo_root, ctx.resolution_index
        )
        resolve = make_resolver(  # calls[].path (Tiers 1+2 + receiver-type Phase 2)
            bindings, defined_names(root, source), path, type_map(root, source)
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for child in root.named_children:
            if child.type in _CLASS_TYPES:
                cls, methods, cls_statements = build_class(
                    child, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                classes.append(cls)
                functions.extend(methods)
                statements.extend(cls_statements)

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="java",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
