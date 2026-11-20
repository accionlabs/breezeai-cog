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
from ..additive import run_additive
from ..base import BaseParser, ParseContext
from ..treesitter import parse_source
from ..callresolve import make_resolver
from ..statements_common import begin_datastore_vendor, end_datastore_vendor, set_datastore_vendor
from .anonymous import type_kinds
from .classes import build_class
from .functions import class_method_names, defined_names, type_map
from .imports import JavaIndex, build_fqcn_index, extract_imports
from ..groovy.imports import GroovyIndex
from ..jvm_related import file_heritage, jvm_view
from ..comments_common import comment_statements_for
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration")

#: JDK interfaces that declare no fields. Inside an anonymous class of one of these
#: (`new Runnable() { … }`), a bare name the class does not declare is a field of the enclosing
#: class — inherited ones included — exactly as outside it. Any other anonymous type (a
#: framework's handler interface included) may declare a field of that name itself, so the call
#: is left to the other rules there.
_FIELDLESS_INTERFACES = frozenset({
    "java.lang.Runnable",
    "java.util.concurrent.Callable",
})


def fieldless_types(external: list[str]) -> frozenset[str]:
    """The simple names that mean a field-less interface in a file with these external imports:
    a single-type import of it, its package imported on demand, or ``java.lang`` — unless a
    single-type import binds the simple name to another class."""
    single = {imp.rsplit(".", 1)[-1]: imp for imp in external if not imp.endswith(".*")}
    out: set[str] = set()
    for fqcn in _FIELDLESS_INTERFACES:
        package, simple = fqcn.rsplit(".", 1)
        bound = single.get(simple)
        if bound == fqcn or (bound is None and (package == "java.lang"
                                                or f"{package}.*" in external)):
            out.add(simple)
    return frozenset(out)


class JavaParser(BaseParser):
    name = "java"
    extensions = (".java",)
    # Java imports, calls and extends the repo's Groovy classes; Kotlin's index only gives the
    # MCP server types it declares (jvm_mcp_common.index_types)
    related_languages = ("groovy", "kotlin")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> JavaIndex:
        """Repo-level pre-pass (one parse per file): FQCN → path map for imports + a
        ``Class.FIELD → value`` constant map for address folding."""
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
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

        idx = ctx.resolution_index
        view = (jvm_view(idx, ctx.related_indexes.get("groovy"), GroovyIndex, ".groovy")
                if isinstance(idx, JavaIndex) else None)
        fqcn = view.fqcn if view is not None else idx  # bare FqcnIndex/None still ok
        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.repo_root, fqcn
        )
        set_datastore_vendor(external)
        resolve = make_resolver(  # calls[].path (Tiers 1+2, receiver type, inherited methods)
            bindings, defined_names(root, source), path, type_map(root, source),
            heritage=file_heritage(view, bindings, {}) if view is not None else None,
            declared_only=True,
            owner_defs=class_method_names(root, source),
            unknown_methods=view.unknown_methods if view is not None else None,
            inherited_fields=True,
            fieldless_types=fieldless_types(external),
            nested_types=True,
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        kind_of = type_kinds(root, source, idx.type_kinds if isinstance(idx, JavaIndex) else None)

        for child in root.named_children:
            if child.type in _CLASS_TYPES:
                cls_list, methods, cls_statements = build_class(
                    child, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                    kind_of=kind_of,
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
            language="java",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        # Additive detectors (parsers/additive.py) — layered on top of the base extraction, before
        # any framework subclass adds its own (a Quartz job co-exists with a Spring controller).
        run_additive("java", root, ctx, record, is_fixture=self.is_fixture_file(path))
        return record
