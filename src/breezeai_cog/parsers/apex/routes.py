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
from ..treesitter import node_text
from .modifiers import has_annotation, modifier_keywords, modifiers_node

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


def webservice_methods(root: Node, source: bytes) -> set[tuple[str, int]]:
    """``(name, startLine)`` of every method declared with the ``webservice`` keyword."""
    found: set[tuple[str, int]] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "method_declaration" and "webservice" in modifier_keywords(modifiers_node(node)):
            name = node.child_by_field_name("name")
            if name is not None:
                found.add((node_text(name, source), node.start_point[0] + 1))
        stack.extend(node.named_children)
    return found


def trigger_headers(root: Node, source: bytes) -> dict[int, str]:
    """startLine → source header (``trigger T on Account (before insert)``) of each trigger."""
    out: dict[int, str] = {}
    for node in root.named_children:
        body = node.child_by_field_name("body") if node.type == "trigger_declaration" else None
        if body is not None:
            header = source[node.start_byte:body.start_byte].decode("utf-8", "replace")
            out[node.start_point[0] + 1] = " ".join(header.split())
    return out


def trigger_framework(sobject: str) -> str:
    return "apex-platform-event" if sobject.lower().endswith(_EVENT_SUFFIXES) else "apex-trigger"


def detect_apex_routes(record: FileRecord, root: Node, source: bytes) -> list[Statement]:
    by_id = {c.id: c for c in record.classes}
    soap = webservice_methods(root, source)
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

    def add(fn: Function, text: str, semantic: str, method: str | None, endpoint: str,
            framework: str, kind: str | None, *, dto: bool = True) -> None:
        out.append(Statement(
            id=disambiguate(statement_id(fn.path or "", fn.startLine, 0), seen),
            parentId=fn.id,
            nodeType="synthetic",
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
            startLine=fn.startLine,
            endLine=fn.endLine,
            path=fn.path,
        ))

    for fn in record.functions:
        if fn.type == "trigger":
            sobject = (fn.metadata or {}).get("sObject")
            header = headers.get(fn.startLine)
            if sobject and header:
                add(fn, header, "eventbus_consumer", "EVENT", sobject,
                    trigger_framework(sobject), None, dto=False)
            continue
        cls = by_id.get(fn.parentId)
        if cls is None or fn.type != "method" or in_test_class(cls.id):
            continue
        if has_annotation(fn.decorators, "IsTest"):
            continue
        url = _url_mapping(cls.decorators)
        if url is not None:
            for d in fn.decorators:
                verb = _VERBS.get(d.name.lower())
                if verb:
                    add(fn, f"@{d.name}", "route", verb, _join(url), "apex-rest", "route")
        if has_annotation(fn.decorators, "AuraEnabled"):
            add(fn, "@AuraEnabled", "route", "RPC", f"{cls.name}.{fn.name}", "apex-aura", "rpc")
        if (fn.name, fn.startLine) in soap:
            add(fn, "webservice", "route", "RPC", f"{cls.name}/{fn.name}", "apex-soap", "rpc")
    return out
