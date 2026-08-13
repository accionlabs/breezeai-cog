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
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import node_text, parse_source
from .classes import _CLASS_TYPES, _unwrap_template, build_class, build_enum
from .functions import (
    build_function,
    defined_names,
    function_declarator_of,
    has_declaration_error,
)
from .imports import extract_imports
from .index import CppIndex, _join_ns, _ns_name, build_cpp_index, make_cpp_resolver
from .mappings import FRAMEWORKS, STATEMENT_TYPES

#: Transparent wrappers whose *members* are top-level declarations: the ``#ifndef GUARD``
#: include guard every header uses (``preproc_ifdef``), other preprocessor conditionals, and
#: ``extern "C" { … }`` (``linkage_specification``). ``process`` recurses through these so a
#: class/function nested under an include guard isn't missed.
_TRANSPARENT_SCOPES = frozenset({
    "preproc_ifdef", "preproc_if", "preproc_ifndef", "preproc_else", "preproc_elif",
    "linkage_specification",
})


class CppParser(BaseParser):
    name = "cpp"
    extensions = (
        ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h", ".hh", ".hxx", ".ipp", ".inl",
    )
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> CppIndex:
        """Repo-wide index: the ``#include`` header map plus a parse pre-pass mapping
        free-function / ``Scope::method`` **definitions** → path (for cross-file call
        resolution). Honest-null on any name shared by >1 file."""
        return build_cpp_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("cpp", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index if isinstance(ctx.resolution_index, CppIndex) else None
        internal, external, _, _ = extract_imports(root, source, idx.headers if idx else None)
        resolve = make_cpp_resolver(defined_names(root, source), path, idx)

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        class_map: dict[str, str] = {}  # simple class name → class id (out-of-class method attach)

        def process(scope: Node, ns: str = "") -> None:
            for child in scope.named_children:
                node = _unwrap_template(child)
                if has_declaration_error(node):
                    continue  # corrupt header — skip rather than emit fabricated data
                if node.type in _TRANSPARENT_SCOPES:
                    process(node, ns)  # include guard / preproc conditional / extern "C" — recurse
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

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="cpp",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )
