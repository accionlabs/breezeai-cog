"""PHP class / interface / trait / enum extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ClassType, ConstructorParam, Decorator, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import emit_enum_members
from ..treesitter import line_span, node_text
from .attributes import extract_attributes
from .functions import build_function, extract_params
from .statements import extract_statements


def _class_type(node: Node) -> ClassType:
    if node.type == "interface_declaration":
        return "interface"
    if node.type == "trait_declaration":
        return "trait"
    if node.type == "enum_declaration":
        return "enum"
    return "class"


def _is_abstract(node: Node) -> bool:
    if node.type == "interface_declaration":
        return True
    return any(c.type == "abstract_modifier" for c in node.children)


def _extends(node: Node, source: bytes) -> str | None:
    base = next((c for c in node.named_children if c.type == "base_clause"), None)
    if base is not None:
        target = next(
            (c for c in base.named_children if c.type in ("name", "qualified_name")), None
        )
        if target is not None:
            return node_text(target, source)
    return None


def _implements(node: Node, source: bytes) -> list[str]:
    iface_clause = next(
        (c for c in node.named_children if c.type == "class_interface_clause"), None
    )
    if iface_clause is None:
        return []
    out: list[str] = []
    for c in iface_clause.named_children:
        if c.type in ("name", "qualified_name"):
            out.append(node_text(c, source))
    return out


def _constructor_params(body: Node | None, source: bytes) -> list[ConstructorParam]:
    if body is None:
        return []
    for child in body.named_children:
        if child.type == "method_declaration":
            nm = child.child_by_field_name("name")
            if nm is not None and node_text(nm, source) == "__construct":
                params_node = child.child_by_field_name("parameters")
                params = extract_params(params_node, source)
                return [ConstructorParam(name=p.name, type=p.type) for p in params]
    return []


def build_class(
    node: Node,
    decorators: list[Decorator],
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Build a Class model (class, interface, trait, enum) + its methods and statements."""
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(node)
    ctype = _class_type(node)
    cid = disambiguate(class_id(path, name), seen_ids)

    extends_name = _extends(node, source)
    implements_names = _implements(node, source)
    is_abs = _is_abstract(node)
    all_decs = list(decorators) + extract_attributes(node, source)

    body = node.child_by_field_name("body")
    cparams = _constructor_params(body, source)

    methods: list[Function] = []
    cls_statements: list[Statement] = []

    if body is not None:
        # Extract enum cases
        if ctype == "enum" and capture:
            cls_statements.extend(
                emit_enum_members(
                    body,
                    source,
                    path,
                    member_types={"enum_case"},
                    parent_id=cid,
                    limit=limit,
                    seen_ids=seen_ids,
                )
            )

        # Extract methods
        for child in body.named_children:
            if child.type == "method_declaration":
                mnode = child.child_by_field_name("name")
                mname = node_text(mnode, source) if mnode is not None else ""
                fns, fn_stmts = build_function(
                    child,
                    name=mname,
                    kind="method",
                    decorators=[],
                    source=source,
                    path=path,
                    parent_id=cid,
                    class_name=name,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                methods.extend(fns)
                cls_statements.extend(fn_stmts)

        # Extract class-body statements (excluding nested methods)
        if capture:
            method_spans = frozenset(
                (m.start_byte, m.end_byte)
                for m in body.named_children
                if m.type == "method_declaration"
            )
            cls_statements.extend(
                extract_statements(
                    body,
                    source,
                    path,
                    parent_id=cid,
                    capture=capture,
                    limit=limit,
                    seen_ids=seen_ids,
                    descend_all=False,
                    barriers=method_spans,
                )
            )

    cls = Class(
        id=cid,
        parentId=parent_id,
        name=name,
        type=ctype,
        startLine=start,
        endLine=end,
        path=path,
        isAbstract=is_abs if is_abs else None,
        extends=extends_name,
        implements=implements_names,
        constructorParams=cparams,
        decorators=all_decs,
    )

    return [cls], methods, cls_statements
