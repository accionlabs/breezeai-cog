"""Vert.x **handler factories** (Java): a static method whose body is only ``return new T(…)``,
read over the Java AST and composed into ``detect_vertx``.

Vert.x handlers are often built through a factory (``router.get("/x").handler(
SessionHandler.create(options))``, with ``static Handler<RoutingContext> create(AppOptions o)
{ return new SessionHandler(o); }``). The call alone does not say what it returns, so
:func:`~..handler_sites.handler_info` leaves it empty; the factory's own body does. The
``vertx.handler-factories`` fact lists them repo-wide by fully-qualified
``pkg.Class.method/arity`` → the fully-qualified class ``T`` it builds, and a handler argument
calling one (directly, or through a local assigned once) is then handled like ``new T(…)``:
``handle`` of ``T``, located in its file.

Only that one shape counts: a factory with any other statement (a cache, a condition), one
returning an anonymous class, a builder chain or a library factory (``BodyHandler.create()``,
not in the repo) gives nothing. A factory is read only in a file naming ``org.vertx`` or
``io.vertx``. Groovy is left out: an implicit ``return`` and an untyped ``def`` make the shape
weaker there. Shapes only, never names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from .forwarders import _Names, pick
from .java_syntax import arguments, local_value, name_of

FACTORIES = "vertx.handler-factories"

#: One factory, as collected: (``pkg.Class.method/arity``, candidates for the class built, exact).
Factory = tuple[str, tuple[str, ...], bool]


def _built(decl: Node, source: bytes) -> str | None:
    """The class a static method builds when its body is only ``return new T(…)``, as written."""
    mods = next((c for c in decl.named_children if c.type == "modifiers"), None)
    body = decl.child_by_field_name("body")
    if mods is None or "static" not in name_of(mods, source).split() or body is None:
        return None
    stmts = [c for c in body.named_children if c.type != "line_comment"
             and c.type != "block_comment"]
    if len(stmts) != 1 or stmts[0].type != "return_statement":
        return None
    value = next(iter(stmts[0].named_children), None)
    if value is None or value.type != "object_creation_expression" \
            or any(c.type == "class_body" for c in value.named_children):
        return None
    typ = value.child_by_field_name("type")
    return name_of(typ, source).split("<", 1)[0].strip() if typ is not None else None


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[Factory] | None:
    """This file's factories: the static methods of each top-level or member class (no walk
    into method bodies)."""
    names = _Names(root, source)
    found: list[Factory] = []

    def scan(decl: Node, chain: tuple[str, ...]) -> None:
        simple = name_of(decl.child_by_field_name("name"), source)
        body = decl.child_by_field_name("body")
        if not simple or body is None:
            return
        inner = (*chain, simple)
        for member in body.named_children:
            if member.type == "class_declaration":
                scan(member, inner)
            elif member.type == "method_declaration":
                built = _built(member, source)
                params = member.child_by_field_name("parameters")
                if built is None or params is None \
                        or any(p.type == "spread_parameter" for p in params.named_children):
                    continue
                arity = sum(1 for p in params.named_children if p.type == "formal_parameter")
                method = name_of(member.child_by_field_name("name"), source)
                found.append((f"{names.qualify(*inner)}.{method}/{arity}",
                              *names.candidates(built)))

    for node in root.named_children:
        if node.type == "class_declaration":
            scan(node, ())
    return found or None


def _resolve(values: list[list[Factory]], index: object) -> dict[str, str] | None:
    """Merged across files; the class built must be a type of the repo, and a key declared
    twice with a different result is dropped (unsure)."""
    types: dict[str, Any] = getattr(index, "fqcn", None) or {}
    merged: dict[str, str | None] = {}
    for found in values:
        for key, candidates, exact in found:
            record_distinct(merged, key, pick(candidates, exact, types, types))
    known = {k: v for k, v in merged.items() if v is not None}
    return known or None


register_fact(FactSpec(
    name=FACTORIES, language="java", gate=(b"org.vertx", b"io.vertx"),  # Vert.x 2, Vert.x 3+
    collect=_collect, resolve=_resolve,
))


class FactoryCalls:
    """The handler factories one file calls: :meth:`built` gives the class a handler argument
    builds through one, else None. The file's imports are read only when the repo has a
    factory."""

    def __init__(self, root: Node, source: bytes, index: object | None) -> None:
        self.root = root
        self.source = source
        self.index = index
        self.table: dict[str, str] = index_fact(index, FACTORIES) or {}
        self.classes = {k.rsplit("/", 1)[0].rsplit(".", 1)[0] for k in self.table}
        self._names: _Names | None = None

    def built(self, arg: Node | None) -> str | None:
        if not self.table or arg is None:
            return None
        if arg.type == "identifier":
            arg = local_value(arg, name_of(arg, self.source), self.source)
        if arg is None or arg.type != "method_invocation":
            return None
        obj = arg.child_by_field_name("object")
        written = name_of(obj, self.source) if obj is not None \
            and obj.type in ("identifier", "field_access") else ""
        if not written.rsplit(".", 1)[-1][:1].isupper():  # a class name (static call) only
            return None
        if self._names is None:
            self._names = _Names(self.root, self.source, self.index)
        owner = self._names.resolve(written, self.classes)
        method = name_of(arg.child_by_field_name("name"), self.source)
        return self.table.get(f"{owner}.{method}/{len(arguments(arg))}") if owner else None
