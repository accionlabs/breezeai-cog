"""Apex route detection, built from the finished ``FileRecord``.

Three kinds of entry point are reported:

* ``@RestResource(urlMapping=…)`` classes whose methods carry ``@HttpGet`` / ``@HttpPost`` /
  ``@HttpPut`` / ``@HttpPatch`` / ``@HttpDelete``; served under ``/services/apexrest``.
* ``@AuraEnabled`` methods, called by Lightning components as ``Class.method``; they have
  no HTTP verb.
* ``webservice`` methods, exposed as SOAP operations.

Test classes (``@IsTest``) are never entry points and emit nothing.
"""

from __future__ import annotations

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Function, Statement
from .modifiers import has_annotation

_REST_PREFIX = "/services/apexrest"
_VERBS = {"httpget": "GET", "httppost": "POST", "httpput": "PUT",
          "httppatch": "PATCH", "httpdelete": "DELETE"}


def _url_mapping(decorators: list[Decorator]) -> str | None:
    for d in decorators:
        if d.name.lower() == "restresource":
            for arg in d.args:
                key, _, value = arg.partition("=")
                if key.strip().lower() == "urlmapping":
                    return value
    return None


def _join(url: str) -> str:
    return _REST_PREFIX + "/" + url.lstrip("/")


def detect_apex_routes(record: FileRecord) -> list[Statement]:
    by_id = {c.id: c for c in record.classes}

    def in_test_class(cls_id: str) -> bool:
        while cls_id in by_id:
            cls = by_id[cls_id]
            if has_annotation(cls.decorators, "IsTest"):
                return True
            cls_id = cls.parentId
        return False

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []

    def add(fn: Function, text: str, method: str | None, endpoint: str,
            framework: str, kind: str) -> None:
        routes.append(Statement(
            id=disambiguate(statement_id(fn.path or "", fn.startLine, 0), seen),
            parentId=fn.id,
            nodeType="synthetic",
            semanticType="route",
            text=text,
            method=method,
            endpoint=endpoint,
            framework=framework,
            handler=fn.name,
            handlerLine=fn.startLine,
            routeKind=kind,
            isRegex=False,
            responseDTO=fn.returnType or None,
            startLine=fn.startLine,
            endLine=fn.endLine,
            path=fn.path,
        ))

    for fn in record.functions:
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
                    add(fn, f"@{d.name}", verb, _join(url), "apex-rest", "route")
        if has_annotation(fn.decorators, "AuraEnabled"):
            add(fn, "@AuraEnabled", None, f"{cls.name}.{fn.name}", "apex-aura", "aura")
        if fn.metadata and fn.metadata.get("webservice"):
            add(fn, "webservice", None, f"{cls.name}.{fn.name}", "apex-soap", "soap")
    return routes
