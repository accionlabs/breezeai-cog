"""Apex entry points, built from the finished ``FileRecord``.

Four kinds are reported (role table: ``docs/entry-point-roles/apex.md``):

* ``@RestResource(urlMapping=…)`` classes whose methods carry ``@HttpGet`` / ``@HttpPost`` /
  ``@HttpPut`` / ``@HttpPatch`` / ``@HttpDelete``; served under ``/services/apexrest``.
* ``@AuraEnabled`` methods, called by Lightning components as ``Class.method`` — an RPC.
* ``webservice`` methods, exposed as SOAP operations ``Class/method`` — an RPC.
* triggers, run by the platform when records of their sObject change, or when a message
  arrives on a Platform Event / Change Data Capture channel — a consumer of that sObject.

Test classes (``@IsTest``) and fixture files are never entry points and emit nothing.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Function, Statement
from ..treesitter import end_line, first_line, node_text
from .modifiers import has_annotation, modifiers_node

_REST_PREFIX = "/services/apexrest"
_VERBS = {"httpget": "GET", "httppost": "POST", "httpput": "PUT",
          "httppatch": "PATCH", "httpdelete": "DELETE"}
#: sObject suffixes of event channels: Platform Events and Change Data Capture events.
_EVENT_SUFFIXES = ("__e", "changeevent")


def _url_mapping(decorators: list[Decorator]) -> str | None:
    for d in decorators:
        if d.name.lower() == "restresource":
            for arg in d.args:
                key, _, value = arg.partition("=")
                if key.strip().lower() == "urlmapping":
                    return value.strip().strip("'")
    return None


def _join(url: str) -> str:
    return _REST_PREFIX + "/" + url.lstrip("/")


def _response_dto(return_type: str | None) -> str | None:
    return return_type if return_type and return_type.lower() != "void" else None


def method_nodes(root: Node, source: bytes) -> dict[tuple[str, int], Node]:
    """``(name, startLine)`` → each method's declaration — the key its ``Function`` carries."""
    found: dict[tuple[str, int], Node] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "method_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                found[(node_text(name, source), node.start_point[0] + 1)] = node
        stack.extend(node.named_children)
    return found


def _webservice(method: Node) -> Node | None:
    """The ``webservice`` keyword's ``modifier`` node, when the method is declared with it."""
    modifiers = modifiers_node(method)
    for child in modifiers.named_children if modifiers is not None else []:
        if child.type == "modifier" and child.children and child.children[0].type == "webservice":
            return child
    return None


def _annotation(method: Node | None, fn: Function, index: int) -> Node | None:
    """The ``annotation`` node ``fn.decorators[index]`` was read from (the decorators are the
    named annotations, in order); None when they cannot be matched one to one."""
    modifiers = modifiers_node(method) if method is not None else None
    if modifiers is None:
        return None
    anns = [n for n in modifiers.named_children
            if n.type == "annotation" and n.child_by_field_name("name") is not None]
    return anns[index] if len(anns) == len(fn.decorators) else None


def trigger_headers(root: Node, source: bytes) -> dict[int, tuple[Node, str]]:
    """startLine → each trigger's declaration and source header
    (``trigger T on Account (before insert)``)."""
    out: dict[int, tuple[Node, str]] = {}
    for node in root.named_children:
        body = node.child_by_field_name("body") if node.type == "trigger_declaration" else None
        if body is not None:
            header = source[node.start_byte:body.start_byte].decode("utf-8", "replace")
            out[node.start_point[0] + 1] = (node, " ".join(header.split()))
    return out


def trigger_framework(sobject: str) -> str:
    return "apex-platform-event" if sobject.lower().endswith(_EVENT_SUFFIXES) else "apex-trigger"


def detect_apex_routes(record: FileRecord, root: Node, source: bytes) -> list[Statement]:
    by_id = {c.id: c for c in record.classes}
    methods = method_nodes(root, source)
    headers = trigger_headers(root, source)

    def in_test_class(cls_id: str) -> bool:
        while cls_id in by_id:
            cls = by_id[cls_id]
            if has_annotation(cls.decorators, "IsTest"):
                return True
            cls_id = cls.parentId
        return False

    seen = {s.id for s in record.statements}
    out: list[Statement] = []

    def add(fn: Function, node: Node | None, text: str, semantic: str, method: str | None,
            endpoint: str, framework: str, kind: str | None, *, dto: bool = True) -> None:
        """One record at ``node``, the construct it was read from (its own nodeType, span and
        first line). Without one (it cannot be matched) the method stands in, as ``synthetic``
        with ``text`` and the method's span."""
        if node is not None:
            line, col = node.start_point[0] + 1, node.start_point[1]
            last, node_type = end_line(node), node.type
            text = first_line(node_text(node, source)) if node.type != "trigger_declaration" else text
        else:
            line, col, last, node_type = fn.startLine, 0, fn.endLine, "synthetic"
        out.append(Statement(
            id=disambiguate(statement_id(fn.path or "", line, col), seen),
            parentId=fn.id,
            nodeType=node_type,
            semanticType=semantic,  # type: ignore[arg-type]
            text=text,
            method=method,
            endpoint=endpoint,
            framework=framework,
            handler=fn.name,
            handlerLine=fn.startLine,
            routeKind=kind,
            isRegex=False if kind else None,
            responseDTO=_response_dto(fn.returnType) if dto else None,
            startLine=line,
            endLine=last,
            path=fn.path,
        ))

    for fn in record.functions:
        if fn.type == "trigger":
            sobject = (fn.metadata or {}).get("sObject")
            trigger = headers.get(fn.startLine)
            if sobject and trigger:
                add(fn, trigger[0], trigger[1], "eventbus_consumer", "EVENT", sobject,
                    trigger_framework(sobject), None, dto=False)
            continue
        cls = by_id.get(fn.parentId)
        if cls is None or fn.type != "method" or in_test_class(cls.id):
            continue
        if has_annotation(fn.decorators, "IsTest"):
            continue
        decl = methods.get((fn.name, fn.startLine))
        url = _url_mapping(cls.decorators)
        if url is not None:
            for i, d in enumerate(fn.decorators):
                verb = _VERBS.get(d.name.lower())
                if verb:
                    add(fn, _annotation(decl, fn, i), f"@{d.name}", "route", verb, _join(url),
                        "apex-rest", "route")
        aura = next((i for i, d in enumerate(fn.decorators) if d.name.lower() == "auraenabled"),
                    None)
        if aura is not None:
            add(fn, _annotation(decl, fn, aura), "@AuraEnabled", "route", "RPC",
                f"{cls.name}.{fn.name}", "apex-aura", "rpc")
        keyword = _webservice(decl) if decl is not None else None
        if keyword is not None:
            add(fn, keyword, "webservice", "route", "RPC", f"{cls.name}/{fn.name}", "apex-soap",
                "rpc")
    return out
