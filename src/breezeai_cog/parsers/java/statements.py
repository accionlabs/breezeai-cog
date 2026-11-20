"""Flat statement capture for Java (gated by statement capture) + shared API/DB
call detection."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..detection import text_has_query
from ..statements_common import classify_statement, render_concat, resolve_endpoint
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


#: Client methods whose HTTP verb is an ``HttpMethod.X`` argument: Vert.x ``HttpClient`` /
#: ``WebClient`` ``request`` / ``requestAbs``, Spring ``RestTemplate`` ``exchange`` /
#: ``execute``, ``WebClient`` ``method``.
_VERB_ARG_METHODS = frozenset({"request", "requestAbs", "exchange", "execute", "method"})
_HTTP_METHOD_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _http_method_arg(args: list[Node], source: bytes) -> tuple[int, str] | None:
    """``(index, verb)`` of the first ``HttpMethod.PUT`` argument (any qualifier ending in
    ``HttpMethod``), else None."""
    for i, arg in enumerate(args):
        if arg.type != "field_access":
            continue
        obj, field = arg.child_by_field_name("object"), arg.child_by_field_name("field")
        if obj is None or field is None:
            continue
        verb = node_text(field, source)
        if node_text(obj, source).rsplit(".", 1)[-1] == "HttpMethod" and verb in _HTTP_METHOD_VERBS:
            return i, verb.lower()
    return None


def _verb_arg_url(args: list[Node], at: int, source: bytes) -> str | None:
    """The URL of a call whose verb is argument ``at``: the first argument when the verb is not
    first (``exchange(url, HttpMethod.GET, …)``); else the last later argument that reads as
    a path or URL (Vert.x ``request(method, [port,] [host,] uri)``: the host is no path)."""
    if at > 0:
        return render_url(args[0], source)
    for arg in reversed(args[at + 1:]):
        url = render_url(arg, source)
        if url and (url.startswith(("/", "{")) or "://" in url):
            return url
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    obj = call.child_by_field_name("object")
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    verb_arg = _http_method_arg(named, source) if method in _VERB_ARG_METHODS else None
    if verb_arg is not None:
        at, verb = verb_arg
        return callee, verb, _verb_arg_url(named, at, source)
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
    """``node`` is the body of an anonymous class: ``new T(…) { … }``, or an enum constant's
    (``RED { … }``: Java makes it an anonymous subclass of the enum)."""
    parent = node.parent
    return node.type == "class_body" and parent is not None \
        and parent.type in ("object_creation_expression", "enum_constant")


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
    for node in _iter_in_scope(body, descend_all, skip_anonymous):
        records = (
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="java",
                decorators=_field_decorators(node, source),
            )
        )
        out.extend(_elide_anonymous(node, source, records) if skip_anonymous else records)
    return out
