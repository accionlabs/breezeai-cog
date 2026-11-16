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

**HttpClient calls.** ``get`` / ``post`` / ``getNow`` / ``request(method, uri, …)`` … on a
variable declared as a Vert.x 2 ``org.vertx.java.core.http.HttpClient`` (or on an in-file
method returning one) are ``api_call`` records; the URI is folded when constant, else empty.

An address passed through a helper method or constructor is :mod:`.forwarders`.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit
from ..java_variables_common import typed_vars
from ..treesitter import node_text
from ..vertx_common import EVENTBUS, is_bus_receiver, vertx_emit
from .handler_factories import FactoryCalls
from .java_syntax import (arguments, handle_location, handler_class, known_address, local_value,
                          name_of, plain)

_CONSUMERS = frozenset(m for m, s in EVENTBUS.items() if s == "eventbus_consumer")


# ── registration tables ─────────────────────────────────────────────────────────────────────

def _loop_over(node: Node, var: str, source: bytes, iterator: str) -> str | None:
    """The map ``M`` when ``var`` is the variable of an enclosing ``for (… var : M.<iterator>())``."""
    parent = node.parent
    while parent is not None:
        if parent.type == "enhanced_for_statement" \
                and name_of(parent.child_by_field_name("name"), source) == var:
            value = parent.child_by_field_name("value")
            if value is not None and value.type == "method_invocation" \
                    and name_of(value.child_by_field_name("name"), source) == iterator \
                    and not arguments(value) and value.child_by_field_name("object") is not None:
                return plain(name_of(value.child_by_field_name("object"), source))
            return None
        parent = parent.parent
    return None


def _table_of(addr: Node, source: bytes) -> str | None:
    """The map whose keys an address argument is: ``e.getKey()`` over ``M.entrySet()``, a local
    assigned exactly that, or the loop variable over ``M.keySet()``. Anything else → None."""
    node: Node | None = addr
    if addr.type == "identifier":
        var = name_of(addr, source)
        by_keys = _loop_over(addr, var, source, "keySet")
        if by_keys is not None:
            return by_keys
        node = local_value(addr, var, source)
    if node is None or node.type != "method_invocation" or arguments(node) \
            or name_of(node.child_by_field_name("name"), source) != "getKey":
        return None
    entry = node.child_by_field_name("object")
    if entry is None or entry.type != "identifier":
        return None
    return _loop_over(node, name_of(entry, source), source, "entrySet")


def registration_tables(
    calls: list[Node], source: bytes, consts: dict[str, str], index: object | None, path: str,
    factories: FactoryCalls | None = None,
) -> tuple[list[Emit], set[tuple[int, int]]]:
    """One ``eventbus_consumer`` per ``put`` into a registration table (module docstring), and
    the spans of the loop ``registerHandler`` calls those records replace. ``factories``: the
    handler factories this file calls (a value ``X.create()`` is handled like ``new T()``)."""
    loops: dict[str, list[Node]] = {}
    for call in calls:
        obj = call.child_by_field_name("object")
        args = arguments(call)
        if name_of(call.child_by_field_name("name"), source) in _CONSUMERS and obj is not None \
                and is_bus_receiver(name_of(obj, source)) and args:
            table = _table_of(args[0], source)
            if table is not None:
                loops.setdefault(table, []).append(call)
    if not loops:
        return [], set()
    found: list[Emit] = []
    replaced: set[tuple[int, int]] = set()
    for table, registrations in loops.items():
        puts = [c for c in calls if name_of(c.child_by_field_name("name"), source) == "put"
                and len(arguments(c)) == 2
                and plain(name_of(c.child_by_field_name("object"), source)) == table]
        if not puts:  # filled elsewhere: the loop's own record stays
            continue
        replaced |= {(c.start_byte, c.end_byte) for c in registrations}
        for put in puts:
            key, value = arguments(put)
            impl = handler_class(value, source) or (
                factories.built(value) if factories is not None else None)
            line, handler_path = handle_location(index, impl, path)
            found.append(vertx_emit(put, "eventbus_consumer", None, known_address(key, source, consts),
                                    None, "handle" if impl else None, line,
                                    handler_path=handler_path))
    return found, replaced


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
    root: Node, calls: list[Node], source: bytes, record: FileRecord, consts: dict[str, str],
) -> list[Emit]:
    """An ``api_call`` for each request made on a Vert.x 2 ``HttpClient`` (module docstring)."""
    if b"org.vertx.java.core.http" not in source or not _vertx2_http_client(root, source):
        return []
    clients = typed_vars(root, source, {"HttpClient"})
    returning = {f.name for f in record.functions
                 if f.returnType and f.returnType.split("<", 1)[0].rsplit(".", 1)[-1]
                 == "HttpClient"}
    found: list[Emit] = []
    for call in calls:
        method = name_of(call.child_by_field_name("name"), source)
        if method not in _HTTP_CLIENT and method != "request":
            continue
        obj = call.child_by_field_name("object")
        if obj is None:
            continue
        if obj.type in ("identifier", "field_access"):
            is_client = plain(name_of(obj, source)) in clients
        else:
            inner = obj.child_by_field_name("object") if obj.type == "method_invocation" else None
            is_client = obj.type == "method_invocation" \
                and name_of(obj.child_by_field_name("name"), source) in returning \
                and (inner is None or name_of(inner, source) == "this")
        args = arguments(call)
        if not is_client or not args:
            continue
        if method == "request":
            if len(args) < 2:
                continue
            verb_text = known_address(args[0], source, consts)
            verb = verb_text.upper() if verb_text and verb_text.upper() in _VERBS else None
            uri = args[1]
        else:
            verb, uri = _HTTP_CLIENT[method], args[0]
        found.append(Emit(call, {
            "semanticType": "api_call", "framework": "vertx", "method": verb,
            "endpoint": known_address(uri, source, consts),
        }, enrich="unclassified", reclassify=frozenset({"api_call", "db_method_call"})))
    return found
