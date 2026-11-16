"""Laravel data access: Eloquent models and the ``DB`` facade (``db_method_call``). The rules
shared by every PHP data-access detector are in ``parsers/php_data_access_common.py``."""

from __future__ import annotations

from collections.abc import Callable

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php.class_ancestors import kinds_by_base
from ..php.receivers import Rules
from ..php_data_access_common import Kinds, detect_data_access

#: Laravel base classes → the kind of class that extends them.
_BASES = {
    "Illuminate\\Database\\Eloquent\\Model": "eloquent",
    "Illuminate\\Foundation\\Auth\\User": "eloquent",
    "Illuminate\\Database\\Eloquent\\Relations\\Pivot": "eloquent",
    "Illuminate\\Database\\Eloquent\\Relations\\MorphPivot": "eloquent",
}
_DB_FACADE = "Illuminate\\Support\\Facades\\DB"

_ELOQUENT_QUERY = {
    "query", "where", "wherein", "wherenotin", "wherenull", "wherenotnull", "wherebetween",
    "wherehas", "wheredoesnthave", "wherekey", "orwhere", "find", "findmany", "findorfail",
    "findornew", "first", "firstorfail", "firstwhere", "firstorcreate", "firstornew",
    "updateorcreate", "create", "createmany", "forcecreate", "insert", "insertorignore",
    "insertgetid", "upsert", "destroy", "all", "get", "count", "exists", "doesntexist", "pluck",
    "paginate", "simplepaginate", "cursorpaginate", "select", "with", "withcount", "has",
    "doesnthave", "orderby", "orderbydesc", "latest", "oldest", "limit", "take", "chunk",
    "chunkbyid", "cursor", "lazy", "lazybyid", "sum", "avg", "max", "min", "increment",
    "decrement", "truncate", "withtrashed", "onlytrashed", "update", "delete", "value", "sole",
}
_ELOQUENT_INSTANCE = {
    "save", "savequietly", "update", "updatequietly", "delete", "deletequietly", "forcedelete",
    "restore", "refresh", "fresh", "push", "increment", "decrement", "touch", "load",
    "loadmissing", "loadcount",
}
_DB_FACADE_METHODS = {
    "table", "select", "selectone", "insert", "update", "delete", "statement", "unprepared",
    "affectingstatement", "scalar", "query",
}

_KINDS: Kinds = {
    "eloquent": (_ELOQUENT_QUERY | _ELOQUENT_INSTANCE, "eloquent"),
    "eloquent-instance": (_ELOQUENT_INSTANCE, "eloquent"),
    # The facade starts a query builder, whose methods are Eloquent's builder methods.
    "db-facade": (_DB_FACADE_METHODS | _ELOQUENT_QUERY, None),
}


def _static(models: dict[str, str]) -> Callable[[str, str], str | None]:
    def kind(fqcn: str, method: str) -> str | None:
        if fqcn == _DB_FACADE:
            # Only a call that starts a query (table / select / insert / statement …).
            return "db-facade" if method in _DB_FACADE_METHODS else None
        # A static model call must itself start a query: Scout's ``Song::search(...)`` hits a
        # search engine, ``User::observe(...)`` registers an observer.
        return "eloquent" if models.get(fqcn) == "eloquent" and method in _ELOQUENT_QUERY else None
    return kind


def _detect(dc: DetectContext) -> Detection | None:
    models = kinds_by_base(dc.ctx.resolution_index, _BASES)
    if not models and b"Illuminate\\" not in dc.source:
        return None
    rules = Rules(
        types=models,
        static=_static(models),
        instance=lambda kind: "eloquent-instance" if kind == "eloquent" else kind,
    )
    return detect_data_access(dc, rules, _KINDS)


register_detector(Detector(
    name="laravel-data-access",
    gate=Gate("php", order=40),
    detect=_detect,
))
