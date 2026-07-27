"""GroovyParser — extracts one .groovy file into a FileRecord.

Groovy structurally mirrors Java (package/import/class/method/field/enum), so this
parser follows the Java parser's model: imports + classes (with flat methods and
statements), a repo-level FQCN ``build_index`` for import resolution, and the same
receiver-type call resolver. Unlike Java, Groovy also allows **top-level (script)
methods** outside any class, which are extracted as functions parented to the file.

Groovy is a **best-effort / second-tier** language: the dekobon grammar recovers the
package/class/method/field/enum skeleton reliably, but degrades expression bodies with
named-argument commas, parenthesised enum constants, and heavy GString/DSL use to
*missing* nodes (never wrong). See ``.todo/groovy-grammar-evaluation.md``.
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
from ..treesitter import parse_source
from .classes import build_class
from .functions import build_function, defined_names, has_declaration_error, type_map
from .imports import FqcnIndex, build_fqcn_index, extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = (
    "class_declaration", "interface_declaration", "enum_declaration", "trait_declaration",
)


class GroovyParser(BaseParser):
    name = "groovy"
    extensions = (".groovy",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> FqcnIndex:
        """Repo-level pre-pass: map each file's package.TypeName → repo path (FQCN)."""
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("groovy", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.text_truncation_limit

        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.repo_root, ctx.resolution_index
        )
        resolve = make_resolver(
            bindings, defined_names(root, source), path, type_map(root, source)
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for child in root.named_children:
            if has_declaration_error(child):
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if child.type in _CLASS_TYPES:
                cls_list, methods, cls_statements = build_class(
                    child, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            elif child.type == "method_declaration":  # top-level script method
                fn, fn_statements = build_function(
                    child, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                functions.append(fn)
                statements.extend(fn_statements)

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="groovy",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
