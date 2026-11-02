"""NestJS microservice **outbound** messages: ``this.client.send(pattern, data)`` →
``eventbus_send`` and ``this.client.emit(pattern, data)`` → ``eventbus_publish``, where
``client`` is a ``ClientProxy`` — declared with that type (a constructor parameter or a
property, usually ``@Inject('X')`` / ``@Client()``) or assigned ``ClientProxyFactory.create(…)``.
A transport's own client — ``ClientKafka``, ``ClientRMQ``, ``ClientNats``, ``ClientMqtt``,
``ClientRedis``, ``ClientTCP`` — counts too, when the file imports that type from
``@nestjs/microservices`` (the names are common enough to belong to another library).
``ClientGrpc`` is not one: it has no ``send`` / ``emit`` (it hands out service stubs). A
receiver whose type is not known to be a client records nothing (``send`` and ``emit`` are
common method names).

**Additive** (``nestjs-clients``): a client is a messaging library, used in files other
frameworks own — a LoopBack or Express controller, a GraphQL resolver — so it runs for every
TypeScript file, whatever parser owns it, not only inside the NestJS parser.

The ``endpoint`` is written exactly like the consumer side's ``@MessagePattern`` /
``@EventPattern`` records (:func:`.patterns.message_pattern`, shared) so a sender and its
consumer match: a string pattern as the string, an object pattern as canonical JSON with sorted
keys. A pattern held in a constant resolves through the repo's string constants; anything else
leaves the endpoint empty (never the symbol it was read from)."""

from __future__ import annotations

from functools import cache
from typing import Any

from tree_sitter import Node

from ...schemas import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..typescript.declarations import import_bindings
from .patterns import message_pattern

_KIND: dict[str, SemanticType] = {"send": "eventbus_send", "emit": "eventbus_publish"}
#: Transport clients of ``@nestjs/microservices`` with ClientProxy's ``send`` / ``emit``.
_TRANSPORT_CLIENTS = frozenset({
    "ClientKafka", "ClientRMQ", "ClientNats", "ClientMqtt", "ClientRedis", "ClientTCP",
})
_MICROSERVICES = "@nestjs/microservices"


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


def _client_types(root: Node, source: bytes) -> set[str]:
    """The type names that mark a client here: ``ClientProxy``, and each transport client this
    file imports from ``@nestjs/microservices`` (under its local name)."""
    types = {"ClientProxy"}
    if _MICROSERVICES.encode() in source:
        types |= {local for local, (module, name) in import_bindings(root, source).items()
                  if module == _MICROSERVICES and name in _TRANSPORT_CLIENTS}
    return types


def _client_members(root: Node, source: bytes) -> set[str]:
    """Names of ``this.<name>`` members that are microservice clients in this file."""
    types = _client_types(root, source)
    names: set[str] = set()
    for p in _walk(root, ("required_parameter", "optional_parameter")):
        pat = p.child_by_field_name("pattern")
        if pat is not None and _type_name(p.child_by_field_name("type"), source) in types:
            names.add(node_text(pat, source))
    for f in _walk(root, ("public_field_definition",)):
        name = f.child_by_field_name("name")
        value = f.child_by_field_name("value")
        typed = _type_name(f.child_by_field_name("type"), source) in types
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


def detect_client_proxy_calls(
    root: Node, source: bytes, *, framework: str, index: object | None = None,
) -> list[Emit]:
    """The ``eventbus_send`` / ``eventbus_publish`` calls of this file, for
    :func:`~..emits.apply`: each enriches the unclassified statement it sits on, or is a new one."""
    if b"ClientProxy" not in source and not any(t.encode() in source for t in _TRANSPORT_CLIENTS):
        return []
    clients = _client_members(root, source)
    if not clients:
        return []
    index_consts = getattr(index, "const_values", None)
    consts: dict[str, str | None] = index_consts if isinstance(index_consts, dict) else {}
    found: list[Emit] = []
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
        found.append(Emit(
            call,
            {"semanticType": _KIND[node_text(prop, source)],
             "endpoint": message_pattern(first, source, consts) if first is not None else None,
             "framework": framework},
            enrich="unclassified", node_type="synthetic",
            text=first_line(node_text(call, source))[:200],
        ))
    return found


def nest_framework(source: bytes) -> str:
    """The framework label of a decorator-controller file (shared with the NestJS parser)."""
    if b"@nestjs/" in source:
        return "nestjs"
    if b"routing-controllers" in source:
        return "routing-controllers"
    return "nestjs-like"  # custom / re-exported decorators (matches the legacy label)


@cache
def _nest_parser() -> Any:
    from .parser import NestJSParser  # the parser module imports this one

    return NestJSParser()


def _detect(dc: DetectContext) -> Detection | None:
    # The files the NestJS parser claims (a `@nestjs/` / routing-controllers import, or a
    # decorator controller), whatever parser owns them: a `ClientProxy` / `ClientKafka` without
    # one is some other library's type.
    if not _nest_parser().claims(dc.path, dc.source):
        return None
    sends = detect_client_proxy_calls(
        dc.root, dc.source, framework=nest_framework(dc.source),
        index=dc.ctx.resolution_index,
    )
    return Detection(sends, label=nest_framework(dc.source)) if sends else None


# Guarded by "Client": every client type (ClientProxy, ClientKafka, …) carries it.
register_detector(Detector(
    name="nestjs-clients", gate=Gate("typescript", order=25, guard=b"Client", skip_fixtures=True),
    detect=_detect, frameworks=("nestjs", "routing-controllers", "nestjs-like"),
))
