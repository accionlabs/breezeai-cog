"""KotlinParser — extracts a .kt file into a FileRecord.

Covers imports, classes/interfaces/enums/objects, functions/methods, annotations,
and simple call extraction for .kt files. Uses tree-sitter-kotlin grammar via
the shared parse_source() helper.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import class_id, disambiguate, file_id, function_id
from ...schemas import (
    SCHEMA_VERSION,
    Call,
    Class,
    ConstructorParam,
    Decorator,
    FileRecord,
    Function,
    Parameter,
    Statement,
)
from ...schemas.enums import ClassType
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import CallResolver, make_resolver, noop_resolver
from ..treesitter import line_span, node_text, parse_source
from .functions import defined_names, type_map
from .imports import KotlinIndex, build_fqcn_index, extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES

_CLASS_TYPES = ("class_declaration", "object_declaration")

_LOCAL_DECL_TYPES = frozenset({"function_declaration"} | set(_CLASS_TYPES))
_LOCAL_SCOPE_STOP = frozenset({"lambda_literal", "class_body", "enum_class_body"})


def _local_decl_members(node: Node) -> Iterator[Node]:
    """Yield local function/class declarations directly owned by a function body.

    Descends through control-flow and block wrapper nodes but stops at lambda
    literals and class bodies — those are separate scopes handled elsewhere.
    """
    for child in node.named_children:
        if child.type in _LOCAL_DECL_TYPES:
            yield child
        elif child.type not in _LOCAL_SCOPE_STOP:
            yield from _local_decl_members(child)

# First unnamed child token that distinguishes class kind.
_CLASS_KIND_MAP: dict[str, ClassType] = {
    "class": "class",
    "interface": "interface",
    "enum": "enum",
}


def _infix_object_name(node: Node, source: bytes) -> str | None:
    """Detect `object Name` (no body) which tree-sitter-kotlin misparses as infix_expression.

    Grammar produces: infix_expression(object_literal, "Name", ...). The bodyless form
    may be nested inside another infix_expression when adjacent declarations share a line
    without braces.
    """
    named = node.named_children
    if not named:
        return None
    if named[0].type == "object_literal":
        name_node = next((c for c in named[1:] if c.type == "simple_identifier"), None)
        return node_text(name_node, source) if name_node else None
    if named[0].type == "infix_expression":
        return _infix_object_name(named[0], source)
    return None


def _class_kind(node: Node) -> ClassType:
    """Map the first unnamed child keyword to a schema ClassType string."""
    if node.type == "object_declaration":
        return "module"
    for child in node.children:
        if not child.is_named:
            kind = _CLASS_KIND_MAP.get(child.type)
            if kind is not None:
                return kind
    return "class"


def _visibility(modifiers: Node | None, source: bytes, default: str = "public") -> str:
    if modifiers is None:
        return default
    for child in modifiers.named_children:
        if child.type == "visibility_modifier":
            return node_text(child, source)
    return default


def _is_abstract(node: Node, modifiers: Node | None) -> bool:
    # interface keyword → implicitly abstract in Kotlin
    for child in node.children:
        if not child.is_named and child.type == "interface":
            return True
    if modifiers is None:
        return False
    for child in modifiers.named_children:
        if child.type == "inheritance_modifier":
            for kw in child.children:
                if kw.type == "abstract":
                    return True
    return False


def _modifiers(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def _annotations(modifiers: Node | None, source: bytes) -> list[Decorator]:
    if modifiers is None:
        return []
    out: list[Decorator] = []
    for child in modifiers.named_children:
        if child.type == "annotation":
            # @Name or @Name(args...)
            name_node = next(
                (c for c in child.named_children if c.type in {"user_type", "constructor_invocation"}),
                None,
            )
            if name_node is None:
                continue
            if name_node.type == "user_type":
                ann_name = next(
                    (node_text(c, source) for c in name_node.named_children if c.type == "type_identifier"),
                    node_text(name_node, source),
                )
                out.append(Decorator(name=ann_name, args=[]))
            else:
                # constructor_invocation: Name(args)
                ut = next((c for c in name_node.named_children if c.type == "user_type"), None)
                ann_name = next(
                    (node_text(c, source) for c in ut.named_children if c.type == "type_identifier"),
                    node_text(name_node, source),
                ) if ut is not None else node_text(name_node, source)
                args_node = next((c for c in name_node.named_children if c.type == "value_arguments"), None)
                args = [node_text(a, source) for a in args_node.named_children] if args_node else []
                out.append(Decorator(name=ann_name, args=args))
    return out


def _supertypes(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Extract (extends, implements) from delegation_specifier children."""
    extends: str | None = None
    implements: list[str] = []
    for child in node.named_children:
        if child.type != "delegation_specifier":
            continue
        # constructor_invocation → class being extended (has parens)
        ctor = next((c for c in child.named_children if c.type == "constructor_invocation"), None)
        if ctor is not None:
            ut = next((c for c in ctor.named_children if c.type == "user_type"), None)
            if ut is not None:
                ti = next((c for c in ut.named_children if c.type == "type_identifier"), None)
                if ti is not None:
                    extends = node_text(ti, source)
            continue
        # bare user_type → interface being implemented
        ut = next((c for c in child.named_children if c.type == "user_type"), None)
        if ut is not None:
            ti = next((c for c in ut.named_children if c.type == "type_identifier"), None)
            if ti is not None:
                implements.append(node_text(ti, source))
    return extends, implements


def _return_type(node: Node, source: bytes) -> str | None:
    """Extract function return type from the user_type AFTER function_value_parameters."""
    found_params = False
    for child in node.named_children:
        if child.type == "function_value_parameters":
            found_params = True
            continue
        if found_params and child.type in {"user_type", "nullable_type"}:
            inner = next(
                (c for c in child.named_children if c.type == "type_identifier"),
                None,
            )
            return node_text(inner, source) if inner is not None else node_text(child, source)
    return None


class KotlinParser(BaseParser):
    name = "kotlin"
    extensions = (".kt",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> KotlinIndex:
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("kotlin", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index
        fqcn = idx.fqcn if isinstance(idx, KotlinIndex) else idx
        internal, external, _, bindings = extract_imports(root, source, path, ctx.repo_root, fqcn)
        resolve = make_resolver(
            bindings, defined_names(root, source), path, type_map(root, source)
        )
        functions: list[Function] = []
        classes: list[Class] = []
        statements: list[Statement] = []

        for child in root.named_children:
            if child.type in _CLASS_TYPES:
                cls_list, methods, cls_statements = self._build_class(
                    child, source, path, parent_id=fid, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            elif child.type == "function_declaration":
                fn, nested_fns, nested_cls, fn_statements = self._build_function(
                    child, source, path, parent_id=fid, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                functions.append(fn)
                functions.extend(nested_fns)
                classes.extend(nested_cls)
                statements.extend(fn_statements)
            elif child.type == "infix_expression":
                # tree-sitter misparses `object Name` (no body) as infix_expression
                obj_name = _infix_object_name(child, source)
                if obj_name:
                    classes.append(self._synthetic_object(obj_name, child, source, path, fid, seen_ids))

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="kotlin",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            functions=functions,
            classes=classes,
            statements=statements,
        )

    def _build_class(
        self,
        node: Node,
        source: bytes,
        path: str,
        *,
        parent_id: str,
        seen_ids: set[str],
        capture: bool,
        limit: int,
        resolve: CallResolver = noop_resolver,
    ) -> tuple[list[Class], list[Function], list[Statement]]:
        name = self._decl_name(node, source)
        start, end = line_span(node)
        cid = disambiguate(class_id(path, name), seen_ids)

        mods = _modifiers(node)
        visibility = _visibility(mods, source)
        is_abstract = _is_abstract(node, mods)
        decorators = _annotations(mods, source)
        extends, implements = _supertypes(node, source)

        type_params = next((c for c in node.named_children if c.type == "type_parameters"), None)
        generics = node_text(type_params, source) if type_params is not None else None

        body = next((c for c in node.named_children if c.type in {"class_body", "enum_class_body"}), None)
        methods: list[Function] = []
        statements: list[Statement] = []
        nested_classes: list[Class] = []
        ctor_params: list[ConstructorParam] = []

        # Primary constructor lives on the class node itself, not in the body.
        ctor = next((c for c in node.named_children if c.type == "primary_constructor"), None)
        if ctor is not None:
            ctor_params = self._primary_ctor_params(ctor, source)

        if body is not None:
            from .statements import extract_statements as _extract_stmts
            statements.extend(
                _extract_stmts(body, source, path, parent_id=cid, capture=capture,
                                limit=limit, seen_ids=seen_ids)
            )
            for member in body.named_children:
                if member.type == "function_declaration":
                    fn, sub_fns, sub_cls, fn_stmts = self._build_function(
                        member, source, path, parent_id=cid, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    methods.append(fn)
                    methods.extend(sub_fns)
                    nested_classes.extend(sub_cls)
                    statements.extend(fn_stmts)
                elif member.type in _CLASS_TYPES:
                    # Nested inner class/object — returned flat, parented to this class.
                    sub_cls, sub_methods, sub_stmts = self._build_class(
                        member, source, path, parent_id=cid, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    nested_classes.extend(sub_cls)
                    methods.extend(sub_methods)
                    statements.extend(sub_stmts)

        cls = Class(
            id=cid,
            parentId=parent_id,
            path=path,
            name=name,
            type=_class_kind(node),
            visibility=visibility,
            isAbstract=is_abstract,
            generics=generics,
            extends=extends,
            implements=implements,
            constructorParams=ctor_params,
            decorators=decorators,
            startLine=start,
            endLine=end,
        )
        return [cls, *nested_classes], methods, statements

    def _primary_ctor_params(self, ctor: Node, source: bytes) -> list[ConstructorParam]:
        params: list[ConstructorParam] = []
        for child in ctor.named_children:
            if child.type == "class_parameter":
                # class_parameter: binding_pattern_kind? simple_identifier : user_type
                name_node = next(
                    (c for c in child.named_children if c.type == "simple_identifier"), None
                )
                type_node = next(
                    (c for c in child.named_children if c.type in {"user_type", "nullable_type"}), None
                )
                if name_node is not None:
                    type_text = ""
                    if type_node is not None:
                        ti = next(
                            (c for c in type_node.named_children if c.type == "type_identifier"), None
                        )
                        type_text = node_text(ti, source) if ti is not None else node_text(type_node, source)
                    params.append(ConstructorParam(name=node_text(name_node, source), type=type_text))
        return params

    def _synthetic_object(
        self,
        name: str,
        node: Node,
        source: bytes,
        path: str,
        parent_id: str,
        seen_ids: set[str],
    ) -> Class:
        start, end = line_span(node)
        cid = disambiguate(class_id(path, name), seen_ids)
        return Class(
            id=cid,
            parentId=parent_id,
            path=path,
            name=name,
            type="module",
            visibility="public",
            isAbstract=False,
            startLine=start,
            endLine=end,
        )

    def _build_function(
        self,
        node: Node,
        source: bytes,
        path: str,
        *,
        parent_id: str,
        seen_ids: set[str],
        capture: bool,
        limit: int,
        resolve: CallResolver = noop_resolver,
    ) -> tuple[Function, list[Function], list[Class], list[Statement]]:
        name = self._decl_name(node, source)
        start, end = line_span(node)
        fid = disambiguate(function_id(path, name, start, class_name=None), seen_ids)

        mods = _modifiers(node)
        visibility = _visibility(mods, source)
        decorators = _annotations(mods, source)

        params_node = next((c for c in node.named_children if c.type == "function_value_parameters"), None)
        params: list[Parameter] = []
        if params_node is not None:
            for param in params_node.named_children:
                if param.type == "parameter":
                    params.append(Parameter(
                        name=self._param_name(param, source),
                        type=self._param_type(param, source),
                    ))

        ret = _return_type(node, source)

        body = next((c for c in node.named_children if c.type == "function_body"), None)
        calls: list[Call] = []
        statements: list[Statement] = []
        nested_fns: list[Function] = []
        nested_cls: list[Class] = []
        if body is not None:
            calls = self._collect_calls(body, source, resolve)
            from .statements import extract_statements as _extract_stmts
            statements = _extract_stmts(
                body, source, path, parent_id=fid, capture=capture,
                limit=limit, seen_ids=seen_ids, descend_all=True,
                stop_at=_LOCAL_DECL_TYPES,
            )
            for member in _local_decl_members(body):
                if member.type == "function_declaration":
                    sub_fn, sub_fns, sub_cls, sub_stmts = self._build_function(
                        member, source, path, parent_id=fid, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    nested_fns.append(sub_fn)
                    nested_fns.extend(sub_fns)
                    nested_cls.extend(sub_cls)
                    statements.extend(sub_stmts)
                else:
                    sub_cls_list, sub_methods, sub_stmts = self._build_class(
                        member, source, path, parent_id=fid, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    nested_cls.extend(sub_cls_list)
                    nested_fns.extend(sub_methods)
                    statements.extend(sub_stmts)

        fn = Function(
            id=fid,
            parentId=parent_id,
            path=path,
            name=name,
            type="function",
            visibility=visibility,
            params=params,
            decorators=decorators,
            returnType=ret,
            startLine=start,
            endLine=end,
            calls=calls,
        )
        return fn, nested_fns, nested_cls, statements

    def _decl_name(self, node: Node, source: bytes) -> str:
        # Kotlin grammar doesn't define field names on most nodes; use typed child search.
        for child in node.children:
            if child.type in {"type_identifier", "simple_identifier"}:
                return node_text(child, source)
        return ""

    def _param_name(self, node: Node, source: bytes) -> str:
        return next(
            (node_text(c, source) for c in node.named_children if c.type == "simple_identifier"),
            "",
        )

    def _param_type(self, node: Node, source: bytes) -> str:
        # First pass: prefer user_type (the colon-separated type in `name: Type`)
        for child in node.named_children:
            if child.type == "user_type":
                inner = next(
                    (c for c in child.named_children if c.type in {"type_identifier", "simple_identifier"}),
                    None,
                )
                if inner is not None:
                    return node_text(inner, source)
            if child.type == "nullable_type":
                inner_ut = next((c for c in child.named_children if c.type == "user_type"), None)
                if inner_ut is not None:
                    name_node = next(
                        (c for c in inner_ut.named_children if c.type in {"type_identifier", "simple_identifier"}),
                        None,
                    )
                    if name_node is not None:
                        return node_text(name_node, source) + "?"
                return node_text(child, source)
        # Second pass: bare type_identifier
        for child in node.named_children:
            if child.type == "type_identifier":
                return node_text(child, source)
        return ""

    def _call_name(self, node: Node, source: bytes) -> str | None:
        """Return the method/function name from a call_expression or its callee sub-tree."""
        if node.type in {"simple_identifier", "identifier", "type_identifier"}:
            return node_text(node, source)
        if node.type == "navigation_expression":
            # repo.findById — the method name lives in the last navigation_suffix
            suffix = next(
                (c for c in reversed(node.named_children) if c.type == "navigation_suffix"),
                None,
            )
            if suffix is not None:
                name_node = next(
                    (c for c in suffix.named_children if c.type in {"simple_identifier", "identifier"}),
                    None,
                )
                if name_node is not None:
                    return node_text(name_node, source)
            return next(
                (node_text(c, source) for c in node.named_children if c.type == "simple_identifier"),
                None,
            )
        if node.type == "call_expression":
            callee = node.named_children[0] if node.named_children else None
            return self._call_name(callee, source) if callee is not None else None
        return None

    def _call_receiver(self, node: Node, source: bytes) -> str | None:
        """Return the receiver variable from a call_expression (e.g. 'repo' from 'repo.findById(id)')."""
        if node.type == "call_expression" and node.named_children:
            callee = node.named_children[0]
            if callee.type == "navigation_expression" and callee.named_children:
                first = callee.named_children[0]
                if first.type == "simple_identifier":
                    return node_text(first, source)
        return None

    def _collect_calls(self, body: Node, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
        calls: list[Call] = []
        seen: set[str] = set()

        def visit(node: Node) -> None:
            for child in node.named_children:
                if child.type == "call_expression":
                    name = self._call_name(child, source)
                    if name and name not in seen:
                        seen.add(name)
                        receiver = self._call_receiver(child, source)
                        calls.append(Call(name=name, path=resolve(name, receiver)))
                visit(child)

        visit(body)
        return calls
