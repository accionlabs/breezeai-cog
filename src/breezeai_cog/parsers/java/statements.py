"""Flat statement capture for Java (gated by statement capture) + shared API/DB
call detection."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..detection import text_has_query
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..sql_variables import built_queries
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "method_invocation"

# Declaration node types that may carry annotations (@Column, @Autowired, @NotNull, ...).
_ANNOTATED_DECL_TYPES = {"field_declaration", "constant_declaration"}


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_variable_declaration", "field_declaration", "constant_declaration"):
        decl = node.child_by_field_name("declarator")
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
    return None


def render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string literal or ``+`` concatenation (Java has no
    string interpolation); non-string parts of a concatenation become ``{name}``."""
    if node.type == "string_literal":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')
    if node.type == "binary_expression":  # "/users/" + id
        return render_concat(node, source, render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    obj = call.child_by_field_name("object")
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(named, source, render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _field_decorators(node: Node, source: bytes) -> list[Decorator] | None:
    """Structured annotations on a field declaration, via the shared ``extract_annotations``
    (same parser used for class/method/param annotations)."""
    if node.type not in _ANNOTATED_DECL_TYPES:
        return None
    from .functions import extract_annotations, modifiers_node  # lazy — avoid an import cycle with functions.py

    return extract_annotations(modifiers_node(node), source)


#: What an anonymous class body is replaced with in the text of the statement that creates it:
#: the body is captured as its own Class, so the creating statement keeps only its head.
ELIDED_BODY = "{ … }"


def is_anonymous_body(node: Node) -> bool:
    """``node`` is the body of an anonymous class (``new T(…) { … }``)."""
    parent = node.parent
    return node.type == "class_body" and parent is not None and parent.type == "object_creation_expression"


def stored_field(body: Node) -> Node | None:
    """The ``variable_declarator`` of the field an anonymous class body is the whole initializer
    of (``T onError = new T() { … };`` → ``onError``), else None: an anonymous class inside a
    larger expression, a local variable or an argument is stored in no field of its own."""
    creation = body.parent
    decl = creation.parent if creation is not None else None
    if decl is None or decl.type != "variable_declarator" \
            or decl.child_by_field_name("value") != creation:
        return None
    field = decl.parent
    return decl if field is not None and field.type == "field_declaration" else None


def stores_only_anonymous(node: Node) -> bool:
    """A field declaration whose every declarator stores an anonymous class: each is a Class of
    its own spanning the declaration, so the declaration is not also a statement."""
    if node.type != "field_declaration":
        return False
    decls = node.children_by_field_name("declarator")
    return bool(decls) and all(
        (value := d.child_by_field_name("value")) is not None
        and value.type == "object_creation_expression"
        and any(is_anonymous_body(c) for c in value.named_children)
        for d in decls
    )


def anonymous_bodies(node: Node) -> list[Node]:
    """The outermost anonymous class bodies under ``node``, in source order (one nested in
    another is the outer one's own)."""
    out: list[Node] = []

    def walk(n: Node) -> None:
        for child in n.named_children:
            if is_anonymous_body(child):
                out.append(child)
            else:
                walk(child)

    walk(node)
    return out


def _iter_in_scope(node: Node, descend_all: bool = False, skip_anonymous: bool = False) -> Iterator[Node]:
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas, attributing their statements to this function; ``False``
    (file-root / class-body) keeps nested scopes as barriers since they are extracted
    as their own Function/Class. ``skip_anonymous``: an anonymous class body is a barrier
    too (it is extracted as its own Class)."""
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if skip_anonymous and (is_anonymous_body(child) or stores_only_anonymous(child)):
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child, descend_all, skip_anonymous)


def _elide_anonymous(node: Node, source: bytes, records: list[Statement]) -> list[Statement]:
    """The records of one statement with each anonymous class body in their text replaced by
    ``ELIDED_BODY``. A raw-SQL classification that came only from the body's text is dropped:
    that SQL belongs to the anonymous class's own statements."""
    bodies = [node_text(b, source) for b in anonymous_bodies(node)]
    if not bodies:
        return records
    out: list[Statement] = []
    for rec in records:
        text = rec.text
        for body in bodies:
            text = text.replace(body, ELIDED_BODY)
        if text == rec.text:
            out.append(rec)
            continue
        update: dict[str, object] = {"text": text}
        if rec.semanticType == "query_statement" and not text_has_query(text):
            update["semanticType"] = None
        out.append(rec.model_copy(update=update))
    return out


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    skip_anonymous: bool = False,
) -> list[Statement]:
    """``skip_anonymous``: anonymous class bodies are captured as their own Class, so their
    statements are not walked here and their text is left out of the statement creating them."""
    if not capture or body is None:
        return []
    out: list[Statement] = []
    queries = built_queries(body, source, "java")
    for node in _iter_in_scope(body, descend_all, skip_anonymous):
        records = (
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="java",
                built_sql=queries,
                decorators=_field_decorators(node, source),
            )
        )
        out.extend(_elide_anonymous(node, source, records) if skip_anonymous else records)
    return out
