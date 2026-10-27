"""Semantic classification of Apex call-like nodes (SOQL/SOSL, DML, callouts, jobs, events).

The shared detectors key off Java/JS-style method names and quoted SQL, so they miss
Apex's inline ``[SELECT …]``, DML keywords (``insert acc;``), ``Database.insert`` and
``Http.send``. This module classifies those directly from the syntax tree. Names are
compared in lower case because Apex is case-insensitive.
"""

from __future__ import annotations

from tree_sitter import Node

from ..statements_common import render_concat
from ..treesitter import node_text
from .modifiers import extract_annotations, has_annotation, modifier_keywords, modifiers_node

#: ``(semanticType, method, endpoint, dataAccessHint)``
Hit = tuple[str, str | None, str | None, str | None]

CALL_TYPES = frozenset({"method_invocation", "dml_expression", "query_expression"})

_DML_METHODS = frozenset({"insert", "update", "upsert", "delete", "undelete", "merge"})
#: Lower-cased ``Database`` query method → its documented spelling, which is what is emitted.
_QUERY_METHODS = {
    name.lower(): name
    for name in ("query", "countQuery", "getQueryLocator", "queryWithBinds",
                 "countQueryWithBinds", "getQueryLocatorWithBinds")
}
_DATA_ACCESS_HINT = "salesforce"
_FUNCTION_SCOPES = ("method_declaration", "constructor_declaration", "trigger_declaration")
_TYPE_SCOPES = ("class_declaration", "interface_declaration", "enum_declaration")


def _receiver(call: Node, source: bytes) -> str:
    """Lower-cased receiver text with an explicit ``System.`` namespace removed, so
    ``System.Database.insert`` reads the same as ``Database.insert``."""
    obj = call.child_by_field_name("object")
    text = "".join(node_text(obj, source).lower().split()) if obj is not None else ""
    return text.removeprefix("system.") if text != "system" else text


def _enclosing(node: Node, types: tuple[str, ...]) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type not in types:
        cur = cur.parent
    return cur


def _declarators(scope: Node, kinds: tuple[str, ...], *, stop: tuple[str, ...]) -> list[tuple[Node, Node]]:
    """``(name, type)`` of every ``kinds`` declaration in ``scope``, not descending into
    ``stop`` scopes."""
    out: list[tuple[Node, Node]] = []
    stack = list(scope.named_children)
    while stack:
        node = stack.pop()
        if node.type in stop:
            continue
        typ = node.child_by_field_name("type")
        if node.type == "formal_parameter" and "formal_parameter" in kinds and typ is not None:
            name = node.child_by_field_name("name")
            if name is not None:
                out.append((name, typ))
        elif node.type in kinds and typ is not None:
            for d in node.named_children:
                name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                if name is not None:
                    out.append((name, typ))
        stack.extend(node.named_children)
    return out


def declared_type(name: str, at: Node, source: bytes) -> str | None:
    """Declared type (source spelling) of variable ``name`` as seen from ``at``: a parameter or
    local of the enclosing method, else a field of the enclosing class. ``None`` when it is
    not declared there or is declared twice with different types."""
    want = name.lower()
    fn = _enclosing(at, _FUNCTION_SCOPES)
    scopes: list[tuple[Node, tuple[str, ...]]] = []
    if fn is not None:
        scopes.append((fn, ("formal_parameter", "local_variable_declaration")))
    cls = _enclosing(at, _TYPE_SCOPES)
    body = cls.child_by_field_name("body") if cls is not None else None
    if body is not None:
        scopes.append((body, ("field_declaration",)))
    for scope, kinds in scopes:
        stop = _FUNCTION_SCOPES + _TYPE_SCOPES if scope is not fn else _TYPE_SCOPES
        types = {node_text(t, source) for n, t in _declarators(scope, kinds, stop=stop)
                 if node_text(n, source).lower() == want}
        if types:
            return types.pop() if len({t.lower() for t in types}) == 1 else None
    return None


def in_test_code(node: Node, source: bytes) -> bool:
    """Inside an ``@IsTest`` / ``testMethod`` method, or a class marked ``@IsTest``."""
    cur = node.parent
    while cur is not None:
        if cur.type in _FUNCTION_SCOPES + _TYPE_SCOPES:
            modifiers = modifiers_node(cur)
            if has_annotation(extract_annotations(modifiers, source), "IsTest"):
                return True
            if "testmethod" in {k.lower() for k in modifier_keywords(modifiers)}:
                return True
        cur = cur.parent
    return False


def _is_http_receiver(call: Node, source: bytes) -> bool:
    obj = call.child_by_field_name("object")
    if obj is None:
        return False
    if obj.type == "object_creation_expression":
        typ = obj.child_by_field_name("type")
        return typ is not None and node_text(typ, source).lower() == "http"
    if obj.type != "identifier":
        return False
    declared = declared_type(node_text(obj, source), call, source)
    return declared is not None and declared.lower() == "http"


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "string_literal":
        return node_text(node, source).strip("'")
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _nearest_block(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type != "block":
        cur = cur.parent
    return cur


def _request_settings(call: Node, source: bytes) -> tuple[Node | None, Node | None]:
    """The ``setMethod`` / ``setEndpoint`` argument that is in force for ``send(<request>)``:
    the last setter written after the request variable's last assignment and before the
    call. A value is left out when it could differ by path — the last assignment, or a
    competing setter, sits in a block that does not enclose the call (an ``if`` branch)."""
    args = call.child_by_field_name("arguments")
    scope = _enclosing(call, _FUNCTION_SCOPES)
    if args is None or scope is None or not args.named_children:
        return None, None
    request = node_text(args.named_children[0], source).lower()
    enclosing = set()
    cur = call.parent
    while cur is not None:
        enclosing.add(cur.id)
        cur = cur.parent

    def on_path(node: Node) -> bool:
        block = _nearest_block(node)
        return block is not None and block.id in enclosing

    assigned: list[Node] = []
    setters: dict[str, list[Node]] = {"setmethod": [], "setendpoint": []}
    stack = [scope]
    while stack:
        node = stack.pop()
        if node.start_byte >= call.start_byte:
            continue
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and node_text(name, source).lower() == request:
                assigned.append(node)
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and node_text(left, source).lower() == request:
                assigned.append(node)
        elif node.type == "method_invocation":
            name = node.child_by_field_name("name")
            obj = node.child_by_field_name("object")
            setter = node_text(name, source).lower() if name is not None else ""
            if setter in setters and obj is not None and node_text(obj, source).lower() == request:
                setters[setter].append(node)
        stack.extend(node.named_children)

    last = max(assigned, key=lambda n: n.start_byte, default=None)
    if last is not None and not on_path(last):
        return None, None
    after = last.start_byte if last is not None else -1

    def in_force(kind: str) -> Node | None:
        found = sorted((n for n in setters[kind] if n.start_byte > after), key=lambda n: n.start_byte)
        if not found or not all(on_path(n) for n in found):
            return None
        call_args = found[-1].child_by_field_name("arguments")
        return call_args.named_children[0] if call_args is not None and call_args.named_children else None

    return in_force("setmethod"), in_force("setendpoint")


def _callout(call: Node, source: bytes) -> Hit:
    method_arg, endpoint_arg = _request_settings(call, source)
    verb = _render_url(method_arg, source) if method_arg is not None else None
    endpoint = _render_url(endpoint_arg, source) if endpoint_arg is not None else None
    return "api_call", verb.upper() if verb else None, endpoint, None


def _first_args(call: Node, count: int) -> list[Node] | None:
    args = call.child_by_field_name("arguments")
    if args is None or len(args.named_children) < count:
        return None
    return list(args.named_children)


def _base_type(typ: str) -> str:
    """``List<Order_Event__e>`` → ``Order_Event__e``; a plain type is returned as is."""
    while "<" in typ and typ.endswith(">"):
        typ = typ[typ.index("<") + 1:-1].strip()
    return typ


def _event_channel(call: Node, source: bytes) -> str | None:
    """The Platform Event type published by ``EventBus.publish(<arg>)``, when the code shows it:
    ``new X__e(…)``, or a variable / list declared as ``X__e`` in the same method or class."""
    args = _first_args(call, 1)
    if args is None:
        return None
    arg = args[0]
    if arg.type == "object_creation_expression":
        typ_node = arg.child_by_field_name("type")
        typ = node_text(typ_node, source) if typ_node is not None else None
    elif arg.type == "identifier":
        typ = declared_type(node_text(arg, source), call, source)
    else:
        typ = None
    typ = _base_type(typ) if typ else None
    return typ if typ and typ.lower().endswith("__e") else None


def _schedule(call: Node, source: bytes) -> Hit | None:
    """``System.schedule(name, cron, job)`` outside test code → a ``timer`` with its cron."""
    if in_test_code(call, source):
        return None
    args = _first_args(call, 3)
    cron = node_text(args[1], source).strip("'") if args and args[1].type == "string_literal" else None
    return "timer", "PERIODIC", cron or None, None


def schedule_job_class(call: Node, source: bytes) -> str | None:
    """The class whose ``execute`` a ``System.schedule`` call runs: ``new Job()``, or a variable
    whose declared type is visible from the call."""
    args = _first_args(call, 3)
    if args is None:
        return None
    job = args[2]
    if job.type == "object_creation_expression":
        typ = job.child_by_field_name("type")
        return node_text(typ, source) if typ is not None else None
    if job.type == "identifier":
        return declared_type(node_text(job, source), call, source)
    return None


def is_schedule_call(node: Node, source: bytes) -> bool:
    if node.type != "method_invocation":
        return False
    name = node.child_by_field_name("name")
    return (name is not None and node_text(name, source).lower() == "schedule"
            and _receiver(node, source) == "system")


def classify_node(node: Node, source: bytes) -> Hit | None:
    if node.type == "query_expression":
        return "query_statement", None, None, None
    if node.type == "dml_expression":
        dml = next((c for c in node.named_children if c.type == "dml_type"), None)
        verb = node_text(dml, source).lower() if dml is not None else ""
        return ("db_method_call", verb, None, _DATA_ACCESS_HINT) if verb else None
    if node.type != "method_invocation":
        return None
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name, receiver = node_text(name_node, source).lower(), _receiver(node, source)
    if receiver == "database":
        if name in _QUERY_METHODS:
            return "query_statement", _QUERY_METHODS[name], None, None
        if name in _DML_METHODS:
            return "db_method_call", name, None, _DATA_ACCESS_HINT
    elif receiver == "search" and name == "query":
        return "query_statement", "query", None, None
    elif is_schedule_call(node, source):
        return _schedule(node, source)
    elif receiver == "eventbus" and name == "publish":
        return "eventbus_publish", None, _event_channel(node, source), None
    elif name == "send" and _is_http_receiver(node, source):
        return _callout(node, source)
    return None
