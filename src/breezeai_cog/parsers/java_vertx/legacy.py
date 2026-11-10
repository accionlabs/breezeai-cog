"""Legacy Vert.x 2 (``org.vertx.java``) idioms that hide the EventBus address or the HTTP call
behind ordinary Java, read over the Java AST and composed into ``detect_vertx``.

**Registration tables.** A service registers its handlers through a map and a loop, so the
addresses are the map's keys, put elsewhere in the file::

    static { handlers.put(OrderFilter.ADDRESS, new OrderFilter()); }      // ← the consumers
    for (Map.Entry<String, BusModBase> e : handlers.entrySet()) {
        String name = e.getKey();
        vertx.eventBus().registerHandler(name, …);                        // ← address = a key
    }

A map whose keys reach ``registerHandler`` (directly, through a local assigned the entry's key,
or as the loop variable over ``keySet()``) is a registration table: each ``put(KEY, VALUE)`` into
it is one consumer, ``endpoint`` = KEY folded, handler = ``handle`` of VALUE's class when VALUE is
``new X(…)``. The loop's own ``registerHandler`` is then wiring, not a record. A map whose keys
never reach ``registerHandler`` is not one; nor is a class merely extending a handler base.

**Address forwarders.** A method or constructor that passes one of its parameters straight on
as the address of an EventBus call (``send(…, String address, …)`` →
``sendWithTimeout(address, …)``, or a constructor storing ``this.address = address`` for a
later ``registerHandler(address, this)``). The ``vertx.address-forwarders`` fact lists them
repo-wide by ``Class.method/arity`` (``Class.<init>/arity``); each call site is then the
record, with its own argument folded (unfoldable → ``endpoint`` empty) and the forwarder's own
call suppressed (its address is a parameter). Shapes only, never names.

**HttpClient calls.** ``get`` / ``post`` / ``getNow`` / ``request(method, uri, …)`` … on a
variable declared as a Vert.x 2 ``org.vertx.java.core.http.HttpClient`` (or on an in-file
method returning one) are ``api_call`` records; the URI is folded when constant, else empty.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...schemas import FileRecord, SemanticType
from ..additive import FactSpec, index_fact, register_fact
from ..emits import Emit
from ..handler_sites import handler_location, locate_method
from ..index_common import record_distinct
from ..java.variables import base_type, typed_vars
from ..treesitter import node_text
from ..vertx_common import (EVENTBUS, handler_info, is_bus_receiver, method_lines, render_address,
                            vertx_emit)
from .events import _handler_class, _invocations, _java_string, _local_value

#: EventBus methods that register a consumer (Vert.x 2 ``registerHandler`` family, 3.x names).
_CONSUMERS = frozenset(m for m, s in EVENTBUS.items() if s == "eventbus_consumer")

FORWARDERS = "vertx.address-forwarders"

#: A forwarder: (semanticType, address argument index, handler argument index or -1, the
#: handler is the class itself (``registerHandler(address, this)``)).
Forwarder = tuple[SemanticType, int, int, bool]
#: A relay: method ``key`` passes its own parameters on to method ``callee``, as
#: ``((callee argument index, own parameter index), …)``; it forwards an address when the
#: callee does (``send(c, v, m, address, …)`` calling the full ``send`` overload).
Relay = tuple[str, str, tuple[tuple[int, int], ...]]
#: How many relays deep a forwarder is followed.
_RELAY_DEPTH = 4


def _name(node: Node | None, source: bytes) -> str:
    return node_text(node, source) if node is not None else ""


def _plain(text: str) -> str:
    return text.removeprefix("this.").strip()


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def _known(node: Node, source: bytes, consts: dict[str, str]) -> str | None:
    """An address / URI argument folded to a constant, else None (never a ``{name}``)."""
    value = render_address(node, source, consts, _java_string)
    return value if value is not None and "{" not in value else None


def _located(index: object | None, impl: str | None, path: str) -> tuple[int | None, str | None]:
    found = locate_method(index, "java", impl, "handle") if impl else None
    return handler_location(found, path)


# ── registration tables ─────────────────────────────────────────────────────────────────────

def _loop_over(node: Node, var: str, source: bytes, iterator: str) -> str | None:
    """The map ``M`` when ``var`` is the variable of an enclosing ``for (… var : M.<iterator>())``."""
    parent = node.parent
    while parent is not None:
        if parent.type == "enhanced_for_statement" \
                and _name(parent.child_by_field_name("name"), source) == var:
            value = parent.child_by_field_name("value")
            if value is not None and value.type == "method_invocation" \
                    and _name(value.child_by_field_name("name"), source) == iterator \
                    and not _args(value) and value.child_by_field_name("object") is not None:
                return _plain(_name(value.child_by_field_name("object"), source))
            return None
        parent = parent.parent
    return None


def _table_of(addr: Node, source: bytes) -> str | None:
    """The map whose keys an address argument is: ``e.getKey()`` over ``M.entrySet()``, a local
    assigned exactly that, or the loop variable over ``M.keySet()``. Anything else → None."""
    node: Node | None = addr
    if addr.type == "identifier":
        var = _name(addr, source)
        by_keys = _loop_over(addr, var, source, "keySet")
        if by_keys is not None:
            return by_keys
        node = _local_value(addr, var, source)
    if node is None or node.type != "method_invocation" or _args(node) \
            or _name(node.child_by_field_name("name"), source) != "getKey":
        return None
    entry = node.child_by_field_name("object")
    if entry is None or entry.type != "identifier":
        return None
    return _loop_over(node, _name(entry, source), source, "entrySet")


def registration_tables(
    calls: list[Node], source: bytes, consts: dict[str, str], index: object | None, path: str,
) -> tuple[list[Emit], set[tuple[int, int]]]:
    """One ``eventbus_consumer`` per ``put`` into a registration table (module docstring), and
    the spans of the loop ``registerHandler`` calls those records replace."""
    loops: dict[str, list[Node]] = {}
    for call in calls:
        obj = call.child_by_field_name("object")
        args = _args(call)
        if _name(call.child_by_field_name("name"), source) in _CONSUMERS and obj is not None \
                and is_bus_receiver(_name(obj, source)) and args:
            table = _table_of(args[0], source)
            if table is not None:
                loops.setdefault(table, []).append(call)
    if not loops:
        return [], set()
    found: list[Emit] = []
    replaced: set[tuple[int, int]] = set()
    for table, registrations in loops.items():
        puts = [c for c in calls if _name(c.child_by_field_name("name"), source) == "put"
                and len(_args(c)) == 2
                and _plain(_name(c.child_by_field_name("object"), source)) == table]
        if not puts:  # filled elsewhere: the loop's own record stays
            continue
        replaced |= {(c.start_byte, c.end_byte) for c in registrations}
        for put in puts:
            key, value = _args(put)
            impl = _handler_class(value, source)
            line, handler_path = _located(index, impl, path)
            found.append(vertx_emit(put, "eventbus_consumer", None, _known(key, source, consts),
                                    None, "handle" if impl else None, line,
                                    handler_path=handler_path))
    return found, replaced


# ── address forwarders ──────────────────────────────────────────────────────────────────────

def _params(decl: Node, source: bytes) -> list[str] | None:
    """Parameter names of a method / constructor; None with varargs (positions are unsure)."""
    params = decl.child_by_field_name("parameters")
    names: list[str] = []
    for p in params.named_children if params is not None else []:
        if p.type == "spread_parameter":
            return None
        if p.type == "formal_parameter":
            names.append(_name(p.child_by_field_name("name"), source))
    return names


def _assigned(body: Node, source: bytes) -> dict[str, list[Node]]:
    """Plain name (``this.`` dropped) → the right-hand sides assigned to it in ``body``."""
    out: dict[str, list[Node]] = {}

    def walk(n: Node) -> None:
        if n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None:
                out.setdefault(_plain(_name(left, source)), []).append(right)
        for c in n.named_children:
            walk(c)

    walk(body)
    return out


def _bus_calls(body: Node, source: bytes) -> list[tuple[Node, SemanticType, list[Node]]]:
    out = []
    for call in _invocations(body):
        method = _name(call.child_by_field_name("name"), source)
        obj = call.child_by_field_name("object")
        args = _args(call)
        if method in EVENTBUS and obj is not None and is_bus_receiver(_name(obj, source)) \
                and args:
            out.append((call, EVENTBUS[method], args))
    return out


def _handler_ref(semantic: SemanticType, args: list[Node], names: list[str],
                 source: bytes) -> tuple[int, bool]:
    """A consumer's handler argument as (parameter index or -1, is ``this``)."""
    if semantic != "eventbus_consumer" or len(args) < 2:
        return -1, False
    text = _plain(_name(args[1], source))
    if text == "this":
        return -1, True
    return (names.index(text), False) if text in names else (-1, False)


def _callee_key(call: Node, cname: str, source: bytes) -> str | None:
    """``Class.method/arity`` of a call on the class itself (unqualified or ``this.``) or on a
    class name (static); None otherwise."""
    obj = call.child_by_field_name("object")
    owner = cname if obj is None or _name(obj, source) == "this" else \
        _name(obj, source) if obj.type == "identifier" and _name(obj, source)[:1].isupper() \
        else None
    method = _name(call.child_by_field_name("name"), source)
    return f"{owner}.{method}/{len(_args(call))}" if owner and method else None


def _forwarders(root: Node, source: bytes) -> tuple[
        list[tuple[str, Forwarder]], list[Relay], set[tuple[int, int]]]:
    """This file's address forwarders by ``Class.method/arity``, its relays (calls passing a
    parameter on to another method of a class, resolved repo-wide), and the spans of the
    forwarders' own EventBus calls (whose address is a parameter)."""
    found: list[tuple[str, Forwarder]] = []
    relays: list[Relay] = []
    spans: set[tuple[int, int]] = set()

    def visit(cls: Node) -> None:
        cname = _name(cls.child_by_field_name("name"), source)
        body = cls.child_by_field_name("body")
        if not cname or body is None:
            return
        decls = [m for m in body.named_children
                 if m.type in ("method_declaration", "constructor_declaration")]
        ctors = [d for d in decls if d.type == "constructor_declaration"]
        # Fields set from a constructor parameter, and assigned nowhere else in the class.
        everywhere = _assigned(body, source)
        ctor_fields: dict[str, list[tuple[Node, int]]] = {}
        for ctor in ctors:
            names = _params(ctor, source)
            cbody = ctor.child_by_field_name("body")
            if names is None or cbody is None:
                continue
            for field, values in _assigned(cbody, source).items():
                if len(values) == 1 and values[0].type == "identifier" \
                        and _name(values[0], source) in names:
                    ctor_fields.setdefault(field, []).append(
                        (ctor, names.index(_name(values[0], source))))
        stable = {f: v for f, v in ctor_fields.items() if len(everywhere.get(f, ())) == len(v)}
        for decl in decls:
            names = _params(decl, source)
            dbody = decl.child_by_field_name("body")
            if names is None or dbody is None:
                continue
            reassigned = _assigned(dbody, source)
            mname = "<init>" if decl.type == "constructor_declaration" \
                else _name(decl.child_by_field_name("name"), source)
            key = f"{cname}.{mname}/{len(names)}"
            for call in _invocations(dbody):
                callee = _callee_key(call, cname, source)
                mapping = tuple(
                    (i, names.index(_name(a, source))) for i, a in enumerate(_args(call))
                    if a.type == "identifier" and _name(a, source) in names
                    and _name(a, source) not in reassigned)
                if callee is not None and callee != key and mapping:
                    relays.append((key, callee, mapping))
            for call, semantic, args in _bus_calls(dbody, source):
                addr = _name(args[0], source)
                if args[0].type == "identifier" and addr in names and addr not in reassigned:
                    hidx, self_ = _handler_ref(semantic, args, names, source)
                    found.append((f"{cname}.{mname}/{len(names)}",
                                  (semantic, names.index(addr), hidx, self_)))
                    spans.add((call.start_byte, call.end_byte))
                elif args[0].type in ("identifier", "field_access") and addr not in names \
                        and _plain(addr) in stable and decl.type == "method_declaration":
                    self_ = semantic == "eventbus_consumer" and len(args) >= 2 \
                        and _name(args[1], source) == "this"
                    for ctor, index in stable[_plain(addr)]:
                        arity = len(_params(ctor, source) or [])
                        found.append((f"{cname}.<init>/{arity}", (semantic, index, -1, self_)))
                    spans.add((call.start_byte, call.end_byte))
        for member in body.named_children:  # nested classes
            if member.type == "class_declaration":
                visit(member)

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "class_declaration":
                visit(c)
            elif c.type not in ("method_declaration", "constructor_declaration"):
                walk(c)

    walk(root)
    return found, relays, spans


def _collect_forwarders(root: Node, source: bytes, rel: str, repo_root: Path | None
                        ) -> tuple[list[tuple[str, Forwarder]], list[Relay]] | None:
    found, relays, _ = _forwarders(root, source)
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
    for _ in range(_RELAY_DEPTH):
        derived: dict[str, Forwarder | None] = {}
        for key, callee, mapping in sorted(relays):
            fwd = known.get(callee)
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
    name=FORWARDERS, language="java", gate=(b"org.vertx", b"io.vertx"),
    collect=_collect_forwarders, resolve=_resolve_forwarders,
))


def forwarder_spans(root: Node, source: bytes) -> set[tuple[int, int]]:
    """Spans of this file's forwarders' own EventBus calls (suppressed: the address is a
    parameter, resolved at each call site instead)."""
    return _forwarders(root, source)[2]


def _enclosing_classes(node: Node, source: bytes) -> list[str]:
    names: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type == "class_declaration":
            names.append(_name(parent.child_by_field_name("name"), source))
        parent = parent.parent
    return names  # innermost first


def forwarder_sites(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    index: object | None, skip: set[tuple[int, int]],
) -> tuple[list[Emit], set[tuple[int, int]]]:
    """The records of every call to a known forwarder in this file — a method call on the
    class itself, on a class name (static) or on a variable declared with the class's type,
    or ``new Class(…)`` — and their spans (so they are not classified again)."""
    table: dict[str, Forwarder] = index_fact(index, FORWARDERS) or {}
    if not table:
        return [], set()
    classes = {k.split(".", 1)[0] for k in table}
    typed = typed_vars(root, source, classes)
    methods = method_lines(record.functions)
    found: list[Emit] = []
    spans: set[tuple[int, int]] = set()

    def relayed(node: Node, arg: Node) -> bool:
        """``arg`` is a parameter of the enclosing method, itself a known forwarder: this call
        is that forwarder's plumbing, recorded at its callers instead."""
        decl = node.parent
        while decl is not None and decl.type not in ("method_declaration",
                                                     "constructor_declaration"):
            decl = decl.parent
        names = _params(decl, source) if decl is not None else None
        owners = _enclosing_classes(decl, source) if decl is not None else []
        if decl is None or names is None or not owners or arg.type != "identifier" \
                or _name(arg, source) not in names:
            return False
        mname = "<init>" if decl.type == "constructor_declaration" \
            else _name(decl.child_by_field_name("name"), source)
        return f"{owners[0]}.{mname}/{len(names)}" in table

    def emit(node: Node, cls: str, fwd: Forwarder, args: list[Node]) -> None:
        semantic, addr, hidx, self_ = fwd
        if addr < len(args) and relayed(node, args[addr]):
            spans.add((node.start_byte, node.end_byte))
            return
        endpoint = _known(args[addr], source, consts) if addr < len(args) else None
        handler: str | None = None
        line: int | None = None
        handler_path: str | None = None
        if semantic == "eventbus_consumer":
            impl = cls if self_ else None
            if hidx >= 0 and hidx < len(args):
                handler, line = handler_info(args[hidx], source, methods)
                if handler is None and line is None:
                    impl = _handler_class(args[hidx], source)
            if impl:
                handler = "handle"
                line, handler_path = _located(index, impl, path)
        found.append(vertx_emit(node, semantic, None, endpoint, None, handler, line,
                                handler_path=handler_path))
        spans.add((node.start_byte, node.end_byte))

    def walk(n: Node) -> None:
        span = (n.start_byte, n.end_byte)
        if span not in skip:
            if n.type == "method_invocation":
                args = _args(n)
                method = _name(n.child_by_field_name("name"), source)
                obj = n.child_by_field_name("object")
                owners: list[str] = []
                if obj is None or _name(obj, source) == "this":
                    owners = _enclosing_classes(n, source)
                elif obj.type in ("identifier", "field_access"):
                    text = _plain(_name(obj, source))
                    owners = [typed[text]] if text in typed else \
                        [text] if obj.type == "identifier" and text[:1].isupper() else []
                cls = next((c for c in owners if f"{c}.{method}/{len(args)}" in table), None)
                if cls is not None:
                    emit(n, cls, table[f"{cls}.{method}/{len(args)}"], args)
            elif n.type == "object_creation_expression" \
                    and not any(c.type == "class_body" for c in n.named_children):
                cls = base_type(n.child_by_field_name("type"), source)
                args = _args(n)
                fwd = table.get(f"{cls}.<init>/{len(args)}")
                if fwd is not None:
                    emit(n, cls, fwd, args)
        for c in n.named_children:
            walk(c)

    walk(root)
    return found, spans


# ── HttpClient calls ────────────────────────────────────────────────────────────────────────

#: Vert.x 2 ``HttpClient`` request methods → HTTP method (``request`` takes the method first).
_HTTP_CLIENT = {"get": "GET", "getNow": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
                "head": "HEAD", "options": "OPTIONS", "patch": "PATCH", "trace": "TRACE",
                "connect": "CONNECT"}
_VERBS = frozenset(_HTTP_CLIENT.values())


def _vertx2_http_client(root: Node, source: bytes) -> bool:
    """The file imports the Vert.x 2 ``HttpClient`` (the type or its package)."""
    for node in root.named_children:
        if node.type == "import_declaration":
            text = node_text(node, source).removeprefix("import").strip().rstrip(";").strip()
            if text in ("org.vertx.java.core.http.HttpClient", "org.vertx.java.core.http.*"):
                return True
    return False


def http_client_calls(
    root: Node, source: bytes, record: FileRecord, consts: dict[str, str],
) -> list[Emit]:
    """An ``api_call`` for each request made on a Vert.x 2 ``HttpClient`` (module docstring)."""
    if b"org.vertx.java.core.http" not in source or not _vertx2_http_client(root, source):
        return []
    clients = typed_vars(root, source, {"HttpClient"})
    returning = {f.name for f in record.functions
                 if f.returnType and f.returnType.split("<", 1)[0].rsplit(".", 1)[-1]
                 == "HttpClient"}
    found: list[Emit] = []
    for call in _invocations(root):
        method = _name(call.child_by_field_name("name"), source)
        if method not in _HTTP_CLIENT and method != "request":
            continue
        obj = call.child_by_field_name("object")
        if obj is None:
            continue
        if obj.type in ("identifier", "field_access"):
            is_client = _plain(_name(obj, source)) in clients
        else:
            inner = obj.child_by_field_name("object") if obj.type == "method_invocation" else None
            is_client = obj.type == "method_invocation" \
                and _name(obj.child_by_field_name("name"), source) in returning \
                and (inner is None or _name(inner, source) == "this")
        args = _args(call)
        if not is_client or not args:
            continue
        if method == "request":
            if len(args) < 2:
                continue
            verb_text = _known(args[0], source, consts)
            verb = verb_text.upper() if verb_text and verb_text.upper() in _VERBS else None
            uri = args[1]
        else:
            verb, uri = _HTTP_CLIENT[method], args[0]
        found.append(Emit(call, {
            "semanticType": "api_call", "framework": "vertx", "method": verb,
            "endpoint": _known(uri, source, consts),
        }, enrich="unclassified", reclassify=frozenset({"api_call", "db_method_call"})))
    return found
