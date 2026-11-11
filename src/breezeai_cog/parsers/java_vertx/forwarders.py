"""EventBus **address forwarders** (Vert.x 2 and 3+), read over the Java AST and composed into
``detect_vertx``.

A method or constructor that passes one of its parameters straight on as the address of an
EventBus call (``send(…, String address, …)`` → ``sendWithTimeout(address, …)``, or a
constructor storing ``this.address = address`` for a later ``registerHandler(address, this)``).
Only that self-registration is followed through a field: a class sending through a stored
address (an event-bus proxy, one ``request(address, …)`` per method) keeps each send where it
is, since the construction is not a send.

The ``vertx.address-forwarders`` fact lists them repo-wide by fully-qualified
``pkg.Class.method/arity`` (``pkg.Class.<init>/arity``); each call site is then a record too,
with its own argument folded (unfoldable → ``endpoint`` empty). The forwarder's own call keeps
its record (address unknown): it is the call that sends or registers, and a helper whose call
sites the detector does not read (never called, called from a file without a Vert.x marker, or
declared with two shapes) would otherwise leave no record at all. A call site's class is
resolved as Java resolves it (:class:`_Names`): a same-named class of another package is never
taken for a forwarder. Shapes only, never names.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord, SemanticType
from ..additive import FactSpec, index_fact, register_fact
from ..emits import Emit
from ..handler_sites import handler_info, method_lines
from ..index_common import record_distinct
from ..java_variables_common import typed_vars
from ..vertx_common import EVENTBUS, is_bus_receiver, vertx_emit
from .java_syntax import (arguments, handle_location, handler_class, imports_and_package,
                          known_address, name_of, plain)

FORWARDERS = "vertx.address-forwarders"

#: A forwarder: (semanticType, address argument index, handler argument index or -1, the
#: handler is the class itself (``registerHandler(address, this)``)).
Forwarder = tuple[SemanticType, int, int, bool]
#: A relay: method ``key`` passes its own parameters on to a method ``member``
#: (``name/arity``) of a class written ``owner`` in its file, as
#: ``((callee argument index, own parameter index), …)``; it forwards an address when the
#: callee does (``send(c, v, m, address, …)`` calling the full ``send`` overload). ``owner`` is
#: resolved like a call site's (:class:`_Names`), from the candidates kept with it.
Relay = tuple[str, tuple[str, ...], bool, str, tuple[tuple[int, int], ...]]
#: How many relays deep a forwarder is followed.
_RELAY_DEPTH = 4


# ── address forwarders ──────────────────────────────────────────────────────────────────────

def _params(decl: Node, source: bytes) -> list[str] | None:
    """Parameter names of a method / constructor; None with varargs (positions are unsure)."""
    params = decl.child_by_field_name("parameters")
    names: list[str] = []
    for p in params.named_children if params is not None else []:
        if p.type == "spread_parameter":
            return None
        if p.type == "formal_parameter":
            names.append(name_of(p.child_by_field_name("name"), source))
    return names


def _bus_call(call: Node, source: bytes) -> tuple[SemanticType, list[Node]] | None:
    """(semanticType, arguments) of an EventBus call with arguments, else None."""
    method = name_of(call.child_by_field_name("name"), source)
    if method not in EVENTBUS:
        return None
    obj = call.child_by_field_name("object")
    args = arguments(call)
    if obj is None or not is_bus_receiver(name_of(obj, source)) or not args:
        return None
    return EVENTBUS[method], args


def _handler_ref(semantic: SemanticType, args: list[Node], names: list[str],
                 source: bytes) -> tuple[int, bool]:
    """A consumer's handler argument as (parameter index or -1, is ``this``)."""
    if semantic != "eventbus_consumer" or len(args) < 2:
        return -1, False
    text = plain(name_of(args[1], source))
    if text == "this":
        return -1, True
    return (names.index(text), False) if text in names else (-1, False)


class _Names:
    """How a class name written in one file resolves to a fully-qualified one, as Java does: a
    single-type import; else a type of the file's own package (it shadows every on-demand
    import, forwarder or not — checked against the repo's types, ``JavaIndex.fqcn``); else the
    one ``known`` class among the on-demand (``.*``) imports. Anything else → None."""

    def __init__(self, root: Node, source: bytes, types: object | None = None) -> None:
        self.singles, self.wildcards, self.package = imports_and_package(root, source)
        self.types: dict[str, Any] = getattr(types, "fqcn", None) or {}

    def qualify(self, *chain: str) -> str:
        return ".".join((self.package, *chain)) if self.package else ".".join(chain)

    def candidates(self, written: str) -> tuple[tuple[str, ...], bool]:
        """(fully-qualified candidates, exact): exact for a single-type import or a name written
        fully qualified; else this package's name first, then each on-demand import's."""
        written = written.split("<", 1)[0].strip()
        head, _, rest = written.partition(".")
        tail = f".{rest}" if rest else ""
        single = next((i for i in self.singles if i.rsplit(".", 1)[-1] == head), None)
        if single is not None:
            return (single + tail,), True
        if rest and head[:1].islower():  # com.acme.Util as written
            return (written,), True
        return (self.qualify(written), *(f"{w}.{written}" for w in self.wildcards)), False

    def resolve(self, written: str, known: Collection[str]) -> str | None:
        found, exact = self.candidates(written)
        return pick(found, exact, known, self.types)


def pick(found: tuple[str, ...], exact: bool, known: Collection[str],
         types: dict[str, Any]) -> str | None:
    """The class a written name means, from :meth:`_Names.candidates` (see :class:`_Names`)."""
    if exact:
        return found[0] if found[0] in known else None
    own, wildcards = found[0], found[1:]
    if own in known:
        return own
    if _declared(own, types):  # a same-package type shadows every on-demand import
        return None
    hits = [c for c in wildcards if c in known]
    return hits[0] if len(hits) == 1 else None


def _declared(fqn: str, types: dict[str, Any]) -> bool:
    """A type of the repo declares ``fqn`` or its outer class: the repo's type index lists
    top-level types only (``pkg.Outer`` for ``pkg.Outer.Inner``)."""
    parts = fqn.split(".")
    return any(".".join(parts[:i]) in types for i in range(len(parts), 1, -1))


def _class_of(key: str) -> str:
    """``pkg.Class.method/arity`` → ``pkg.Class``."""
    return key.rsplit("/", 1)[0].rsplit(".", 1)[0]


def _callee(call: Node, here: str, names: _Names, source: bytes
            ) -> tuple[tuple[str, ...], bool, str] | None:
    """(owner candidates, exact, ``method/arity``) of a call on the class itself (unqualified
    or ``this.``) or on a class name (static); None otherwise."""
    obj = call.child_by_field_name("object")
    method = name_of(call.child_by_field_name("name"), source)
    member = f"{method}/{len(arguments(call))}"
    if not method:
        return None
    if obj is None or name_of(obj, source) == "this":
        return (here,), True, member
    if obj.type in ("identifier", "field_access") and name_of(obj, source)[:1].isalpha():
        text = name_of(obj, source)
        if text.rsplit(".", 1)[-1][:1].isupper():
            found, exact = names.candidates(text)
            return found, exact, member
    return None


class _Decl:
    """A method or constructor of a scanned class: its calls and the names assigned in it."""

    __slots__ = ("node", "calls", "assigned")

    def __init__(self, node: Node) -> None:
        self.node = node
        self.calls: list[Node] = []
        self.assigned: dict[str, list[Node]] = {}


class _Class:
    """A scanned class: its fully-qualified name, its members and every name assigned anywhere
    in its body (nested classes included)."""

    __slots__ = ("name", "body", "decls", "assigned")

    def __init__(self, name: str, body: Node) -> None:
        self.name = name
        self.body = body
        self.decls: list[_Decl] = []
        self.assigned: dict[str, list[Node]] = {}


def _scan(root: Node, source: bytes, names_of: _Names) -> list[_Class]:
    """One walk of the file: each class reached outside a method (a top-level one, or a member
    of a scanned class, named with its outer classes) with its methods and constructors, each
    method's calls (those in its anonymous / local classes too) and the assignments of each
    method and class (``this.`` dropped)."""
    classes: list[_Class] = []

    def walk(n: Node, stack: tuple[_Class, ...], chain: tuple[str, ...],
             decl: _Decl | None) -> None:
        kind = n.type
        if kind == "class_declaration" and decl is None \
                and (not stack or n.parent == stack[-1].body):
            simple = name_of(n.child_by_field_name("name"), source)
            body = n.child_by_field_name("body")
            if simple and body is not None:
                inner = (*chain, simple) if stack else (simple,)
                cls = _Class(names_of.qualify(*inner), body)
                classes.append(cls)
                for c in n.named_children:
                    walk(c, (*stack, cls), inner, None)
                return
        elif kind in ("method_declaration", "constructor_declaration") and decl is None:
            decl = _Decl(n)
            if stack and n.parent == stack[-1].body:
                stack[-1].decls.append(decl)
            # else a method of a class not scanned (an interface's, an enum's): read for its
            # class's assignments only, and a class inside it is not scanned
        elif kind == "method_invocation" and decl is not None:
            decl.calls.append(n)
        elif kind == "assignment_expression" and stack:
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None:
                name = plain(name_of(left, source))
                for cls in stack:
                    cls.assigned.setdefault(name, []).append(right)
                if decl is not None:
                    decl.assigned.setdefault(name, []).append(right)
        for c in n.named_children:
            walk(c, stack, chain, decl)

    walk(root, (), (), None)
    return classes


def _forwarders(root: Node, source: bytes) -> tuple[list[tuple[str, Forwarder]], list[Relay]]:
    """This file's address forwarders by ``Class.method/arity`` and its relays (calls passing a
    parameter on to another method of a class, resolved repo-wide). One walk (:func:`_scan`);
    each method's calls and assignments are then read from what it collected."""
    found: list[tuple[str, Forwarder]] = []
    relays: list[Relay] = []
    names_of = _Names(root, source)
    for cls in _scan(root, source, names_of):
        params = {id(d): _params(d.node, source) for d in cls.decls}
        # Fields set from a constructor parameter, and assigned nowhere else in the class.
        ctor_fields: dict[str, list[tuple[Node, int]]] = {}
        for d in cls.decls:
            names = params[id(d)]
            if d.node.type != "constructor_declaration" or names is None \
                    or d.node.child_by_field_name("body") is None:
                continue
            for field, values in d.assigned.items():
                if len(values) == 1 and values[0].type == "identifier" \
                        and name_of(values[0], source) in names:
                    ctor_fields.setdefault(field, []).append(
                        (d.node, names.index(name_of(values[0], source))))
        stable = {f: v for f, v in ctor_fields.items()
                  if len(cls.assigned.get(f, ())) == len(v)}
        for d in cls.decls:
            names = params[id(d)]
            if names is None or d.node.child_by_field_name("body") is None:
                continue
            reassigned = d.assigned
            ctor = d.node.type == "constructor_declaration"
            mname = "<init>" if ctor else name_of(d.node.child_by_field_name("name"), source)
            key = f"{cls.name}.{mname}/{len(names)}"
            for call in d.calls:
                args = arguments(call)
                mapping = tuple(
                    (i, names.index(name_of(a, source))) for i, a in enumerate(args)
                    if a.type == "identifier" and name_of(a, source) in names
                    and name_of(a, source) not in reassigned)
                callee = _callee(call, cls.name, names_of, source) if mapping else None
                if callee is not None and not (callee[1] and f"{callee[0][0]}.{callee[2]}" == key):
                    relays.append((key, callee[0], callee[1], callee[2], mapping))
                bus = _bus_call(call, source)
                if bus is None:
                    continue
                semantic, args = bus
                addr = name_of(args[0], source)
                if args[0].type == "identifier" and addr in names and addr not in reassigned:
                    hidx, self_ = _handler_ref(semantic, args, names, source)
                    found.append((key, (semantic, names.index(addr), hidx, self_)))
                elif args[0].type in ("identifier", "field_access") and addr not in names \
                        and plain(addr) in stable and not ctor \
                        and semantic == "eventbus_consumer" and len(args) >= 2 \
                        and plain(name_of(args[1], source)) == "this":
                    # A class registering itself on the address its constructor was given: the
                    # construction is the registration. A send / publish through the field
                    # stays at its own call site (one per method of a proxy).
                    for node, index in stable[plain(addr)]:
                        arity = len(_params(node, source) or [])
                        found.append((f"{cls.name}.<init>/{arity}",
                                      (semantic, index, -1, True)))
    return found, relays


def _collect_forwarders(root: Node, source: bytes, rel: str, repo_root: Path | None
                        ) -> tuple[list[tuple[str, Forwarder]], list[Relay]] | None:
    found, relays = _forwarders(root, source)
    return (found, relays) if found or relays else None


def _resolve_forwarders(values: list[tuple[list[tuple[str, Forwarder]], list[Relay]]],
                        index: object) -> dict[str, Forwarder] | None:
    """Merged across files; a key declared twice with a different shape is dropped (unsure).
    Relays are then followed (at most :data:`_RELAY_DEPTH` deep, independent of file order):
    a method passing its parameter into a forwarder's address position is a forwarder too."""
    merged: dict[str, Forwarder | None] = {}
    relays: set[Relay] = set()
    for direct, relayed in values:
        for key, value in direct:
            record_distinct(merged, key, value)
        relays.update(relayed)
    known = {k: v for k, v in merged.items() if v is not None}
    types: dict[str, Any] = getattr(index, "fqcn", None) or {}
    for _ in range(_RELAY_DEPTH):
        derived: dict[str, Forwarder | None] = {}
        classes = {_class_of(k) for k in known}
        for key, owners, exact, member, mapping in sorted(relays):
            owner = pick(owners, exact, classes, types)
            fwd = known.get(f"{owner}.{member}") if owner is not None else None
            if key in merged or fwd is None:
                continue
            semantic, addr, hidx, self_ = fwd
            own = dict(mapping)
            if addr in own:
                # A handler that is the callee's own class is not this method's: none (unsure).
                record_distinct(derived, key, (semantic, own[addr],
                                               own.get(hidx, -1) if hidx >= 0 else -1, False))
        new = {k: v for k, v in derived.items() if v is not None and k not in known}
        if not new:
            break
        known.update(new)
        merged.update(derived)
    return known or None


register_fact(FactSpec(
    name=FORWARDERS, language="java", gate=(b"org.vertx",),  # Vert.x 2 only: 3.x+ output unchanged
    collect=_collect_forwarders, resolve=_resolve_forwarders,
))


def _enclosing_classes(node: Node, source: bytes, names: _Names) -> list[str]:
    """Fully-qualified names of the classes around ``node``, innermost first."""
    chain: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type == "class_declaration":
            chain.insert(0, name_of(parent.child_by_field_name("name"), source))
        parent = parent.parent
    return [names.qualify(*chain[:i]) for i in range(len(chain), 0, -1)]


def forwarder_sites(
    root: Node, calls: list[Node], creations: list[Node], source: bytes, path: str, record: FileRecord,
    consts: dict[str, str], index: object | None, skip: set[tuple[int, int]],
) -> tuple[list[Emit], set[tuple[int, int]]]:
    """The records of every call to a known forwarder in this file — a method call on the
    class itself, on a class name (static) or on a variable declared with the class's type,
    or ``new Class(…)`` — and their spans (so they are not classified again). ``calls`` and
    ``creations`` are the file's method invocations and object creations (one walk, shared
    with the rest of the Vert.x detection)."""
    table: dict[str, Forwarder] = index_fact(index, FORWARDERS) or {}
    if not table:
        return [], set()
    # Cheap first checks: the method name / arity, and the constructed type's simple name.
    members = {k.rsplit(".", 1)[-1] for k in table}
    classes = {_class_of(k) for k in table}
    simple = {c.rsplit(".", 1)[-1] for c in classes}
    names_of = _Names(root, source, index)
    typed = typed_vars(root, source, simple)
    methods = method_lines(record.functions)
    found: list[Emit] = []
    spans: set[tuple[int, int]] = set()

    def emit(node: Node, cls: str, fwd: Forwarder, args: list[Node]) -> None:
        # A relay's own call (an overload passing its address parameter on) is a record too,
        # its address unknown: like the helper's own call, it is kept whoever calls it.
        semantic, addr, hidx, self_ = fwd
        endpoint = known_address(args[addr], source, consts) if addr < len(args) else None
        handler: str | None = None
        line: int | None = None
        handler_path: str | None = None
        if semantic == "eventbus_consumer":
            impl = cls if self_ else None
            if hidx >= 0 and hidx < len(args):
                handler, line = handler_info(args[hidx], source, methods)
                if handler is None and line is None:
                    impl = handler_class(args[hidx], source)
            if impl:
                handler = "handle"
                line, handler_path = handle_location(index, impl, path)
        found.append(vertx_emit(node, semantic, None, endpoint, None, handler, line,
                                handler_path=handler_path))
        spans.add((node.start_byte, node.end_byte))

    def call_site(n: Node) -> None:
        args = arguments(n)
        method = name_of(n.child_by_field_name("name"), source)
        if f"{method}/{len(args)}" not in members:
            return
        obj = n.child_by_field_name("object")
        owners: list[str] = []
        if obj is None or name_of(obj, source) == "this":
            owners = _enclosing_classes(n, source, names_of)
        elif obj.type in ("identifier", "field_access"):
            text = plain(name_of(obj, source))
            written = typed.get(text) or (
                text if text.rsplit(".", 1)[-1][:1].isupper() else None)
            resolved = names_of.resolve(written, classes) if written else None
            owners = [resolved] if resolved else []
        cls = next((c for c in owners if f"{c}.{method}/{len(args)}" in table), None)
        if cls is not None:
            emit(n, cls, table[f"{cls}.{method}/{len(args)}"], args)

    def creation_site(n: Node) -> None:
        if any(c.type == "class_body" for c in n.named_children):
            return
        typ = n.child_by_field_name("type")
        written = name_of(typ, source) if typ is not None else ""
        if written.split("<", 1)[0].rsplit(".", 1)[-1] not in simple:
            return
        resolved = names_of.resolve(written, classes) if written else None
        args = arguments(n)
        fwd = table.get(f"{resolved}.<init>/{len(args)}") if resolved else None
        if resolved is not None and fwd is not None:
            emit(n, resolved, fwd, args)

    # In source order (a node before the nodes inside it), as one walk would visit them.
    for n in sorted((*calls, *creations), key=lambda n: (n.start_byte, -n.end_byte)):
        if (n.start_byte, n.end_byte) in skip:
            continue
        if n.type == "method_invocation":
            call_site(n)
        else:
            creation_site(n)
    return found, spans
