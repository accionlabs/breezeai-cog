"""NestJS microservice **outbound** messages: ``this.client.send(pattern, data)`` →
``eventbus_send`` and ``this.client.emit(pattern, data)`` → ``eventbus_publish``, where
``client`` is a ``ClientProxy`` — declared with that type (a constructor parameter or a
property, usually ``@Inject('X')`` / ``@Client()``) or assigned ``ClientProxyFactory.create(…)``.
A receiver whose type is not known to be a ``ClientProxy`` records nothing (``send`` and
``emit`` are common method names).

The ``endpoint`` is written exactly like the consumer side's ``@MessagePattern`` /
``@EventPattern`` records (:func:`routes._pattern`) so a sender and its consumer match: a
string pattern unquoted, an object pattern as its source text. A pattern held in a constant
resolves through the repo's string constants; anything else leaves the endpoint empty (never
the symbol it was read from)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, SemanticType, Statement
from ..enclosing import Enclosing
from ..treesitter import first_line, node_text
from ..typescript.imports import resolve_const_value

_KIND: dict[str, SemanticType] = {"send": "eventbus_send", "emit": "eventbus_publish"}


def _walk(node: Node, types: tuple[str, ...]) -> list[Node]:
    out: list[Node] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _type_name(node: Node | None, source: bytes) -> str:
    """``: ClientProxy`` annotation → ``ClientProxy`` (the last identifier of the type)."""
    if node is None:
        return ""
    return node_text(node, source).lstrip(":").strip().rsplit(".", 1)[-1]


def _client_members(root: Node, source: bytes) -> set[str]:
    """Names of ``this.<name>`` members that are ClientProxy instances in this file."""
    names: set[str] = set()
    for p in _walk(root, ("required_parameter", "optional_parameter")):
        pat = p.child_by_field_name("pattern")
        if pat is not None and _type_name(p.child_by_field_name("type"), source) == "ClientProxy":
            names.add(node_text(pat, source))
    for f in _walk(root, ("public_field_definition",)):
        name = f.child_by_field_name("name")
        value = f.child_by_field_name("value")
        typed = _type_name(f.child_by_field_name("type"), source) == "ClientProxy"
        created = value is not None and node_text(value, source).startswith("ClientProxyFactory.create(")
        if name is not None and (typed or created):
            names.add(node_text(name, source))
    for a in _walk(root, ("assignment_expression",)):
        left, right = a.child_by_field_name("left"), a.child_by_field_name("right")
        if (left is not None and right is not None and left.type == "member_expression"
                and node_text(right, source).startswith("ClientProxyFactory.create(")):
            prop = left.child_by_field_name("property")
            if prop is not None:
                names.add(node_text(prop, source))
    return names


def _pattern(arg: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    """The message pattern as the consumer side writes it, or None when it cannot be resolved."""
    if arg.type == "object":
        return node_text(arg, source)  # same as @MessagePattern({ … }) on the consumer side
    return resolve_const_value(arg, source, consts)


def detect_client_proxy_calls(
    root: Node, source: bytes, path: str, record: FileRecord, *, framework: str,
    index: object | None = None,
) -> bool:
    """Enrich / append ``eventbus_send`` / ``eventbus_publish`` statements. True if any."""
    if b"ClientProxy" not in source:
        return False
    clients = _client_members(root, source)
    if not clients:
        return False
    index_consts = getattr(index, "const_values", None)
    consts: dict[str, str | None] = index_consts if isinstance(index_consts, dict) else {}
    enclosing = Enclosing(record)
    seen = {s.id for s in record.statements}
    used: set[str] = set()
    found = False
    for call in _walk(root, ("call_expression",)):
        fn = call.child_by_field_name("function")
        prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else None
        obj = fn.child_by_field_name("object") if prop is not None and fn is not None else None
        if prop is None or obj is None or node_text(prop, source) not in _KIND:
            continue
        member = obj.child_by_field_name("property") if obj.type == "member_expression" else None
        owner = obj.child_by_field_name("object") if obj.type == "member_expression" else None
        if member is None or owner is None or owner.type != "this" or node_text(member, source) not in clients:
            continue
        args = call.child_by_field_name("arguments")
        first = next(iter(args.named_children), None) if args is not None else None
        endpoint = _pattern(first, source, consts) if first is not None else None
        kind = _KIND[node_text(prop, source)]
        line = call.start_point[0] + 1
        stmt = enclosing.statement(line)
        if stmt is not None and stmt.semanticType is None and stmt.id not in used:
            used.add(stmt.id)
            stmt.semanticType = kind
            stmt.endpoint = endpoint
            stmt.framework = framework
        else:
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            seen.add(new_id)
            record.statements.append(Statement(
                id=new_id,
                parentId=enclosing.function_id(line, file_id(path)),
                nodeType="synthetic",
                semanticType=kind,
                text=first_line(node_text(call, source))[:200],
                endpoint=endpoint,
                framework=framework,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
        found = True
    return found
