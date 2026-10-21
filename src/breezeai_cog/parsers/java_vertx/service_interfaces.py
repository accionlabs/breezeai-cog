"""Vert.x service interfaces as a registered fact, ``vertx.service-interfaces``.

A service proxy puts every method of an interface on one event-bus address; the message's
``action`` header picks the method::

    @ProxyGen public interface OrderService {                  // service/OrderService.java
        void listOrders(Handler<AsyncResult<List<JsonObject>>> h);
        void getOrder(String id, Handler<AsyncResult<JsonObject>> h);
    }
    new ServiceBinder(vertx).setAddress("order.service")       // verticle/OrderServiceVerticle.java
        .register(OrderService.class, impl);

Each service method is a binding on that address — one ``eventbus_consumer`` per method, whose
handler is the method. The interface usually lives in another file, so this fact reads every
Java interface while the Java index is built: fully-qualified name → its service methods (the
abstract ones, in source order; ``default`` / ``static`` methods and ``@ProxyIgnore`` are not
proxied). Two interfaces with one fully-qualified name → absent (ambiguous).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text

FACT = "vertx.service-interfaces"

_NOT_PROXIED = frozenset({"default", "static", "private"})


@dataclass(frozen=True)
class ServiceInterfaces:
    """``methods``: interface fully-qualified name → its service methods, in source order."""

    methods: dict[str, tuple[str, ...]] = field(default_factory=dict)


def service_methods(index: Any | None, fqn: str) -> tuple[str, ...]:
    """The service methods of interface ``fqn`` (``()`` when unknown)."""
    fact: ServiceInterfaces | None = index_fact(index, FACT)
    return fact.methods.get(fqn, ()) if fact is not None else ()


def _package(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "package_declaration":
            nm = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
            return node_text(nm, source) if nm is not None else ""
    return ""


def _proxied(method: Node, source: bytes) -> bool:
    mods = next((c for c in method.named_children if c.type == "modifiers"), None)
    if mods is None:
        return True
    words = set(node_text(mods, source).replace("@", " @").split())
    return not (words & _NOT_PROXIED) and "@ProxyIgnore" not in words


def _interfaces(root: Node, source: bytes, outer: str) -> list[tuple[str, tuple[str, ...]]]:
    out: list[tuple[str, tuple[str, ...]]] = []
    for node in root.named_children:
        if node.type not in ("interface_declaration", "class_declaration"):
            continue
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        fqn = f"{outer}.{node_text(name, source)}" if outer else node_text(name, source)
        if node.type == "interface_declaration":
            methods: list[str] = []
            for member in body.named_children:
                mname = member.child_by_field_name("name")
                if member.type == "method_declaration" and mname is not None \
                        and _proxied(member, source) and node_text(mname, source) not in methods:
                    methods.append(node_text(mname, source))
            if methods:
                out.append((fqn, tuple(methods)))
        out += _interfaces(body, source, fqn)  # nested types: Outer.Inner
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[tuple[str, tuple[str, ...]]] | None:
    return _interfaces(root, source, _package(root, source)) or None


def _resolve(values: list[list[tuple[str, tuple[str, ...]]]], index: object) -> ServiceInterfaces | None:
    methods: dict[str, tuple[str, ...] | None] = {}
    for fqn, names in sorted(i for per_file in values for i in per_file):
        record_distinct(methods, fqn, names)
    found = {k: v for k, v in methods.items() if v is not None}
    return ServiceInterfaces(found) if found else None


register_fact(FactSpec(
    name=FACT, language="java", gate=(b"interface",), collect=_collect, resolve=_resolve,
))
