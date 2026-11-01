"""CppParser — extracts one C++ translation unit into a FileRecord.

C++ is a **best-effort** language: real files routinely parse with ``has_error`` (macros
such as ``Q_OBJECT``, unexpanded preprocessor tokens, template metaprogramming), so every
declaration is guarded by :func:`.functions.has_declaration_error` and a corrupt header is
skipped rather than turned into a fabricated node (absent beats wrong).

Structure captured:
* ``#include`` → imports (local ``"x.h"`` resolved to a repo file, ``<system>`` external);
* ``class_specifier`` / ``struct_specifier`` → a ``Class`` (with heritage and flat member
  functions); nested classes parent to their enclosing class;
* ``function_definition`` at file / namespace scope → a free function, or — when its
  declarator is a ``qualified_identifier`` (``Judge::decide``) — an out-of-class **method**
  definition attached to that class by name (else parented to the file, honest-null);
* ``namespace_definition`` is flattened — its members parent to the file (there is no
  namespace node type in the schema).

C shares this extraction on the C grammar (``parsers/c``), and the two share ``.h``: the
ownership step decides each header's language from the evidence in ``ownership.py``. C++ reads
the C index after its own, so a call or ``#include`` from C++ resolves into C files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..additive import run_additive
from ..base import BaseParser, Ownership, ParseContext
from ..treesitter import node_text, parse_source
from .classes import (
    _CLASS_TYPES,
    TYPE_ALIAS_TYPES,
    _declarator_name,
    _unwrap_template,
    build_class,
    build_enum,
    type_alias_statement,
)
from .functions import (
    build_function,
    defined_names,
    function_declarator_of,
    has_declaration_error,
    is_function_pointer,
)
from .imports import FamilyHeaders, extract_imports
from .index import CppIndex, _join_ns, _ns_name, build_cpp_index, make_cpp_resolver
from .ownership import cpp_only_syntax, include_targets
from ..comments_common import comment_statements_for
from ..statements_common import member_statement
from .mappings import (
    COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, MACRO_TYPES, STATEMENT_TYPES, TRANSPARENT_SCOPES,
)


def _is_include_guard(node: Node, source: bytes) -> bool:
    """``#define LIST_H`` with no value, directly inside ``#ifndef LIST_H``: the guard every
    header has, which says nothing about the code."""
    parent, name = node.parent, node.child_by_field_name("name")
    if node.type != "preproc_def" or node.child_by_field_name("value") is not None:
        return False
    if parent is None or parent.type != "preproc_ifdef" or name is None:
        return False
    guard = parent.child_by_field_name("name")
    return (
        parent.children[0].type == "#ifndef"
        and guard is not None
        and node_text(guard, source) == node_text(name, source)
    )


class CppParser(BaseParser):
    name = "cpp"
    #: The tree-sitter grammar this language's files are parsed with.
    grammar = "cpp"
    extensions: tuple[str, ...] = (
        ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h", ".hh", ".hxx", ".ipp", ".inl",
    )
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS
    related_languages = ("c",)
    ownership = Ownership(
        proves=cpp_only_syntax, references=include_targets, includes=("c",), default_for=(".h",),
    )

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> CppIndex:
        """Repo-wide index: the ``#include`` header map plus a parse pre-pass mapping
        free-function / ``Scope::method`` **definitions** → path (for cross-file call
        resolution). Honest-null on any name shared by >1 file."""
        return build_cpp_index(Path(repo_root), files, jobs, self.grammar)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source(self.grammar, ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def callable_in(self, related: CppIndex) -> dict[str, str | None]:
        """The free functions of a related language's index this language can call: C++ can
        call every C function."""
        return related.funcs

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        run_additive("cpp", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index if isinstance(ctx.resolution_index, CppIndex) else None
        related = [r for r in ctx.related_indexes.values() if isinstance(r, CppIndex)]
        headers = FamilyHeaders(idx.headers if idx else {}, [r.headers for r in related])
        internal, external, _, _ = extract_imports(root, source, headers)
        resolve = make_cpp_resolver(
            defined_names(root, source), path, idx, [self.callable_in(r) for r in related],
        )

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        class_map: dict[str, str] = {}  # simple class name → class id (out-of-class method attach)

        def process(scope: Node, ns: str = "") -> None:
            for child in scope.named_children:
                node = _unwrap_template(child)
                if node.type in TRANSPARENT_SCOPES:
                    process(node, ns)  # include guard / preproc conditional / extern "C" — recurse
                elif has_declaration_error(node):
                    continue  # corrupt header — skip rather than emit fabricated data
                elif node.type == "namespace_definition":
                    body = node.child_by_field_name("body")
                    if body is not None:  # flatten members to the file, but track the ns path
                        process(body, _join_ns(ns, _ns_name(node, source)))
                elif node.type in _CLASS_TYPES:
                    cls_list, methods, cls_stmts = build_class(
                        node, source, path,
                        parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                    )
                    classes.extend(cls_list)
                    functions.extend(methods)
                    statements.extend(cls_stmts)
                    for c in cls_list:
                        class_map.setdefault(c.name, c.id)
                elif node.type == "enum_specifier":
                    enum_cls, enum_stmts = build_enum(
                        node, source, path, parent_id=fid, seen_ids=seen_ids,
                        capture=capture, limit=limit,
                    )
                    if enum_cls is not None:
                        classes.append(enum_cls)
                        statements.extend(enum_stmts)
                elif node.type == "function_definition":
                    _emit_function(node, ns)
                elif node.type in MACRO_TYPES and capture and not _is_include_guard(node, source):
                    name = node.child_by_field_name("name")
                    statements.append(
                        member_statement(
                            node, source, path, parent_id=fid, limit=limit, seen_ids=seen_ids,
                            name=node_text(name, source) if name is not None else None,
                        )
                    )
                elif node.type in TYPE_ALIAS_TYPES and capture:
                    # `typedef X Y;` / `using Y = X;` at file or namespace scope. C++ carries
                    # much of its vocabulary in typedefs, and the aliased type stays on the
                    # statement's `text`. `using namespace …` / `using Ns::name` are a
                    # `using_declaration` (an import) and are not in TYPE_ALIAS_TYPES.
                    statements.append(
                        type_alias_statement(
                            node, source, path, parent_id=fid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                elif node.type == "declaration" and capture:
                    # A file-/namespace-scope variable or constant (`const int kMax = 5;`,
                    # `constexpr char kName[] = "x";`). C++ puts much of its controlled
                    # vocabulary here rather than in a class, and these are otherwise
                    # captured nowhere, so emit each as a flat statement (its `text` — incl.
                    # any `= value` — is queryable). The grammar spells a *function
                    # prototype* with this same node type, so a `function_declarator` marks
                    # a declaration that is not a value; skip those (a defined function is a
                    # `function_definition`, handled above). Namespace members parent to the
                    # file, matching how `process` already flattens namespaces.
                    declarator = node.child_by_field_name("declarator")
                    if function_declarator_of(declarator) is None or is_function_pointer(
                        declarator
                    ):  # a variable, incl. a pointer to a function (`int (*cmp)(int, int);`)
                        statements.append(
                            member_statement(
                                node, source, path, parent_id=fid, limit=limit, seen_ids=seen_ids,
                                name=_declarator_name(
                                    node.child_by_field_name("declarator"), source
                                ),
                            )
                        )

        def _emit_function(node: Node, ns: str) -> None:
            fd = function_declarator_of(node.child_by_field_name("declarator"))
            inner = fd.child_by_field_name("declarator") if fd is not None else None
            if inner is None:
                return  # not a recognizable function — do not fabricate
            if inner.type == "qualified_identifier":
                # `A::B::Cls::method` — the class is everything but the last component. The
                # class's fully-qualified name is the enclosing namespace path plus that prefix,
                # so a definition binds only to a class whose whole path matches.
                parts = [p for p in node_text(inner, source).split("::") if p]
                class_simple = parts[-2] if len(parts) >= 2 else None
                class_fqn = _join_ns(ns, "::".join(parts[:-1])) if len(parts) >= 2 else None
                cid = _class_for(class_fqn, class_simple)
                fn, fn_stmts = build_function(
                    node, source, path,
                    parent_id=cid or fid, seen_ids=seen_ids, capture=capture, limit=limit,
                    resolve=resolve, class_name=class_simple, kind="method",
                )
            else:
                fn, fn_stmts = build_function(
                    node, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
            if not fn.name:
                return  # nameless (unparseable declarator) — skip
            functions.append(fn)
            statements.extend(fn_stmts)

        def _class_for(class_fqn: str | None, class_simple: str | None) -> str | None:
            """The class-node id an out-of-class definition attaches to. A class in *this* file
            wins by its same-file id (matched on the simple name). Otherwise the repo index maps
            the class's **fully-qualified** name → the declaring class node's id — so a definition
            binds only when the whole namespace path matches, never to a same-named class in
            another namespace or one outside the repo. ``None`` when unresolved — honest-null."""
            if class_simple:
                same_file = class_map.get(class_simple.split("<", 1)[0])
                if same_file is not None:
                    return same_file
            if idx is None or class_fqn is None:
                return None
            return idx.classdecl.get(class_fqn)  # exact node id, or None when ambiguous/unknown

        process(root)

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
            language=self.name,
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        self.run_detectors(root, ctx, record)
        return record
