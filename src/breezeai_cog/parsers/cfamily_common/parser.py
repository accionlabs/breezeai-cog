"""CFamilyParser — the extraction C, C++ and CUDA share: one translation unit into a FileRecord.

The tree-sitter C++ grammar is built on the C grammar, so the constructs both languages have
(``function_definition``, ``struct_specifier``, ``declaration``, ``preproc_def``, …) are the same
node types in both trees. One walker reads either; C files simply never contain the C++-only
nodes. The CUDA grammar is built on the C++ one in the same way. Each language parses with its
own grammar (``grammar``) and keeps its own identity, index and evidence in its package
(``parsers/c``, ``parsers/cpp``, ``parsers/cuda``).

C and C++ are **best-effort** languages: real files routinely parse with ``has_error`` (macros
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

C and C++ share ``.h``: the ownership step decides each header's language. Each reads the
other's index after its own (``related_languages``), and CUDA reads both; what a language may
call there is :meth:`CFamilyParser.callable_in`.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import node_text
from .classes import (
    _CLASS_TYPES,
    TYPE_ALIAS_TYPES,
    _declarator_name,
    _unwrap_template,
    build_class,
    build_enum,
    parse_cfamily,
    type_alias_statement,
    typedef_type,
)
from .functions import (
    build_function,
    DeclarationRead,
    decorators_of,
    defined_names,
    function_declarator_of,
    has_declaration_error,
    read_declaration,
    is_misread_declarator,
    owner_is_explicit,
    spilled_members,
)
from .imports import FamilyHeaders, extract_imports
from .index import (
    CFamilyIndex, IncludeGraph, _join_ns, _ns_name, header_visibility, make_cfamily_resolver,
)
from ..comments_common import comment_statements_for
from ..statements_common import member_statement
from .statements import macro_statement
from .mappings import (
    COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, MACRO_TYPES, STATEMENT_TYPES, TRANSPARENT_SCOPES,
)


#: File-scope nodes that ``read_declaration`` decides on.
_DECLARATIONS = ("declaration", "function_definition")


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


class CFamilyParser(BaseParser):
    """The shared extraction. A language subclasses it with its ``name``, ``grammar``,
    ``extensions``, ``related_languages``, ``ownership``, ``build_index``, :meth:`callable_in`
    and :meth:`run_detectors`."""

    #: The tree-sitter grammar this language's files are parsed with.
    grammar: str
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS
    build_index_accepts_timeout = True

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_cfamily(self.grammar, ctx.source, ctx.parse_timeout_micros)
        return self.extract(root, ctx)

    def callable_in(self, related: CFamilyIndex) -> dict[str, str | None]:
        """The free functions of a related language's index this language can call."""
        raise NotImplementedError

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        """Run the language's additive detectors (``run_additive(<language>, …)``)."""
        raise NotImplementedError

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index if isinstance(ctx.resolution_index, CFamilyIndex) else None
        related = [r for r in ctx.related_indexes.values() if isinstance(r, CFamilyIndex)]
        headers = FamilyHeaders(idx.headers if idx else {}, [r.headers for r in related])
        internal, external, _, _ = extract_imports(root, source, headers)
        # Class members that spilled to file scope: unqualified definitions aren't emitted.
        spilled = spilled_members(root, source, self.grammar != "c")
        graph = IncludeGraph(headers, [i for i in (idx, *related) if i is not None])
        resolve = make_cfamily_resolver(
            defined_names(root, source, spilled), path, idx,
            [self.callable_in(r) for r in related],
            header_visibility(path, internal, graph),
        )

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        class_map: dict[str, str] = {}  # simple class name → class id (out-of-class method attach)

        def process(scope: Node, ns: str = "") -> None:
            for child in scope.named_children:
                node = _unwrap_template(child)
                read = DeclarationRead()
                if node.type in _DECLARATIONS:
                    if spilled and not owner_is_explicit(node):
                        continue  # maybe a member of a class the grammar lost: absent beats wrong
                    # A declaration or definition is read past a macro where that is safe
                    # (`read_declaration`); one whose name can't be trusted is skipped.
                    maybe_read = read_declaration(node, source)
                    if maybe_read is None:
                        continue
                    read = maybe_read
                if node.type in TRANSPARENT_SCOPES:
                    process(node, ns)  # include guard / preproc conditional / extern "C" — recurse
                elif node.type not in _DECLARATIONS and has_declaration_error(node):
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
                    _emit_function(node, ns, read)
                elif node.type in MACRO_TYPES and capture and not _is_include_guard(node, source):
                    statements.append(
                        macro_statement(
                            node, source, path, parent_id=fid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                elif (typedef := typedef_type(node, source)) is not None:
                    # `typedef struct [tag] { … } name;` defines the type as well as naming it
                    # — C's usual way to declare a struct, enum or union. It is a class under
                    # the typedef's name, the one code writes; a different tag is its `alias`.
                    spec, type_name, tag = typedef
                    alias = tag if tag is not None and tag != type_name else None
                    if spec.type == "enum_specifier":
                        enum_cls, enum_stmts = build_enum(
                            spec, source, path, parent_id=fid, seen_ids=seen_ids,
                            capture=capture, limit=limit, name=type_name, alias=alias,
                        )
                        if enum_cls is not None:
                            classes.append(enum_cls)
                            statements.extend(enum_stmts)
                    else:
                        cls_list, methods, cls_stmts = build_class(
                            spec, source, path, parent_id=fid, seen_ids=seen_ids,
                            capture=capture, limit=limit, resolve=resolve, name=type_name,
                            alias=alias,
                        )
                        classes.extend(cls_list)
                        functions.extend(methods)
                        statements.extend(cls_stmts)
                        for c in cls_list:
                            class_map.setdefault(c.name, c.id)
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
                    # `constexpr char kName[] = "x";`), or a function prototype
                    # (`int list_len(const node_t *head);`). C++ puts much of its controlled
                    # vocabulary here rather than in a class, and a C header's prototypes are
                    # its module's API; neither is captured anywhere else, so each is a flat
                    # statement named after what it declares (its `text` — incl. any
                    # `= value` or parameter list — is queryable). A prototype is not a
                    # `Function`: the definition is, wherever it is. Namespace members parent
                    # to the file, matching how `process` already flattens namespaces. A
                    # declarator the grammar misread (a macro taken for the type) is skipped.
                    # So is one with an error on the way to its name: zlib's `int f OF((int a,
                    # int b));` reads as a variable named after the last parameter. One read
                    # past a macro (`read_declaration`) is kept only with a name to show.
                    declarator = node.child_by_field_name("declarator")
                    name = read.name or _declarator_name(declarator, source)
                    if not is_misread_declarator(declarator) and (
                        name is not None or read.return_type_known
                    ):
                        statements.append(
                            member_statement(
                                node, source, path, parent_id=fid, limit=limit, seen_ids=seen_ids,
                                name=name, decorators=decorators_of(node, source, read),
                            )
                        )

        def _emit_function(node: Node, ns: str, read: DeclarationRead) -> None:
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
                    resolve=resolve, class_name=class_simple, kind="method", read=read,
                )
            else:
                fn, fn_stmts = build_function(
                    node, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                    read=read,
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
