"""ScalaParser — extracts one .scala or .sc file into a FileRecord.

Extracts classes (class, trait, object, case class, enum), functions/methods
(with object members correctly marked as isStatic=True and captured), flat statements,
and comments, with FQCN resolution via build_index.
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
from .functions import build_function, defined_names, type_map
from .imports import ScalaIndex, build_fqcn_index, extract_imports
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements

_CLASS_TYPES = ("class_definition", "object_definition", "trait_definition", "enum_definition")
_FN_TYPES = ("function_definition", "function_declaration")


class ScalaParser(BaseParser):
    name = "scala"
    extensions = (".scala", ".sc")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ScalaIndex:
        """Repo-level pre-pass (one parse per file): FQCN → path map for imports."""
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("scala", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index
        fqcn = idx.fqcn if isinstance(idx, ScalaIndex) else idx
        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.repo_root, fqcn
        )
        resolve = make_resolver(
            bindings, defined_names(root, source), path, type_map(root, source)
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        def process_node(child: Node) -> None:
            if child.type in _CLASS_TYPES:
                cls_list, methods, cls_statements = build_class(
                    child,
                    source,
                    path,
                    parent_id=fid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            elif child.type in _FN_TYPES:
                fn, fn_statements = build_function(
                    child,
                    source,
                    path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    is_static=False,
                    fn_type="function",
                    resolve=resolve,
                )
                functions.append(fn)
                statements.extend(fn_statements)
            elif child.type == "extension_definition":
                for member in child.named_children:
                    if member.type in _FN_TYPES:
                        fn, fn_statements = build_function(
                            member,
                            source,
                            path,
                            parent_id=fid,
                            class_name=None,
                            seen_ids=seen_ids,
                            capture=capture,
                            limit=limit,
                            is_static=False,
                            fn_type="function",
                            resolve=resolve,
                        )
                        functions.append(fn)
                        statements.extend(fn_statements)
            elif child.type == "package_clause":
                for sub in child.named_children:
                    process_node(sub)

        for child in root.named_children:
            process_node(child)

        # File-scope statements (scripts / top-level statements)
        statements.extend(
            extract_statements(
                root,
                source,
                path,
                parent_id=fid,
                capture=capture,
                limit=limit,
                seen_ids=seen_ids,
            )
        )

        if capture:
            statements.extend(
                comment_statements_for(
                    root,
                    source,
                    path,
                    file_id=fid,
                    functions=functions,
                    classes=classes,
                    statements=statements,
                    control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES,
                    limit=limit,
                    seen_ids=seen_ids,
                )
            )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="scala",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
