"""ApexParser — extracts one ``.cls`` or ``.trigger`` file into a FileRecord.

Apex is Java-shaped but its grammar differs in ways that rule out reusing the Java
helpers (wrapped modifier keywords, single-quoted strings, ``key=value`` annotation
arguments, case-insensitive identifiers, no imports). Classes, interfaces, enums and
their members are extracted as in Java; a trigger becomes one ``Function`` of type
``trigger``. ``build_index`` maps class names to files so calls resolve across files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from .classes import build_class
from .detect import reset_var_types, set_var_types
from .functions import build_trigger, defined_names, type_map
from .index import ApexIndex, build_class_index
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES, TYPE_DECLARATIONS


class ApexParser(BaseParser):
    name = "apex"
    extensions = (".cls", ".trigger")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def claims(self, path: str, source: bytes) -> bool:
        """``.cls`` is also the extension of VB6 class modules; those are not Apex."""
        head = source.lstrip()
        return not (head.startswith(b"VERSION ") and b"Attribute VB_Name" in source)

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ApexIndex:
        return build_class_index(Path(repo_root), files)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("apex", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        token = set_var_types(type_map(root, ctx.source))
        try:
            return self._extract(root, ctx)
        finally:
            reset_var_types(token)

    def _extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        index = ctx.resolution_index if isinstance(ctx.resolution_index, dict) else {}
        resolve = make_resolver(index, defined_names(root, source), path, type_map(root, source))

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for child in root.named_children:
            if child.type in TYPE_DECLARATIONS:
                cls_list, methods, cls_statements = build_class(
                    child, source, path, parent_id=fid, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            elif child.type == "trigger_declaration":
                fn, fn_statements = build_trigger(
                    child, source, path, parent_id=fid, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                functions.append(fn)
                statements.extend(fn_statements)

        if capture:
            statements.extend(
                comment_statements_for(
                    root, source, path, file_id=fid, functions=functions, classes=classes,
                    statements=statements, control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES, limit=limit, seen_ids=seen_ids,
                )
            )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="apex",
            loc=count_loc(source.decode("utf-8", "replace")),
            functions=functions,
            classes=classes,
            statements=statements,
        )
