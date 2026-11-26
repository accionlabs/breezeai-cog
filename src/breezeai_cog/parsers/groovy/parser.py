"""GroovyParser — extracts one .groovy file into a FileRecord.

Groovy structurally mirrors Java (package/import/class/method/field/enum), so this
parser follows the Java parser's model: imports + classes (with flat methods and
statements), a repo-level FQCN ``build_index`` for import resolution, and the same
receiver-type call resolver. Unlike Java, Groovy also allows **top-level (script) code**
outside any class: top-level *methods* are extracted as functions parented to the file,
and top-level *statements* (module-level declarations, bare calls, control flow) are
captured and parented to the file — mirroring the Python/TypeScript parsers.

Groovy is a **best-effort / second-tier** language: the dekobon grammar recovers the
package/class/method/field/enum skeleton reliably, but some method-body and enum shapes
still mis-parse (README's Groovy note lists them).
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..additive import run_additive
from ..callresolve import make_resolver
from ..index_common import ClassHeritage
from ..treesitter import parse_source
from ..statements_common import begin_datastore_vendor, end_datastore_vendor, set_datastore_vendor
from .classes import build_class
from .functions import build_function, defined_names, has_declaration_error, type_map
from .heritage import class_method_names
from .imports import GroovyIndex, build_fqcn_index, extract_imports, import_aliases
from ..java.imports import JavaIndex
from ..jvm_related import Overlay, file_heritage, jvm_view
from ..comments_common import comment_statements_for
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements

_CLASS_TYPES = (
    "class_declaration", "interface_declaration", "enum_declaration", "trait_declaration",
)


class GroovyParser(BaseParser):
    name = "groovy"
    extensions = (".groovy",)
    related_languages = ("java",)  # Groovy imports, calls and extends the repo's Java classes
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> GroovyIndex:
        """Repo-level pre-pass (one parse per file): FQCN → path map for imports + a
        ``Class.FIELD → value`` constant map for address folding."""
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("groovy", ctx.source, ctx.parse_timeout_micros).root_node
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
        view = (jvm_view(idx, ctx.related_indexes.get("java"), JavaIndex, ".java")
                if isinstance(idx, GroovyIndex) else None)
        fqcn = view.fqcn if view is not None else idx  # bare FqcnIndex/None still ok
        internal, external, _, bindings = extract_imports(
            root, source, path, ctx.repo_root, fqcn
        )
        set_datastore_vendor(external)
        heritage: Mapping[str, ClassHeritage | None] | None = None
        unknown: Mapping[tuple[str, str], frozenset[str] | None] | None = None
        if view is not None:  # `import a.Foo as Bar`: `Bar.m()` is looked up as `Foo`
            renamed = {a: real for a, real in import_aliases(root, source).items()
                       if real in view.heritage}
            heritage = file_heritage(view, bindings, renamed)
            unknown = view.unknown_methods
            unknown_aliases = {(bindings[a], a): unknown.get((bindings[a], real))
                               for a, real in renamed.items()
                               if a in bindings and (bindings[a], real) in unknown}
            if unknown_aliases:
                unknown = Overlay(unknown_aliases, view.unknown_methods)
        owner_defs = class_method_names(root, source)
        local_defs = defined_names(root, source)
        shared = make_resolver(  # calls[].path: imports, same file, receiver type (declared only)
            bindings, local_defs, path, type_map(root, source),
            heritage=heritage,
            declared_only=True,
            owner_defs=owner_defs,
            unknown_methods=unknown,
        )

        def resolve(name: str, receiver: str | None, owner: str | None = None,
                    scope: Mapping[str, str | None] | None = None) -> str | None:
            """A bare or ``this`` call in a class: the class's own methods first — its index
            entry holds its generated accessors and its traits' methods, each with the file that
            declares it — then the shared tiers, then (bare) the inherited methods. A bare call
            whose name is both statically imported and declared by the class is left empty: Groovy
            picks between them by the arguments."""
            if receiver is None and name in bindings and name in (
                    owner_defs[owner] if owner in owner_defs else local_defs):
                return None  # (a class with a parse error is not in owner_defs: the whole file)
            if owner is not None and receiver in (None, "this") and heritage is not None:
                own = heritage.get(owner)
                if own is not None and name in own.methods:
                    return own.methods[name]
            found = shared(name, receiver, owner, scope)
            if found is None and receiver is None and owner is not None:
                found = shared(name, "this", owner, scope)  # `foo()` is `this.foo()`
            return found
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

        # file-scope (script-body) statements — parented to the file, like Python/TypeScript.
        # descend_all defaults to False, so class/method/closure bodies are barriers here
        # (their statements are captured by build_class / build_method) — no double capture.
        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids)
        )

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
            language="groovy",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        # Additive detectors (parsers/additive.py) — layered on top of the base extraction.
        run_additive("groovy", root, ctx, record, is_fixture=self.is_fixture_file(path))
        return record
